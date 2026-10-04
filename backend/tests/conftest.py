"""Test fixtures: a fake Colab tunnel API backed by a real local Jupyter server.

No Google account is needed: the fake /tun/m/* endpoints hand out a
runtimeProxyInfo that points at a jupyter_server running on localhost, so the
kernel protocol, orchestration and MCP tools run against a real ipykernel.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="colabconnect-test-"))
os.environ["COLABCONNECT_HOME"] = str(_HOME)

import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """In-memory credential store so tests never touch the real OS keyring."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


keyring.set_keyring(MemoryKeyring())

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


@contextmanager
def run_jupyter_server():
    port = _free_port()
    root = Path(tempfile.mkdtemp(prefix="jupyter_root"))
    (root / "content").mkdir()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jupyter_server",
            f"--port={port}", "--ip=127.0.0.1", "--no-browser",
            "--IdentityProvider.token=", "--ServerApp.password=",
            "--ServerApp.disable_check_xsrf=True",
            f"--ServerApp.root_dir={root}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(url + "/api")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="session")
def jupyter_url():
    with run_jupyter_server() as url:
        yield url


@pytest.fixture(scope="session")
def fake_colab(jupyter_url):
    with run_fake_colab(jupyter_url) as result:
        yield result


@contextmanager
def run_fake_colab(jupyter_url):
    """Fake colab.research.google.com /tun/m API. Yields (url, state)."""
    app = FastAPI()
    state: dict = {"assignments": {}, "calls": []}

    def xssi(data) -> PlainTextResponse:
        return PlainTextResponse(")]}'\n" + json.dumps(data))

    def proxy_info():
        return {"url": jupyter_url, "token": "proxy-token", "tokenExpiresInSeconds": 3600}

    @app.get("/tun/m/assign")
    async def get_assign(request: Request):
        state["calls"].append(("GET assign", dict(request.query_params)))
        assert request.headers["authorization"] == "Bearer fake-access-token"
        return xssi({"acc": "", "nbh": request.query_params["nbh"], "token": "xsrf", "variant": "DEFAULT"})

    @app.post("/tun/m/assign")
    async def post_assign(request: Request):
        assert request.headers["x-goog-colab-token"] == "xsrf"
        acc = request.query_params.get("accelerator", "NONE")
        if acc == "H100":
            return PlainTextResponse("no quota", status_code=400)
        endpoint = f"gpu-{acc.lower()}-{uuid.uuid4().hex[:6]}"
        variant = {"GPU": 1, "TPU": 2}.get(request.query_params.get("variant", ""), 0)
        state["assignments"][endpoint] = {
            "accelerator": acc, "endpoint": endpoint, "variant": variant,
            "machineShape": 1 if request.query_params.get("shape") == "hm" else 0,
        }
        return xssi({**state["assignments"][endpoint], "runtimeProxyInfo": proxy_info()})

    @app.get("/tun/m/assignments")
    async def assignments():
        return xssi({"assignments": [
            {**a, "runtimeProxyInfo": proxy_info()} for a in state["assignments"].values()
        ]})

    @app.get("/tun/m/unassign/{endpoint}")
    async def get_unassign(endpoint: str):
        return xssi({"token": "xsrf-un"})

    @app.post("/tun/m/unassign/{endpoint}")
    async def post_unassign(endpoint: str):
        state["assignments"].pop(endpoint, None)
        return xssi({})

    @app.get("/tun/m/ccu-info")
    async def ccu():
        return xssi({"currentBalance": 97.5, "consumptionRateHourly": 1.76,
                     "assignmentsCount": len(state["assignments"])})

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    _wait_http(url + "/tun/m/ccu-info")
    try:
        yield url, state
    finally:
        server.should_exit = True
        thread.join(5)


class FakeCreds:
    valid = True
    token = "fake-access-token"

    def to_json(self) -> str:
        return "{}"


@pytest.fixture
def service(fake_colab):
    from colabconnect.service import ColabService

    svc = ColabService()
    svc.client.domain = fake_colab[0]
    svc.auth._creds = FakeCreds()
    svc.auth._method = "oauth"
    return svc
