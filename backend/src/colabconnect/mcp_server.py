"""MCP server exposing ColabConnect to AI agents (Claude Code, Gemini CLI, …).

Mounted in the web app at ``/mcp`` (streamable HTTP), so the agent and the UI
share the same notebooks, runtimes and kernels, and the UI shows live what the
agent does.
"""

from __future__ import annotations

import base64
import time
from collections import deque
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.server.middleware.middleware import Middleware
from fastmcp.utilities.types import Image
from pydantic import Field

from . import settings as app_settings
from .colab_client import ACCELERATORS
from .events import bus
from .kernel import ExecutionResult
from .service import ColabService, outputs_to_text, validate_accelerator

INSTRUCTIONS = """\
ColabConnect lets you create local Jupyter notebooks and run them on Google Colab
runtimes (CPU, GPU T4/L4/A100/H100/G4, TPU v5e/v6e).

Typical workflow:
1. list_notebooks / create_notebook (choose accelerator, pip packages, env vars).
2. add_cell / update_cell to write code.
3. run_cell or run_notebook — a Colab runtime is assigned automatically on first run
   (or call connect_runtime explicitly). GPU runtimes consume Colab compute units.
4. Inspect results, use list_files / upload_file / download_file for data.
5. run_shell for bash commands (nvidia-smi, git clone…), install_packages for pip.
6. disconnect_runtime when done to release the VM (saves compute units). Idle runtimes
   are also released automatically (set_auto_stop).

The user watches the notebook live in the ColabConnect web UI.
"""


Accel = Annotated[
    str, Field(description=f"Accélérateur : {', '.join(ACCELERATORS)} (NONE = CPU)")
]


# Most recent tool calls, newest first, so the UI can show history on load.
activity_log: deque[dict[str, Any]] = deque(maxlen=100)


def _record(call_id: str, **fields: Any) -> None:
    for entry in activity_log:
        if entry["id"] == call_id:
            entry.update(fields)
            break
    else:
        activity_log.appendleft({"id": call_id, **fields})
    bus.publish("mcp_call", id=call_id, **fields)


class ActivityMiddleware(Middleware):
    """Publishes every tool call to the UI activity feed."""

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        args = context.message.arguments or {}
        started = time.time()
        call_id = f"{started:.6f}"
        _record(call_id, tool=name, arguments=_short(args), status="running")
        try:
            result = await call_next(context)
        except Exception as e:
            _record(call_id, status="error", error=str(e), duration=time.time() - started)
            raise
        _record(call_id, status="ok", duration=time.time() - started)
        return result


def _short(args: dict[str, Any], limit: int = 300) -> dict[str, Any]:
    out = {}
    for k, v in args.items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        if isinstance(s, str) and len(s) > limit:
            s = s[:limit] + "…"
        out[k] = s
    return out


def _result_content(result: ExecutionResult, header: str = "") -> list[Any]:
    text = outputs_to_text(result.outputs)
    summary = f"{header}status={result.status}"
    if result.execution_count is not None:
        summary += f" execution_count={result.execution_count}"
    content: list[Any] = [summary + ("\n" + text if text else "\n(pas de sortie)")]
    images = 0
    for o in result.outputs:
        data = o.get("data") or {}
        for mime, fmt in (("image/png", "png"), ("image/jpeg", "jpeg")):
            if mime in data and images < 4:
                raw = data[mime]
                raw = "".join(raw) if isinstance(raw, list) else raw
                content.append(Image(data=base64.b64decode(raw), format=fmt))
                images += 1
    return content


