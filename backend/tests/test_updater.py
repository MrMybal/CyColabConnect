from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from colabconnect import __version__, updater
from colabconnect import settings as app_settings


def test_version_is_consistent():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert re.search(r'(?m)^version = "([^"]+)"', pyproject).group(1) == __version__


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [("0.2.0", "0.1.0", True), ("v0.1.1", "0.1.0", True), ("0.1.0", "0.1.0", False),
     ("0.10.0", "0.9.9", True), ("1.0", "1.0.0", False), ("0.1.0", "0.2.0", False)],
)
def test_is_newer(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def _make_release(tmp_path: Path, version: str) -> Path:
    root = tmp_path / "build" / f"CyColabConnect-{version}"
    (root / "backend/src/colabconnect").mkdir(parents=True)
    (root / "backend/src/colabconnect/__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "backend/pyproject.toml").write_text(f'version = "{version}"\n')
    (root / "frontend/dist").mkdir(parents=True)
    (root / "frontend/dist/index.html").write_text("new ui")
    (root / "README.md").write_text("new readme")
    archive = tmp_path / f"CyColabConnect-{version}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for p in root.rglob("*"):
            zf.write(p, p.relative_to(root.parent))
    return archive


def test_extract_and_apply(tmp_path):
    install = tmp_path / "install"
    (install / "backend/src/colabconnect").mkdir(parents=True)
    (install / "backend/src/colabconnect/obsolete.py").write_text("old")
    (install / "backend/.venv").mkdir()
    (install / "backend/.venv/keep").write_text("venv")
    (install / "frontend/dist/assets").mkdir(parents=True)
    (install / "frontend/dist/assets/old.js").write_text("old")

    staging = tmp_path / "staging"
    updater.extract_safely(_make_release(tmp_path, "9.9.9"), staging)
    root = updater.find_release_root(staging)
    updater.apply_release(root, install)

    assert '"9.9.9"' in (install / "backend/src/colabconnect/__init__.py").read_text()
    assert not (install / "backend/src/colabconnect/obsolete.py").exists()  # src replaced wholesale
    assert not (install / "frontend/dist/assets/old.js").exists()
    assert (install / "frontend/dist/index.html").read_text() == "new ui"
    assert (install / "README.md").read_text() == "new readme"
    assert (install / "backend/.venv/keep").read_text() == "venv"  # untouched
    assert not list(install.rglob("*.old"))


def test_rejects_zip_slip(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.txt", "x")
    with pytest.raises(updater.UpdateError):
        updater.extract_safely(archive, tmp_path / "out")


def test_rejects_incomplete_archive(tmp_path):
    (tmp_path / "staging/whatever").mkdir(parents=True)
    with pytest.raises(updater.UpdateError):
        updater.find_release_root(tmp_path / "staging")


def test_token_never_exposed():
    app_settings.update(github_token="ghp_secret_value")
    try:
        public = app_settings.public()
        assert public["has_github_token"] is True
        assert "ghp_secret_value" not in str(public)
    finally:
        app_settings.update(github_token="")


@pytest.mark.asyncio
async def test_git_checkout_is_never_overwritten(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updater, "INSTALL_ROOT", tmp_path)
    with pytest.raises(updater.UpdateError, match="git pull"):
        await updater.Updater().install()
