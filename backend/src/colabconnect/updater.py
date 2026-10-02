"""Self-update from GitHub releases.

Each release (built by .github/workflows/release.yml) carries an asset
``CyColabConnect-<version>.zip`` containing the backend sources, the built
frontend and the launchers. Installing replaces those files in the install
directory, then asks the launcher to restart the app (exit code 3).
A git checkout is never overwritten: it is told to ``git pull`` instead.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from . import __version__, config
from . import settings as app_settings
from .events import bus

logger = logging.getLogger(__name__)

REPO = os.environ.get("COLABCONNECT_UPDATE_REPO", "MrMybal/CyColabConnect")
INSTALL_ROOT = Path(__file__).resolve().parents[3]
CHECK_TTL = 6 * 3600
# Paths (relative to the install root) replaced wholesale by an update.
REPLACED_DIRS = ("backend/src", "frontend/dist")
REQUIRED_IN_ARCHIVE = ("backend/pyproject.toml", "backend/src/colabconnect/__init__.py", "frontend/dist/index.html")
RESTART_EXIT_CODE = 3


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", v.strip())
    if not m:
        raise ValueError(f"Version invalide : {v}")
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def install_mode() -> str:
    return "git" if (INSTALL_ROOT / ".git").exists() else "release"


def _token() -> str | None:
    return (
        os.environ.get("COLABCONNECT_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or app_settings.load().get("github_token")
        or None
    )


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CyColabConnect"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class Updater:
    def __init__(self) -> None:
        self._last: dict[str, Any] | None = None
        self._last_check = 0.0
        self._installing = False

    def status(self) -> dict[str, Any]:
        base = {
            "current": __version__,
            "repo": REPO,
            "install_mode": install_mode(),
            "has_token": bool(_token()),
            "installing": self._installing,
        }
        return {**base, **(self._last or {}), **base}

    async def check(self, force: bool = False) -> dict[str, Any]:
        if not force and self._last and time.time() - self._last_check < CHECK_TTL:
            return self.status()
        result: dict[str, Any] = {"checked_at": time.time(), "error": None}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
                resp = await http.get(f"https://api.github.com/repos/{REPO}/releases/latest", headers=_headers())
            if resp.status_code == 404:
                raise UpdateError(
                    "Aucune release trouvée. Si le dépôt est privé, ajoutez un token GitHub "
                    "(lecture du contenu) dans Paramètres."
                )
            if resp.status_code in (401, 403):
                raise UpdateError(f"Accès GitHub refusé (HTTP {resp.status_code}) : vérifiez le token.")
            resp.raise_for_status()
            release = resp.json()
            latest = release["tag_name"].lstrip("v")
            asset = next(
                (a for a in release.get("assets", []) if a["name"] == f"CyColabConnect-{latest}.zip"), None
            )
            result.update(
                latest=latest,
                update_available=is_newer(latest, __version__),
                notes=release.get("body") or "",
                published_at=release.get("published_at"),
                html_url=release.get("html_url"),
                asset=(
                    {"id": asset["id"], "name": asset["name"], "size": asset["size"],
                     "digest": asset.get("digest")}
                    if asset else None
                ),
            )
        except (UpdateError, httpx.HTTPError, KeyError, ValueError) as e:
            result.update(error=str(e), update_available=False)
        self._last = result
        self._last_check = time.time()
        if result.get("update_available"):
            bus.publish("update_available", **self.status())
        return self.status()

    async def install(self) -> dict[str, Any]:
        """Download, verify and apply the latest release. Caller then restarts the app."""
        if install_mode() == "git":
            raise UpdateError(
                "Installation depuis un clone git : utilisez `git pull` (les fichiers ne sont pas écrasés)."
            )
        status = await self.check(force=True)
        if status.get("error"):
            raise UpdateError(status["error"])
        if not status.get("update_available"):
            raise UpdateError("Déjà à jour.")
        asset = status.get("asset")
        if not asset:
            raise UpdateError(f"La release {status['latest']} ne contient pas d'archive CyColabConnect.")

        self._installing = True
        bus.publish("update_progress", step="download", version=status["latest"])
        try:
            workdir = Path(tempfile.mkdtemp(prefix="cc-update-", dir=config.APP_DIR))
            archive = workdir / asset["name"]
            await self._download(asset, archive)
            bus.publish("update_progress", step="extract", version=status["latest"])
            staging = workdir / "staging"
            extract_safely(archive, staging)
            root = find_release_root(staging)
            bus.publish("update_progress", step="apply", version=status["latest"])
            apply_release(root, INSTALL_ROOT)
            shutil.rmtree(workdir, ignore_errors=True)
        finally:
            self._installing = False
        bus.publish("update_progress", step="restart", version=status["latest"])
        return {"installed": status["latest"], "restart_exit_code": RESTART_EXIT_CODE}

    async def _download(self, asset: dict[str, Any], dest: Path) -> None:
        url = f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}"
        sha = hashlib.sha256()
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), follow_redirects=True) as http:
            async with http.stream("GET", url, headers=_headers("application/octet-stream")) as resp:
                if resp.status_code >= 400:
                    raise UpdateError(f"Téléchargement impossible : HTTP {resp.status_code}")
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        sha.update(chunk)
        digest = asset.get("digest")
        if digest and digest.startswith("sha256:") and digest[7:] != sha.hexdigest():
            raise UpdateError("Somme de contrôle SHA-256 invalide : archive corrompue, mise à jour annulée.")


def extract_safely(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if root != target and root not in target.parents:
                raise UpdateError(f"Chemin suspect dans l'archive : {member.filename}")
        zf.extractall(dest)


def find_release_root(staging: Path) -> Path:
    """The archive may contain the files directly or inside one top-level folder."""
    candidates = [staging, *[p for p in staging.iterdir() if p.is_dir()]]
    for c in candidates:
        if all((c / rel).exists() for rel in REQUIRED_IN_ARCHIVE):
            return c
    raise UpdateError("Archive de release invalide (fichiers manquants).")


def apply_release(src: Path, dest: Path) -> None:
    """Replace app files in ``dest`` with those from ``src``. User data lives elsewhere."""
    for rel in REPLACED_DIRS:
        target = dest / rel
        new = src / rel
        if not new.exists():
            continue
        backup = target.with_name(target.name + ".old")
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            target.rename(backup)
        shutil.copytree(new, target)
        shutil.rmtree(backup, ignore_errors=True)
    replaced = {Path(r) for r in REPLACED_DIRS}
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if path.is_dir() or any(rel.is_relative_to(r) for r in replaced):
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)


updater = Updater()
