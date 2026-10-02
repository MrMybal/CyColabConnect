"""Global app settings, persisted in ``$COLABCONNECT_HOME/settings.json``."""

from __future__ import annotations

import json
from typing import Any

from . import config

DEFAULTS: dict[str, Any] = {
    # Release managed runtimes idle for this many minutes (0 = never).
    "auto_stop_minutes": 60,
    # GitHub token used to check/download releases of a private repository.
    "github_token": "",
}

_PATH = config.APP_DIR / "settings.json"


def load() -> dict[str, Any]:
    try:
        stored = json.loads(_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        stored = {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def public() -> dict[str, Any]:
    """Settings safe to send to the UI (the token is never echoed back)."""
    data = load()
    data["has_github_token"] = bool(data.pop("github_token"))
    return data


def update(**changes: Any) -> dict[str, Any]:
    current = load()
    if "auto_stop_minutes" in changes:
        minutes = int(changes["auto_stop_minutes"])
        if minutes < 0:
            raise ValueError("auto_stop_minutes doit être >= 0")
        current["auto_stop_minutes"] = minutes
    if "github_token" in changes:
        current["github_token"] = str(changes["github_token"]).strip()
    config.ensure_dirs()
    _PATH.write_text(json.dumps(current, indent=2))
    return current
