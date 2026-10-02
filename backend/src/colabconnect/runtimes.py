"""Tracks Colab runtimes (VMs), their kernels, and the runtime file API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
from urllib.parse import quote

import contextlib
from typing import AsyncIterator
from urllib.parse import urlencode, urlparse

import httpx
import websockets

from . import config
from .colab_client import Assignment, ColabApiError, ColabClient
from .events import bus
from .kernel import KernelSession

logger = logging.getLogger(__name__)

DRIVE_CONSENT_TIMEOUT = 300


class RuntimeNotFound(Exception):
    pass


class RuntimeManager:
    def __init__(self, client: ColabClient) -> None:
        self.client = client
        self._records: dict[str, dict[str, Any]] = {}
        self._kernels: dict[str, KernelSession] = {}
        self._lock = asyncio.Lock()
        # In-memory activity tracking (for idle auto-stop).
        self._last_activity: dict[str, float] = {}
        self._terminals: dict[str, int] = {}
        self._load()
        now = time.time()
        for endpoint in self._records:
            self._last_activity[endpoint] = now

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if config.RUNTIMES_PATH.exists():
            try:
                self._records = json.loads(config.RUNTIMES_PATH.read_text())
            except Exception:
                self._records = {}

    def _save(self) -> None:
        config.ensure_dirs()
        config.RUNTIMES_PATH.write_text(json.dumps(self._records, indent=2))

    def _public(self, rec: dict[str, Any]) -> dict[str, Any]:
        kernel = self._kernels.get(rec["endpoint"])
        return {
            "endpoint": rec["endpoint"],
            "accelerator": rec["accelerator"],
            "variant": rec["variant"],
            "high_mem": rec["high_mem"],
            "notebook_id": rec.get("notebook_id"),
            "created_at": rec.get("created_at"),
            "managed": rec.get("managed", True),
            "kernel_id": rec.get("kernel_id"),
            "kernel_connected": bool(kernel and kernel.connected),
            "kernel_state": kernel.execution_state if kernel else "disconnected",
            "colab_url": self.client.browser_url(rec["endpoint"]),
            "last_activity": self._last_activity.get(rec["endpoint"]),
            "busy": self.is_busy(rec["endpoint"]),
            "terminals": self._terminals.get(rec["endpoint"], 0),
        }

    # -- activity ----------------------------------------------------------------

    def touch(self, endpoint: str) -> None:
        self._last_activity[endpoint] = time.time()

    def is_busy(self, endpoint: str) -> bool:
        kernel = self._kernels.get(endpoint)
        running = bool(kernel and (kernel.busy or kernel.execution_state == "busy"))
        return running or self._terminals.get(endpoint, 0) > 0

    def idle_endpoints(self, max_idle_seconds: float) -> list[str]:
        """Managed runtimes with no activity for ``max_idle_seconds``."""
        now = time.time()
        return [
            endpoint
            for endpoint, rec in self._records.items()
            if rec.get("managed", True)
            and not self.is_busy(endpoint)
            and now - self._last_activity.get(endpoint, now) >= max_idle_seconds
        ]

    # -- lifecycle -------------------------------------------------------------

    async def refresh(self) -> list[dict[str, Any]]:
        """Sync local records with the assignments that exist server-side."""
        assignments = await self.client.list_assignments()
        async with self._lock:
            live = {a.endpoint for a in assignments}
            for a in assignments:
                rec = self._records.get(a.endpoint)
                if rec is None:
                    rec = {**a.to_dict(), "created_at": time.time(), "managed": False}
                    self._records[a.endpoint] = rec
                    self._last_activity.setdefault(a.endpoint, time.time())
                else:
                    rec.update(proxy_url=a.proxy_url, proxy_token=a.proxy_token,
                               token_expires_at=a.token_expires_at)
            for endpoint in list(self._records):
                if endpoint not in live:
                    self._records.pop(endpoint)
                    self._last_activity.pop(endpoint, None)
                    kernel = self._kernels.pop(endpoint, None)
                    if kernel:
                        await kernel.close()
            self._save()
        runtimes = self.list()
        bus.publish("runtimes", runtimes=runtimes)
        return runtimes

    def list(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self._records.values()]

    def get(self, endpoint: str) -> dict[str, Any]:
        rec = self._records.get(endpoint)
        if rec is None:
            raise RuntimeNotFound(f"Runtime inconnu : {endpoint}")
        return self._public(rec)

    def exists(self, endpoint: str | None) -> bool:
        return bool(endpoint) and endpoint in self._records

    async def create(
        self, accelerator: str = "NONE", high_mem: bool = False, notebook_id: str | None = None
    ) -> dict[str, Any]:
        bus.publish("runtime_status", status="assigning", accelerator=accelerator, notebook_id=notebook_id)
        try:
            a: Assignment = await self.client.assign(accelerator, high_mem)
        except Exception as e:
            bus.publish("runtime_status", status="error", error=str(e), notebook_id=notebook_id)
            raise
        async with self._lock:
            self._records[a.endpoint] = {
                **a.to_dict(),
                "notebook_id": notebook_id,
                "created_at": time.time(),
                "managed": True,
            }
            self._save()
        self.touch(a.endpoint)
        bus.publish("runtime_status", status="ready", endpoint=a.endpoint, notebook_id=notebook_id)
        bus.publish("runtimes", runtimes=self.list())
        return self.get(a.endpoint)

    async def stop(self, endpoint: str) -> None:
        kernel = self._kernels.pop(endpoint, None)
        if kernel:
            await kernel.close(shutdown=True)
        try:
            await self.client.unassign(endpoint)
        except ColabApiError as e:
            if e.status != 404:
                raise
        async with self._lock:
            self._records.pop(endpoint, None)
            self._last_activity.pop(endpoint, None)
            self._save()
        bus.publish("runtime_status", status="stopped", endpoint=endpoint)
        bus.publish("runtimes", runtimes=self.list())

    def bind_notebook(self, endpoint: str, notebook_id: str | None) -> None:
        rec = self._records.get(endpoint)
        if rec is not None:
            rec["notebook_id"] = notebook_id
            self._save()

    # -- credentials -----------------------------------------------------------

    async def credentials(self, endpoint: str) -> tuple[str, str]:
        rec = self._records.get(endpoint)
        if rec is None:
            raise RuntimeNotFound(f"Runtime inconnu : {endpoint}")
        if rec["token_expires_at"] - time.time() < config.PROXY_TOKEN_REFRESH_MARGIN:
            await self.refresh()
            rec = self._records.get(endpoint)
            if rec is None:
                raise RuntimeNotFound(f"Le runtime {endpoint} n'existe plus côté Colab.")
        return rec["proxy_url"], rec["proxy_token"]

    # -- kernels ---------------------------------------------------------------

    def kernel(self, endpoint: str) -> KernelSession:
        if endpoint not in self._records:
            raise RuntimeNotFound(f"Runtime inconnu : {endpoint}")
        kernel = self._kernels.get(endpoint)
        if kernel is None:
            def remember(kernel_id: str) -> None:
                rec = self._records.get(endpoint)
                if rec is not None:
                    rec["kernel_id"] = kernel_id
                    self._save()

            kernel = KernelSession(
                credentials=lambda: self.credentials(endpoint),
                kernel_id=self._records[endpoint].get("kernel_id"),
                on_kernel_id=remember,
                colab_request_handler=lambda msg: self._colab_request(endpoint, msg),
                on_activity=lambda: self.touch(endpoint),
            )
            self._kernels[endpoint] = kernel
        return kernel

    async def _colab_request(self, endpoint: str, msg: dict[str, Any]) -> dict[str, Any] | None:
        request = (msg.get("content") or {}).get("request") or {}
        if request.get("authType") != "dfs_ephemeral":
            logger.info("Unsupported colab_request: %s", request)
            return {"error": f"Requête Colab non supportée : {request.get('authType') or request}"}

        # drive.mount(): propagate the user's Drive credentials to the VM.
        result = await self.client.propagate_drive_credentials(endpoint, dry_run=True)
        if not result.get("success"):
            uri = result.get("unauthorized_redirect_uri")
            bus.publish("drive_auth_required", endpoint=endpoint, url=uri)
            deadline = time.time() + DRIVE_CONSENT_TIMEOUT
            while time.time() < deadline:
                await asyncio.sleep(3)
                result = await self.client.propagate_drive_credentials(endpoint, dry_run=True)
                if result.get("success"):
                    break
            else:
                return {"error": "Autorisation Google Drive non accordée à temps."}
        await self.client.propagate_drive_credentials(endpoint, dry_run=False)
        bus.publish("drive_auth_done", endpoint=endpoint)
        return None

    # -- runtime-side HTTP (files, resources) ---------------------------------

    async def _runtime_request(
        self, endpoint: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        url, token = await self.credentials(endpoint)
        if path != "/api/colab/resources":
            self.touch(endpoint)
        params = {"colab-runtime-proxy-token": token, "authuser": "0", **kwargs.pop("params", {})}
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as http:
            return await http.request(
                method,
                url.rstrip("/") + path,
                params=params,
                headers={"X-Colab-Runtime-Proxy-Token": token, "X-Colab-Client-Agent": "colab-cli"},
                **kwargs,
            )

    @staticmethod
    def _contents_path(path: str) -> str:
        return "/api/contents/" + quote(path.strip("/"), safe="/")

    async def list_files(self, endpoint: str, path: str = "content") -> list[dict[str, Any]]:
        resp = await self._runtime_request(endpoint, "GET", self._contents_path(path))
        if resp.status_code == 404:
            raise FileNotFoundError(path)
        resp.raise_for_status()
        data = resp.json()
        if data.get("type") != "directory":
            return [_file_entry(data)]
        entries = [_file_entry(e) for e in data.get("content") or []]
        return sorted(entries, key=lambda e: (e["type"] != "directory", e["name"].lower()))

    async def upload_file(self, endpoint: str, remote_path: str, content: bytes) -> dict[str, Any]:
        remote_path = remote_path.strip("/")
        payload = {
            "name": remote_path.split("/")[-1],
            "path": remote_path,
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
        resp = await self._runtime_request(endpoint, "PUT", self._contents_path(remote_path), json=payload)
        resp.raise_for_status()
        return _file_entry(resp.json())

    async def download_file(self, endpoint: str, remote_path: str) -> bytes:
        resp = await self._runtime_request(
            endpoint, "GET", self._contents_path(remote_path), params={"content": "1"}
        )
        if resp.status_code == 404:
            raise FileNotFoundError(remote_path)
        resp.raise_for_status()
        data = resp.json()
        if data.get("type") == "directory":
            raise IsADirectoryError(remote_path)
        content = data.get("content") or ""
        if data.get("format") == "base64":
            return base64.b64decode(content)
        if data.get("format") == "json":
            return json.dumps(content, indent=1).encode()
        return str(content).encode("utf-8")

    async def delete_file(self, endpoint: str, remote_path: str) -> None:
        resp = await self._runtime_request(endpoint, "DELETE", self._contents_path(remote_path))
        if resp.status_code == 404:
            raise FileNotFoundError(remote_path)
        resp.raise_for_status()

    async def resources(self, endpoint: str) -> dict[str, Any]:
        resp = await self._runtime_request(endpoint, "GET", "/api/colab/resources")
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}"}
        return resp.json()

    # -- terminal ----------------------------------------------------------------

    async def tty_url(self, endpoint: str) -> str:
        url, token = await self.credentials(endpoint)
        parsed = urlparse(url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/colab/tty?" + urlencode({"colab-runtime-proxy-token": token})

    @contextlib.asynccontextmanager
    async def terminal(self, endpoint: str) -> AsyncIterator[Any]:
        """Websocket to the runtime's tmux+bash terminal (JSON {"data"} / {"cols","rows"} frames)."""
        url = await self.tty_url(endpoint)
        _, token = await self.credentials(endpoint)
        async with websockets.connect(
            url,
            additional_headers={"X-Colab-Runtime-Proxy-Token": token, "X-Colab-Client-Agent": "colab-cli"},
            max_size=None,
            ping_interval=20,
        ) as ws:
            self._terminals[endpoint] = self._terminals.get(endpoint, 0) + 1
            self.touch(endpoint)
            bus.publish("runtimes", runtimes=self.list())
            try:
                yield ws
            finally:
                self._terminals[endpoint] -= 1
                self.touch(endpoint)
                bus.publish("runtimes", runtimes=self.list())

    async def close(self) -> None:
        for kernel in self._kernels.values():
            await kernel.close()
        self._kernels.clear()


def _file_entry(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": e.get("name"),
        "path": e.get("path"),
        "type": e.get("type"),
        "size": e.get("size"),
        "last_modified": e.get("last_modified"),
    }
