"""Google OAuth for the Colab backend.

Uses the same "remote copy-paste" flow as google-colab-cli: the user opens an
authorization URL, Google shows a code on sdk.cloud.google.com, and the user
pastes it back. That redirect page is only registered for Google's cloud-SDK
client, which is why the bundled ``oauth_client.json`` uses that client id.
Application Default Credentials (gcloud) are supported as an alternative.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import warnings
from importlib import resources
from typing import Any

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import config

logger = logging.getLogger(__name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
ADC_LOGIN_COMMAND = (
    "gcloud auth application-default login --scopes=openid,"
    "https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/userinfo.email,"
    "https://www.googleapis.com/auth/colaboratory"
)
_META_PATH = config.APP_DIR / "account.json"


class NotAuthenticatedError(Exception):
    pass


def _load_client_config() -> dict[str, Any]:
    if config.CUSTOM_OAUTH_CLIENT_PATH.exists():
        return json.loads(config.CUSTOM_OAUTH_CLIENT_PATH.read_text())
    text = resources.files("colabconnect").joinpath("oauth_client.json").read_text()
    return json.loads(text)


def _email_from_id_token(id_token: str | None) -> str | None:
    if not id_token:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except Exception:
        return None


class AuthManager:
    def __init__(self) -> None:
        self._creds: Any = None
        self._method: str | None = None  # "oauth" | "adc"
        self._email: str | None = None
        self._pending_flow: Flow | None = None
        self._lock = asyncio.Lock()
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        meta: dict[str, Any] = {}
        if _META_PATH.exists():
            try:
                meta = json.loads(_META_PATH.read_text())
            except Exception:
                meta = {}
        self._email = meta.get("email")
        if meta.get("method") == "adc":
            try:
                self._creds = self._adc_credentials()
                self._method = "adc"
            except Exception as e:
                logger.warning("ADC credentials unavailable: %s", e)
            return
        if config.TOKEN_PATH.exists():
            try:
                self._creds = Credentials.from_authorized_user_file(
                    str(config.TOKEN_PATH), SCOPES
                )
                self._method = "oauth"
            except Exception as e:
                logger.warning("Could not load %s: %s", config.TOKEN_PATH, e)

    def _save(self) -> None:
        config.ensure_dirs()
        if self._method == "oauth" and self._creds is not None:
            config.TOKEN_PATH.write_text(self._creds.to_json())
        _META_PATH.write_text(json.dumps({"method": self._method, "email": self._email}))

    # -- public API --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "authenticated": self._creds is not None,
            "method": self._method,
            "email": self._email,
            "login_pending": self._pending_flow is not None,
            "adc_command": ADC_LOGIN_COMMAND,
        }

    def start_login(self) -> str:
        flow = Flow.from_client_config(
            _load_client_config(), SCOPES, redirect_uri=REMOTE_REDIRECT_URI
        )
        url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
        self._pending_flow = flow
        return url

    async def finish_login(self, code: str) -> dict[str, Any]:
        flow = self._pending_flow
        if flow is None:
            raise ValueError("Aucune connexion en cours : relancez la connexion.")
        await asyncio.to_thread(flow.fetch_token, code=code.strip())
        self._creds = flow.credentials
        self._method = "oauth"
        self._email = _email_from_id_token(getattr(self._creds, "id_token", None))
        self._pending_flow = None
        self._save()
        return self.status()

    async def use_adc(self) -> dict[str, Any]:
        creds = await asyncio.to_thread(self._adc_credentials)
        await asyncio.to_thread(creds.refresh, Request())
        self._creds = creds
        self._method = "adc"
        self._email = getattr(creds, "service_account_email", None) or self._email
        self._save()
        return self.status()

    def logout(self) -> None:
        self._creds = None
        self._method = None
        self._email = None
        self._pending_flow = None
        config.TOKEN_PATH.unlink(missing_ok=True)
        _META_PATH.unlink(missing_ok=True)

    async def access_token(self) -> str:
        async with self._lock:
            creds = self._creds
            if creds is None:
                raise NotAuthenticatedError(
                    "Non connecté à Google. Connectez-vous depuis l'interface ColabConnect."
                )
            if not creds.valid:
                await asyncio.to_thread(creds.refresh, Request())
                if self._method == "oauth":
                    self._save()
            return creds.token

    @staticmethod
    def _adc_credentials() -> Any:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"Your application has authenticated.*")
            creds, _ = google.auth.default(scopes=list(SCOPES))
        if getattr(creds, "requires_scopes", False):
            try:
                creds = creds.with_scopes(list(SCOPES))
            except Exception:
                pass
        return creds
