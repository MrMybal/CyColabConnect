"""High-level operations shared by the web API and the MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import websockets

from . import config
from . import settings as app_settings
from .auth import AuthManager
from .colab_client import ColabClient, normalize_accelerator
from .events import bus
from .kernel import ExecutionResult, KernelError
from .notebooks import NotebookStore
from .runtimes import RuntimeManager

logger = logging.getLogger(__name__)

STREAM_THROTTLE = 0.25
MONITOR_INTERVAL = 60
# Errors meaning the runtime VM may be gone (reclaimed by Colab, quota, …).
CONNECTION_ERRORS = (httpx.HTTPError, websockets.WebSocketException, KernelError, OSError)


class RuntimeLostError(RuntimeError):
    pass


class ColabService:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.auth = AuthManager()
        self.client = ColabClient(self.auth)
        self.runtimes = RuntimeManager(self.client)
        self.notebooks = NotebookStore()
        # notebook_id -> set of cell ids queued/running
        self._running: dict[str, str | None] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.runtimes.close()
        await self.client.aclose()

    # -- background monitoring -------------------------------------------------

    async def monitor(self, interval: float = MONITOR_INTERVAL) -> None:
        """Periodically sync runtimes with Colab and release idle ones."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.monitor_once()
            except Exception as e:
                logger.debug("Runtime monitor iteration failed: %s", e)

    async def monitor_once(self) -> list[str]:
        if not self.auth.status()["authenticated"]:
            return []
        await self.runtimes.refresh()
        self._forget_vanished_runtimes()
        minutes = app_settings.load()["auto_stop_minutes"]
        stopped: list[str] = []
        if minutes > 0:
            for endpoint in self.runtimes.idle_endpoints(minutes * 60):
                runtime = self.runtimes.get(endpoint)
                logger.info("Auto-stopping idle runtime %s", endpoint)
                await self.stop_runtime(endpoint)
                bus.publish(
                    "runtime_auto_stopped", endpoint=endpoint,
                    notebook_id=runtime.get("notebook_id"), minutes=minutes,
                    accelerator=runtime["accelerator"],
                )
                stopped.append(endpoint)
        return stopped

    def _forget_vanished_runtimes(self) -> None:
        """Clear notebooks bound to runtimes that no longer exist."""
        for nb in self.notebooks.list():
            endpoint = nb["settings"].get("runtime_endpoint")
            if endpoint and not self.runtimes.exists(endpoint):
                self.notebooks.update_settings(nb["id"], runtime_endpoint=None)

    async def stop_runtime(self, endpoint: str) -> None:
        await self.runtimes.stop(endpoint)
        self._forget_vanished_runtimes()

    # -- notebook <-> runtime --------------------------------------------------

    def notebook_endpoint(self, notebook_id: str) -> str | None:
        settings = self.notebooks.settings(self.notebooks.load(notebook_id))
        endpoint = settings.get("runtime_endpoint")
        return endpoint if self.runtimes.exists(endpoint) else None

    async def connect(self, notebook_id: str) -> dict[str, Any]:
        """Assign a runtime for the notebook (per its settings) and run its setup."""
        lock = self._connect_locks.setdefault(notebook_id, asyncio.Lock())
        async with lock:
            endpoint = self.notebook_endpoint(notebook_id)
            if endpoint is None:
                # The stored endpoint may be stale after an app restart.
                if self.notebooks.settings(self.notebooks.load(notebook_id)).get("runtime_endpoint"):
                    await self.runtimes.refresh()
                    endpoint = self.notebook_endpoint(notebook_id)
            if endpoint is not None:
                return self.runtimes.get(endpoint)

            settings = self.notebooks.settings(self.notebooks.load(notebook_id))
            runtime = await self.runtimes.create(
                settings["accelerator"], settings["high_mem"], notebook_id=notebook_id
            )
            self.notebooks.update_settings(notebook_id, runtime_endpoint=runtime["endpoint"])
            await self._run_setup(notebook_id, runtime["endpoint"], settings)
            return self.runtimes.get(runtime["endpoint"])

    async def _run_setup(self, notebook_id: str, endpoint: str, settings: dict[str, Any]) -> None:
        parts: list[str] = []
        if settings.get("env"):
            parts.append(f"import os\nos.environ.update({json.dumps(settings['env'])})")
        if settings.get("pip_packages"):
            pkgs = " ".join(json.dumps(p) for p in settings["pip_packages"])
            parts.append(f"%pip install -q {pkgs}")
        if settings.get("setup_code", "").strip():
            parts.append(settings["setup_code"])
        if not parts:
            # Still start the kernel now so the first cell runs immediately.
            await self.runtimes.kernel(endpoint).ensure_connected()
            return
        bus.publish("runtime_status", status="setup", endpoint=endpoint, notebook_id=notebook_id)
        for code in parts:
            result = await self.runtimes.kernel(endpoint).execute(code, timeout=config.DEFAULT_CELL_TIMEOUT)
            if result.status != "ok":
                bus.publish(
                    "runtime_status", status="setup_error", endpoint=endpoint,
                    notebook_id=notebook_id, error=outputs_to_text(result.outputs),
                )
                raise RuntimeError("Échec du setup du runtime :\n" + outputs_to_text(result.outputs))
        bus.publish("runtime_status", status="ready", endpoint=endpoint, notebook_id=notebook_id)

    async def disconnect(self, notebook_id: str, stop_runtime: bool = True) -> None:
        endpoint = self.notebook_endpoint(notebook_id)
        if endpoint and stop_runtime:
            await self.runtimes.stop(endpoint)
        elif endpoint:
            self.runtimes.bind_notebook(endpoint, None)
        self.notebooks.update_settings(notebook_id, runtime_endpoint=None)

    async def attach(self, notebook_id: str, endpoint: str) -> None:
        self.runtimes.get(endpoint)  # raises if unknown
        self.runtimes.bind_notebook(endpoint, notebook_id)
        self.notebooks.update_settings(notebook_id, runtime_endpoint=endpoint)

    # -- execution -------------------------------------------------------------

    def running_cell(self, notebook_id: str) -> str | None:
        return self._running.get(notebook_id)

    async def run_cell(
        self, notebook_id: str, cell_id: str, timeout: float | None = None
    ) -> ExecutionResult:
        cell = self.notebooks.get_cell(notebook_id, cell_id)
        if cell["cell_type"] != "code":
            return ExecutionResult("ok", None, [])
        source = cell["source"] if isinstance(cell["source"], str) else "".join(cell["source"])

        bus.publish("cell_status", notebook_id=notebook_id, cell_id=cell_id, status="queued")
        runtime = await self.connect(notebook_id)
        kernel = self.runtimes.kernel(runtime["endpoint"])

        last_push = 0.0

        def on_output(outputs: list[dict[str, Any]]) -> None:
            nonlocal last_push
            now = time.monotonic()
            if now - last_push >= STREAM_THROTTLE:
                last_push = now
                bus.publish("cell_output", notebook_id=notebook_id, cell_id=cell_id, outputs=outputs)

        self._running[notebook_id] = cell_id
        bus.publish("cell_status", notebook_id=notebook_id, cell_id=cell_id, status="running")
        try:
            result = await self._guarded(
                notebook_id,
                runtime["endpoint"],
                kernel.execute(source, on_output=on_output, timeout=timeout or config.DEFAULT_CELL_TIMEOUT),
            )
        except RuntimeLostError as e:
            result = ExecutionResult("error", None, [{
                "output_type": "error", "ename": "RuntimeLost", "evalue": str(e), "traceback": [],
            }])
        finally:
            self._running.pop(notebook_id, None)
        if result.status == "timeout":
            await kernel.interrupt()
            result.outputs.append({
                "output_type": "error", "ename": "Timeout",
                "evalue": f"Exécution interrompue après {timeout or config.DEFAULT_CELL_TIMEOUT:.0f}s",
                "traceback": [],
            })
        try:
            self.notebooks.set_outputs(notebook_id, cell_id, result.outputs, result.execution_count)
        except Exception:
            logger.warning("Cell %s disappeared before outputs were saved", cell_id)
        bus.publish(
            "cell_status", notebook_id=notebook_id, cell_id=cell_id, status=result.status,
            outputs=result.outputs, execution_count=result.execution_count,
        )
        return result

    async def run_all(
        self, notebook_id: str, stop_on_error: bool = True, timeout: float | None = None,
        from_cell: str | None = None,
    ) -> list[tuple[str, ExecutionResult]]:
        nb = self.notebooks.load(notebook_id)
        ids = [c["id"] for c in nb.cells if c["cell_type"] == "code"]
        if from_cell:
            ids = ids[ids.index(from_cell):] if from_cell in ids else ids
        results = []
        for cell_id in ids:
            result = await self.run_cell(notebook_id, cell_id, timeout)
            results.append((cell_id, result))
            if stop_on_error and result.status != "ok":
                for rest in ids[len(results):]:
                    bus.publish("cell_status", notebook_id=notebook_id, cell_id=rest, status="skipped")
                break
        return results

    async def _guarded(self, notebook_id: str, endpoint: str, coro: Any) -> Any:
        """Run a kernel operation; if it fails because the VM vanished, say so clearly."""
        try:
            return await coro
        except CONNECTION_ERRORS as e:
            try:
                await self.runtimes.refresh()
            except Exception:
                raise e
            if self.runtimes.exists(endpoint):
                raise
            self._forget_vanished_runtimes()
            bus.publish("runtime_status", status="lost", endpoint=endpoint, notebook_id=notebook_id)
            raise RuntimeLostError(
                "Le runtime Colab n'existe plus (arrêté par Colab : inactivité, quota ou limite de durée). "
                "Relancez la cellule pour en allouer un nouveau ; les variables en mémoire sont perdues."
            ) from e

    async def run_code(self, notebook_id: str, code: str, timeout: float | None = None) -> ExecutionResult:
        """Run code on the notebook's runtime without storing it in the notebook."""
        runtime = await self.connect(notebook_id)
        kernel = self.runtimes.kernel(runtime["endpoint"])
        return await self._guarded(
            notebook_id, runtime["endpoint"],
            kernel.execute(code, timeout=timeout or config.DEFAULT_CELL_TIMEOUT),
        )

    async def run_shell(self, notebook_id: str, command: str, timeout: float | None = None) -> ExecutionResult:
        """Run a shell command on the runtime (streams through the kernel)."""
        code = (
            "_cc_ip = get_ipython()\n"
            f"_cc_ip.system({command!r})\n"
            "print(f'[exit code {_cc_ip.user_ns.get(\"_exit_code\")}]')"
        )
        return await self.run_code(notebook_id, code, timeout)

    async def install_packages(
        self, notebook_id: str, packages: list[str], persist: bool = True
    ) -> ExecutionResult:
        """pip-install packages now; optionally add them to the notebook's setup."""
        if not packages:
            raise ValueError("Aucun paquet indiqué")
        pkgs = " ".join(json.dumps(p) for p in packages)
        result = await self.run_code(notebook_id, f"%pip install -q {pkgs}")
        if persist and result.status == "ok":
            current = self.notebooks.settings(self.notebooks.load(notebook_id))["pip_packages"]
            merged = current + [p for p in packages if p not in current]
            self.notebooks.update_settings(notebook_id, pip_packages=merged)
        return result

    async def interrupt(self, notebook_id: str) -> None:
        endpoint = self.notebook_endpoint(notebook_id)
        if endpoint:
            await self.runtimes.kernel(endpoint).interrupt()

    async def restart(self, notebook_id: str) -> None:
        endpoint = self.notebook_endpoint(notebook_id)
        if endpoint:
            await self.runtimes.kernel(endpoint).restart()
            settings = self.notebooks.settings(self.notebooks.load(notebook_id))
            await self._run_setup(notebook_id, endpoint, settings)


def outputs_to_text(outputs: list[dict[str, Any]], max_chars: int = 20000) -> str:
    """Render nbformat outputs as plain text (for MCP responses and errors)."""
    parts: list[str] = []
    for o in outputs:
        kind = o.get("output_type")
        if kind == "stream":
            parts.append(o.get("text", ""))
        elif kind in ("execute_result", "display_data"):
            data = o.get("data", {})
            if "text/plain" in data:
                text = data["text/plain"]
                parts.append("".join(text) if isinstance(text, list) else text)
            elif "image/png" in data or "image/jpeg" in data:
                parts.append("[image]")
            elif "text/html" in data:
                parts.append("[html]")
        elif kind == "error":
            tb = "\n".join(o.get("traceback", []))
            parts.append(_strip_ansi(tb) or f"{o.get('ename')}: {o.get('evalue')}")
    text = "\n".join(p.rstrip("\n") for p in parts if p)
    if len(text) > max_chars:
        text = text[: max_chars // 2] + "\n…[tronqué]…\n" + text[-max_chars // 2:]
    return text


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def validate_accelerator(value: str | None) -> str:
    return normalize_accelerator(value)
