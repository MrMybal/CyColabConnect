"""Paths and settings for ColabConnect.

Everything lives under ``~/.colabconnect`` unless ``COLABCONNECT_HOME`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(os.environ.get("COLABCONNECT_HOME", Path.home() / ".colabconnect"))
TOKEN_PATH = APP_DIR / "token.json"
RUNTIMES_PATH = APP_DIR / "runtimes.json"
CUSTOM_OAUTH_CLIENT_PATH = APP_DIR / "oauth_client.json"
NOTEBOOKS_DIR = Path(os.environ.get("COLABCONNECT_NOTEBOOKS", APP_DIR / "notebooks"))

HOST = os.environ.get("COLABCONNECT_HOST", "127.0.0.1")
PORT = int(os.environ.get("COLABCONNECT_PORT", "8420"))

COLAB_DOMAIN = "https://colab.research.google.com"

# Refresh the runtime proxy token when it has less than this many seconds left.
PROXY_TOKEN_REFRESH_MARGIN = 5 * 60

# Default ceiling for one cell execution (seconds). None = no limit.
DEFAULT_CELL_TIMEOUT = float(os.environ.get("COLABCONNECT_CELL_TIMEOUT", "3600"))


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    NOTEBOOKS_DIR.mkdir(parents=True, exist_ok=True)
