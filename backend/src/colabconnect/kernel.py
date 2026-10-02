"""Minimal async Jupyter kernel client for Colab runtimes.

Talks to the Jupyter server behind ``runtimeProxyInfo.url`` using the default
(JSON text) websocket subprotocol, and turns iopub messages into nbformat
outputs, streamed through a callback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urlparse

import httpx
import websockets

logger = logging.getLogger(__name__)

OutputCallback = Callable[[list[dict[str, Any]]], Any]
ColabRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
# Returns (proxy_url, proxy_token), refreshing the token if needed.
CredentialsProvider = Callable[[], Awaitable[tuple[str, str]]]


class KernelError(Exception):
    pass


@dataclass
class ExecutionResult:
    status: str  # "ok" | "error" | "aborted" | "timeout"
    execution_count: int | None
    outputs: list[dict[str, Any]] = field(default_factory=list)


class _OutputCollector:
    """Accumulates iopub messages into an nbformat output list."""

    def __init__(self) -> None:
        self.outputs: list[dict[str, Any]] = []
        self._clear_pending = False
        self._display_ids: dict[str, list[int]] = {}

    def handle(self, msg_type: str, content: dict[str, Any]) -> bool:
        """Apply one iopub message. Returns True if outputs changed."""
        if msg_type == "clear_output":
            if content.get("wait"):
                self._clear_pending = True
                return False
            self.outputs.clear()
            self._display_ids.clear()
            return True

        if msg_type == "update_display_data":
            display_id = (content.get("transient") or {}).get("display_id")
            changed = False
            for idx in self._display_ids.get(display_id, []):
                if idx < len(self.outputs):
                    self.outputs[idx]["data"] = content.get("data", {})
                    self.outputs[idx]["metadata"] = content.get("metadata", {})
                    changed = True
            return changed

        if msg_type == "stream":
            out = {"output_type": "stream", "name": content["name"], "text": content["text"]}
        elif msg_type in ("display_data", "execute_result"):
            out = {
                "output_type": msg_type,
                "data": content.get("data", {}),
                "metadata": content.get("metadata", {}),
            }
            if msg_type == "execute_result":
                out["execution_count"] = content.get("execution_count")
        elif msg_type == "error":
            out = {
                "output_type": "error",
                "ename": content.get("ename", ""),
                "evalue": content.get("evalue", ""),
                "traceback": content.get("traceback", []),
            }
        else:
            return False

        if self._clear_pending:
            self.outputs.clear()
            self._display_ids.clear()
            self._clear_pending = False

        last = self.outputs[-1] if self.outputs else None
        if (
            out["output_type"] == "stream"
            and last is not None
            and last["output_type"] == "stream"
            and last["name"] == out["name"]
        ):
            last["text"] += out["text"]
        else:
            self.outputs.append(out)
            display_id = (content.get("transient") or {}).get("display_id")
            if display_id:
                self._display_ids.setdefault(display_id, []).append(len(self.outputs) - 1)
        return True


class KernelSession:
    def __init__(
        self,
        credentials: CredentialsProvider,
        kernel_id: str | None = None,
        on_kernel_id: Callable[[str], None] | None = None,
        colab_request_handler: ColabRequestHandler | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> None:
        self._on_activity = on_activity or (lambda: None)
        self._credentials = credentials
        self.kernel_id = kernel_id
        self._on_kernel_id = on_kernel_id
        self._colab_request_handler = colab_request_handler
        self.session_id = uuid.uuid4().hex
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._waiters: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._connect_lock = asyncio.Lock()
        self._exec_lock = asyncio.Lock()
        self.execution_state = "unknown"

    # -- HTTP helpers --------------------------------------------------------

    async def _http(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url, token = await self._credentials()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method,
                url.rstrip("/") + path,
                params={"colab-runtime-proxy-token": token, "authuser": "0"},
                headers={
                    "X-Colab-Runtime-Proxy-Token": token,
                    "X-Colab-Client-Agent": "colab-cli",
                },
                **kwargs,
            )
        return resp

    async def _kernel_alive(self) -> bool:
        if not self.kernel_id:
            return False
        resp = await self._http("GET", f"/api/kernels/{self.kernel_id}")
        return resp.status_code == 200

    async def _create_kernel(self) -> None:
        resp = await self._http("POST", "/api/kernels", json={"name": "python3"})
        if resp.status_code >= 400:
            raise KernelError(f"Création du kernel impossible : HTTP {resp.status_code} {resp.text[:300]}")
        self.kernel_id = resp.json()["id"]
        if self._on_kernel_id:
            self._on_kernel_id(self.kernel_id)

    # -- websocket -----------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._exec_lock.locked()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            if not await self._kernel_alive():
                await self._create_kernel()
            url, token = await self._credentials()
            parsed = urlparse(url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            query = urlencode({"session_id": self.session_id, "colab-runtime-proxy-token": token})
            ws_url = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/api/kernels/{self.kernel_id}/channels?{query}"
            self._ws = await websockets.connect(
                ws_url,
                additional_headers={
                    "X-Colab-Runtime-Proxy-Token": token,
                    "X-Colab-Client-Agent": "colab-cli",
                },
                origin=f"{parsed.scheme}://{parsed.netloc}",
                max_size=None,
                ping_interval=20,
                ping_timeout=60,
            )
            self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue  # binary frames carry widget buffers; ignored
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception:
            logger.exception("Kernel websocket reader crashed")
        finally:
            self._ws = None
            for q in self._waiters.values():
                q.put_nowait({"__closed__": True})

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("msg_type") or msg.get("header", {}).get("msg_type")
        if msg_type == "status" and not msg.get("parent_header"):
            self.execution_state = msg.get("content", {}).get("execution_state", "unknown")
        if msg_type == "colab_request":
            asyncio.create_task(self._handle_colab_request(msg))
            return
        parent_id = (msg.get("parent_header") or {}).get("msg_id")
        if msg_type == "status":
            self.execution_state = msg.get("content", {}).get("execution_state", self.execution_state)
        q = self._waiters.get(parent_id) if parent_id else None
        if q is not None:
            q.put_nowait(msg)

    async def _handle_colab_request(self, msg: dict[str, Any]) -> None:
        colab_msg_id = (msg.get("metadata") or {}).get("colab_msg_id")
        reply_value: dict[str, Any] = {"type": "colab_reply", "colab_msg_id": colab_msg_id}
        try:
            if self._colab_request_handler:
                extra = await self._colab_request_handler(msg)
                if extra:
                    reply_value.update(extra)
            else:
                reply_value["error"] = "unsupported"
        except Exception as e:
            logger.warning("colab_request handler failed: %s", e)
            reply_value["error"] = str(e)
        await self._send("stdin", "input_reply", {"value": reply_value}, parent=msg.get("header"))

    async def _send(
        self,
        channel: str,
        msg_type: str,
        content: dict[str, Any],
        parent: dict[str, Any] | None = None,
    ) -> str:
        msg_id = uuid.uuid4().hex
        msg = {
            "header": {
                "msg_id": msg_id,
                "msg_type": msg_type,
                "username": "colabconnect",
                "session": self.session_id,
                "date": dt.datetime.now(dt.timezone.utc).isoformat(),
                "version": "5.3",
            },
            "parent_header": parent or {},
            "metadata": {},
            "content": content,
            "channel": channel,
            "buffers": [],
        }
        if self._ws is None:
            raise KernelError("Kernel non connecté")
        await self._ws.send(json.dumps(msg))
        return msg_id

    # -- public API ------------------------------------------------------------

    async def execute(
        self,
        code: str,
        on_output: OutputCallback | None = None,
        timeout: float | None = None,
    ) -> ExecutionResult:
        async with self._exec_lock:
            self._on_activity()
            try:
                return await self._execute_locked(code, on_output, timeout)
            finally:
                self._on_activity()

    async def _execute_locked(
        self,
        code: str,
        on_output: OutputCallback | None,
        timeout: float | None,
    ) -> ExecutionResult:
        await self.ensure_connected()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collector = _OutputCollector()
        msg_id = uuid.uuid4().hex
        # Register before sending so no reply can race past us.
        self._waiters[msg_id] = q
        try:
            msg = {
                "header": {
                    "msg_id": msg_id,
                    "msg_type": "execute_request",
                    "username": "colabconnect",
                    "session": self.session_id,
                    "date": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "version": "5.3",
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": True,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                "channel": "shell",
                "buffers": [],
            }
            await self._ws.send(json.dumps(msg))

            status = "ok"
            execution_count = None
            got_reply = False
            got_idle = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout if timeout else None
            while not (got_reply and got_idle):
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return ExecutionResult("timeout", execution_count, collector.outputs)
                try:
                    m = await asyncio.wait_for(q.get(), remaining)
                except asyncio.TimeoutError:
                    return ExecutionResult("timeout", execution_count, collector.outputs)
                if m.get("__closed__"):
                    collector.outputs.append({
                        "output_type": "error",
                        "ename": "ConnectionLost",
                        "evalue": "Connexion au kernel perdue pendant l'exécution.",
                        "traceback": [],
                    })
                    return ExecutionResult("error", execution_count, collector.outputs)
                mtype = m.get("msg_type") or m.get("header", {}).get("msg_type")
                content = m.get("content", {})
                if mtype == "execute_reply":
                    got_reply = True
                    status = content.get("status", "ok")
                    execution_count = content.get("execution_count")
                    if status == "error" and not any(
                        o["output_type"] == "error" for o in collector.outputs
                    ):
                        collector.handle("error", content)
                        if on_output:
                            await _maybe_await(on_output(collector.outputs))
                elif mtype == "status":
                    if content.get("execution_state") == "idle":
                        got_idle = True
                elif mtype == "execute_input":
                    execution_count = content.get("execution_count", execution_count)
                elif collector.handle(mtype, content) and on_output:
                    await _maybe_await(on_output(collector.outputs))
            return ExecutionResult(status, execution_count, collector.outputs)
        finally:
            self._waiters.pop(msg_id, None)

    async def interrupt(self) -> None:
        if self.kernel_id:
            await self._http("POST", f"/api/kernels/{self.kernel_id}/interrupt")

    async def restart(self) -> None:
        if self.kernel_id:
            resp = await self._http("POST", f"/api/kernels/{self.kernel_id}/restart")
            if resp.status_code >= 400:
                raise KernelError(f"Redémarrage impossible : HTTP {resp.status_code}")

    async def close(self, shutdown: bool = False) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
        self._ws = None
        self._reader = None
        if shutdown and self.kernel_id:
            try:
                await self._http("DELETE", f"/api/kernels/{self.kernel_id}")
            except Exception:
                pass


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value