def create_mcp(service: ColabService) -> FastMCP:
    mcp = FastMCP("colabconnect", instructions=INSTRUCTIONS)
    mcp.add_middleware(ActivityMiddleware())
    nbs = service.notebooks

    # -- account --------------------------------------------------------------

    @mcp.tool
    async def colab_status() -> dict[str, Any]:
        """Google account status, compute-unit balance and active runtimes."""
        status = service.auth.status()
        result: dict[str, Any] = {"auth": {k: status[k] for k in ("authenticated", "method", "email")}}
        if status["authenticated"]:
            result["usage"] = await service.client.ccu_info()
            result["runtimes"] = await service.runtimes.refresh()
        return result

    # -- notebooks ------------------------------------------------------------

    @mcp.tool
    async def list_notebooks() -> list[dict[str, Any]]:
        """List local notebooks with their runtime settings."""
        return nbs.list()

    @mcp.tool
    async def create_notebook(
        name: str,
        accelerator: Accel = "NONE",
        high_mem: bool = False,
        pip_packages: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_code: str = "",
        cells: Annotated[
            list[dict[str, str]] | None,
            Field(description="Cellules initiales : [{'cell_type': 'code'|'markdown', 'source': '...'}]"),
        ] = None,
    ) -> dict[str, Any]:
        """Create a new local notebook. pip_packages/env/setup_code run automatically when its runtime starts."""
        notebook_id = nbs.create(
            name,
            cells=cells,
            settings={
                "accelerator": validate_accelerator(accelerator),
                "high_mem": high_mem,
                "pip_packages": pip_packages or [],
                "env": env or {},
                "setup_code": setup_code,
            },
        )
        return nbs.to_json(notebook_id)

    @mcp.tool
    async def get_notebook(
        notebook_id: str,
        include_outputs: bool = True,
    ) -> dict[str, Any]:
        """Get a notebook's settings and cells (with text outputs)."""
        data = nbs.to_json(notebook_id)
        for cell in data["cells"]:
            outputs = cell.pop("outputs")
            if include_outputs and cell["cell_type"] == "code":
                cell["output_text"] = outputs_to_text(outputs, max_chars=4000)
        data["runtime"] = (
            service.runtimes.get(ep) if (ep := service.notebook_endpoint(notebook_id)) else None
        )
        return data

    @mcp.tool
    async def configure_notebook(
        notebook_id: str,
        accelerator: Accel | None = None,
        high_mem: bool | None = None,
        pip_packages: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_code: str | None = None,
    ) -> dict[str, Any]:
        """Change a notebook's runtime settings. Accelerator changes apply on the next connect_runtime."""
        changes: dict[str, Any] = {}
        if accelerator is not None:
            changes["accelerator"] = validate_accelerator(accelerator)
        for key, value in (("high_mem", high_mem), ("pip_packages", pip_packages),
                           ("env", env), ("setup_code", setup_code)):
            if value is not None:
                changes[key] = value
        return nbs.update_settings(notebook_id, **changes)

    @mcp.tool
    async def delete_notebook(notebook_id: str) -> str:
        """Delete a local notebook file (stops its runtime first if one is attached)."""
        if service.notebook_endpoint(notebook_id):
            await service.disconnect(notebook_id, stop_runtime=True)
        nbs.delete(notebook_id)
        return f"Notebook {notebook_id} supprimé."

    # -- cells ------------------------------------------------------------------

    @mcp.tool
    async def add_cell(
        notebook_id: str,
        source: str,
        cell_type: Literal["code", "markdown"] = "code",
        index: Annotated[int | None, Field(description="Position (0 = début). Omis = à la fin.")] = None,
    ) -> dict[str, str]:
        """Insert a cell. Returns its cell_id."""
        cell_id = nbs.add_cell(notebook_id, source, cell_type, index, origin="mcp")
        return {"cell_id": cell_id}

    @mcp.tool
    async def update_cell(
        notebook_id: str,
        cell_id: str,
        source: str | None = None,
        cell_type: Literal["code", "markdown"] | None = None,
    ) -> str:
        """Replace a cell's source and/or change its type."""
        nbs.update_cell(notebook_id, cell_id, source, cell_type, origin="mcp")
        return "ok"

    @mcp.tool
    async def delete_cell(notebook_id: str, cell_id: str) -> str:
        """Delete a cell."""
        nbs.delete_cell(notebook_id, cell_id, origin="mcp")
        return "ok"

    @mcp.tool
    async def move_cell(notebook_id: str, cell_id: str, new_index: int) -> str:
        """Move a cell to a new position."""
        nbs.move_cell(notebook_id, cell_id, new_index, origin="mcp")
        return "ok"

    # -- execution ---------------------------------------------------------------

    @mcp.tool
    async def run_cell(
        notebook_id: str,
        cell_id: str,
        timeout: Annotated[float | None, Field(description="Secondes (défaut 3600)")] = None,
    ) -> list[Any]:
        """Execute one cell on the notebook's Colab runtime (assigned automatically if needed). Outputs are saved in the notebook."""
        result = await service.run_cell(notebook_id, cell_id, timeout)
        return _result_content(result, f"cell {cell_id}: ")

    @mcp.tool
    async def run_notebook(
        notebook_id: str,
        stop_on_error: bool = True,
        from_cell_id: str | None = None,
        timeout_per_cell: float | None = None,
    ) -> list[Any]:
        """Execute all code cells in order (optionally starting from a given cell)."""
        results = await service.run_all(notebook_id, stop_on_error, timeout_per_cell, from_cell_id)
        content: list[Any] = []
        for cell_id, result in results:
            content.extend(_result_content(result, f"--- cell {cell_id}: "))
        return content or ["Aucune cellule de code."]

    @mcp.tool
    async def run_code(
        notebook_id: str,
        code: str,
        timeout: float | None = None,
    ) -> list[Any]:
        """Run scratch code on the notebook's runtime WITHOUT adding it to the notebook (inspection, shell with !cmd, etc.)."""
        result = await service.run_code(notebook_id, code, timeout)
        return _result_content(result)

    @mcp.tool
    async def run_shell(
        notebook_id: str,
        command: str,
        timeout: float | None = None,
    ) -> list[Any]:
        """Run a shell command on the notebook's runtime (bash; e.g. 'nvidia-smi', 'ls -la /content'). Output ends with the exit code."""
        result = await service.run_shell(notebook_id, command, timeout)
        return _result_content(result)

    @mcp.tool
    async def install_packages(
        notebook_id: str,
        packages: list[str],
        persist: Annotated[bool, Field(description="Ajouter aussi aux paquets pip du notebook (réinstallés à chaque démarrage)")] = True,
    ) -> list[Any]:
        """pip-install packages on the runtime now."""
        result = await service.install_packages(notebook_id, packages, persist)
        return _result_content(result)

    @mcp.tool
    async def set_auto_stop(minutes: Annotated[int, Field(description="0 = désactivé")]) -> dict[str, Any]:
        """Release runtimes automatically after this many idle minutes (saves Colab compute units)."""
        app_settings.update(auto_stop_minutes=minutes)
        return app_settings.public()

    @mcp.tool
    async def interrupt_kernel(notebook_id: str) -> str:
        """Interrupt the currently running cell."""
        await service.interrupt(notebook_id)
        return "Interruption envoyée."

    @mcp.tool
    async def restart_kernel(notebook_id: str) -> str:
        """Restart the Python kernel (clears variables) and re-run the notebook setup."""
        await service.restart(notebook_id)
        return "Kernel redémarré."

    # -- runtimes ------------------------------------------------------------------

    @mcp.tool
    async def connect_runtime(notebook_id: str) -> dict[str, Any]:
        """Assign a Colab runtime for the notebook per its settings and run its setup (pip, env, setup_code)."""
        return await service.connect(notebook_id)

    @mcp.tool
    async def disconnect_runtime(notebook_id: str, stop_runtime: bool = True) -> str:
        """Detach the notebook from its runtime; by default also releases the Colab VM."""
        await service.disconnect(notebook_id, stop_runtime)
        return "Runtime libéré." if stop_runtime else "Notebook détaché (runtime conservé)."

    @mcp.tool
    async def list_runtimes() -> list[dict[str, Any]]:
        """List the Colab runtimes currently assigned to the account."""
        return await service.runtimes.refresh()

    @mcp.tool
    async def stop_runtime(endpoint: str) -> str:
        """Release a Colab runtime by endpoint (see list_runtimes)."""
        await service.stop_runtime(endpoint)
        return f"Runtime {endpoint} arrêté."

    @mcp.tool
    async def runtime_resources(notebook_id: str) -> dict[str, Any]:
        """CPU / RAM / GPU / disk usage of the notebook's runtime."""
        endpoint = service.notebook_endpoint(notebook_id)
        if not endpoint:
            return {"error": "Aucun runtime connecté."}
        return await service.runtimes.resources(endpoint)

    # -- files -------------------------------------------------------------------------

    def _endpoint(notebook_id: str) -> str:
        endpoint = service.notebook_endpoint(notebook_id)
        if not endpoint:
            raise ValueError("Aucun runtime connecté : appelez connect_runtime d'abord.")
        return endpoint

    @mcp.tool
    async def list_files(notebook_id: str, path: str = "content") -> list[dict[str, Any]]:
        """List files on the runtime (path relative to '/', default 'content' = /content)."""
        return await service.runtimes.list_files(_endpoint(notebook_id), path)

    @mcp.tool
    async def upload_file(notebook_id: str, local_path: str, remote_path: str) -> dict[str, Any]:
        """Upload a file from this machine to the runtime (e.g. remote_path='content/data.csv')."""
        data = Path(local_path).expanduser().read_bytes()
        return await service.runtimes.upload_file(_endpoint(notebook_id), remote_path, data)

    @mcp.tool
    async def download_file(notebook_id: str, remote_path: str, local_path: str) -> str:
        """Download a file from the runtime to this machine."""
        data = await service.runtimes.download_file(_endpoint(notebook_id), remote_path)
        dest = Path(local_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return f"{len(data)} octets écrits dans {dest}"

    @mcp.tool
    async def colab_browser_url(notebook_id: str) -> str:
        """URL that opens the Colab web UI attached to this notebook's runtime."""
        return service.client.browser_url(_endpoint(notebook_id))

    return mcp
