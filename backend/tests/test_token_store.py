from __future__ import annotations

import datetime as dt
import json

import keyring
import pytest
from google.oauth2.credentials import Credentials
from keyring.backends.fail import Keyring as FailKeyring

from colabconnect import auth, config, token_store

CLIENT = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret",
          "token_uri": "https://oauth2.googleapis.com/token"}


def _creds() -> Credentials:
    return Credentials(
        token="ya29." + "a" * 220,
        refresh_token="1//0g" + "r" * 100,
        token_uri=CLIENT["token_uri"],
        client_id=CLIENT["client_id"],
        client_secret=CLIENT["client_secret"],
        scopes=auth.SCOPES,
        expiry=dt.datetime(2030, 1, 1, 12, 0, 0),
    )


@pytest.fixture(autouse=True)
def clean():
    token_store.delete()
    yield
    token_store.delete()


def test_roundtrip_in_keyring_without_plaintext_file():
    assert token_store.save(_creds()) == "keyring"
    assert not config.TOKEN_PATH.exists()

    raw = keyring.get_password(token_store.SERVICE, token_store.ACCOUNT)
    # Windows Credential Manager limit: 2560 bytes of UTF-16; refresh tokens are <= 512 bytes.
    assert len(raw.encode("utf-16-le")) < 2560
    assert json.loads(raw) == {"refresh_token": _creds().refresh_token}  # only the minimum

    loaded = token_store.load(CLIENT, auth.SCOPES)
    assert loaded.refresh_token == _creds().refresh_token
    assert loaded.token is None and not loaded.valid  # access token re-fetched on first use
    assert loaded.client_id == CLIENT["client_id"] and loaded.client_secret == "csecret"


def test_keyring_write_error_falls_back_to_file(monkeypatch):
    def boom(*a, **k):
        raise OSError(1783, "CredWrite")

    monkeypatch.setattr(keyring, "set_password", boom)
    assert token_store.save(_creds()) == "file"
    assert token_store.load(CLIENT, auth.SCOPES).refresh_token == _creds().refresh_token


def test_migrates_legacy_plaintext_token():
    config.ensure_dirs()
    config.TOKEN_PATH.write_text(_creds().to_json())
    loaded = token_store.load(CLIENT, auth.SCOPES)
    assert loaded.refresh_token == _creds().refresh_token
    assert not config.TOKEN_PATH.exists(), "plaintext token must be removed after migration"
    assert json.loads(keyring.get_password(token_store.SERVICE, token_store.ACCOUNT))["refresh_token"]


def test_delete_removes_everything():
    token_store.save(_creds())
    token_store.delete()
    assert token_store.load(CLIENT, auth.SCOPES) is None


def test_falls_back_to_file_without_secure_backend(monkeypatch):
    real = keyring.get_keyring()
    keyring.set_keyring(FailKeyring())
    try:
        assert not token_store.secure_backend_available()
        assert token_store.save(_creds()) == "file"
        assert config.TOKEN_PATH.exists()
        assert token_store.load(CLIENT, auth.SCOPES).refresh_token == _creds().refresh_token
    finally:
        keyring.set_keyring(real)


def test_auth_manager_uses_keyring_and_logout_clears_it():
    token_store.save(_creds())
    manager = auth.AuthManager()
    assert manager.status()["authenticated"]
    assert manager.status()["token_storage"]
    manager.logout()
    assert keyring.get_password(token_store.SERVICE, token_store.ACCOUNT) is None
    assert not auth.AuthManager().status()["authenticated"]
