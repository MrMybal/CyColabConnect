"""Secure storage for the Google OAuth token.

The token goes to the OS credential store through ``keyring``: Windows
Credential Manager (encrypted with the user's Windows account), macOS Keychain
or the Secret Service on Linux. Only the refresh token is stored: Windows
limits a credential to 2560 bytes (UTF-16, so ~1280 characters) while Google
access tokens can reach 2 KB. A fresh access token is fetched on first use; the
client id/secret and scopes are re-attached when loading.

If no secure backend is available (headless Linux…), falls back to the legacy
``token.json`` file, readable by the current user only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from google.oauth2.credentials import Credentials

from . import config

logger = logging.getLogger(__name__)

SERVICE = "CyColabConnect"
# One entry per data directory, so a test or second install never clobbers the real one.
ACCOUNT = "google-oauth:" + hashlib.sha256(str(config.APP_DIR.resolve()).encode()).hexdigest()[:12]


def secure_backend_available() -> bool:
    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    return "fail" not in name.lower() and getattr(backend, "priority", 0) > 0


def backend_name() -> str:
    if not secure_backend_available():
        return "fichier"
    name = type(keyring.get_keyring()).__name__
    return {
        "WinVaultKeyring": "Gestionnaire d'identifiants Windows",
        "Keyring": "Trousseau macOS" if "macOS" in type(keyring.get_keyring()).__module__ else "Secret Service",
    }.get(name, name)


def _serialize(creds: Credentials) -> str:
    if not creds.refresh_token:
        raise ValueError("Pas de refresh token à enregistrer")
    return json.dumps({"refresh_token": creds.refresh_token})


def _deserialize(raw: str, client: dict[str, Any], scopes: list[str]) -> Credentials:
    data = json.loads(raw)
    # No access token: google-auth sees the credentials as expired and refreshes them.
    return Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client["client_id"],
        client_secret=client.get("client_secret"),
        scopes=scopes,
    )


def save(creds: Credentials) -> str:
    """Store credentials; returns where they were stored."""
    if secure_backend_available():
        try:
            keyring.set_password(SERVICE, ACCOUNT, _serialize(creds))
            config.TOKEN_PATH.unlink(missing_ok=True)
            return "keyring"
        except Exception as e:  # KeyringError, or OS errors such as Windows' CredWrite
            logger.warning("Credential store unavailable (%s), falling back to file", e)
    config.ensure_dirs()
    config.TOKEN_PATH.write_text(creds.to_json())
    try:
        os.chmod(config.TOKEN_PATH, 0o600)
    except OSError:
        pass
    return "file"


def load(client: dict[str, Any], scopes: list[str]) -> Credentials | None:
    """Load credentials, migrating a legacy plaintext token.json into the keyring."""
    if secure_backend_available():
        try:
            raw = keyring.get_password(SERVICE, ACCOUNT)
        except Exception as e:
            logger.warning("Could not read credential store: %s", e)
            raw = None
        if raw:
            try:
                return _deserialize(raw, client, scopes)
            except (ValueError, KeyError) as e:
                logger.warning("Stored credential is invalid, ignoring it: %s", e)
    if config.TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), scopes)
        except Exception as e:
            logger.warning("Could not load %s: %s", config.TOKEN_PATH, e)
            return None
        if secure_backend_available() and save(creds) == "keyring":
            logger.info("Migrated Google token from %s to the OS credential store", config.TOKEN_PATH)
        return creds
    return None


def delete() -> None:
    if secure_backend_available():
        try:
            keyring.delete_password(SERVICE, ACCOUNT)
        except (PasswordDeleteError, KeyringError, OSError):
            pass
    config.TOKEN_PATH.unlink(missing_ok=True)
