"""FastAPI app: REST API for the web UI, live events websocket, MCP endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, config
from . import settings as app_settings
from .auth import NotAuthenticatedError
from .colab_client import ACCELERATORS, ColabApiError
from .events import bus
from .kernel import KernelError
from .mcp_server import activity_log, create_mcp
from .notebooks import CellNotFound, NotebookNotFound
from .runtimes import RuntimeNotFound
from .service import ColabService, validate_accelerator
from .updater import UpdateError, install_mode, updater

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local(host_header: str | None, origin: str | None) -> bool:
    host = urlsplit(f"//{host_header or ''}").hostname
    if host not in ALLOWED_HOSTS:
        return False
    return not origin or urlsplit(origin).hostname in ALLOWED_HOSTS
FRONTEND_DIST = Path(
    __import__("os").environ.get(
        "COLABCONNECT_FRONTEND", Path(__file__).resolve().parents[3] / "frontend" / "dist"
    )
)


# -- request bodies -----------------------------------------------------------


class LoginFinish(BaseModel):
    code: str


class NewRuntime(BaseModel):
    accelerator: str = "NONE"
    high_mem: bool = False


class NewNotebook(BaseModel):
    name: str
    accelerator: str = "NONE"
    high_mem: bool = False


class Rename(BaseModel):
    name: str


class Settings(BaseModel):
    accelerator: str | None = None
    high_mem: bool | None = None
    pip_packages: list[str] | None = None
    env: dict[str, str] | None = None
    setup_code: str | None = None


class NewCell(BaseModel):
    source: str = ""
    cell_type: str = "code"
    index: int | None = None


class CellPatch(BaseModel):
    source: str | None = None
    cell_type: str | None = None


class Move(BaseModel):
    index: int


class RunAll(BaseModel):
    stop_on_error: bool = True
    from_cell_id: str | None = None


class Disconnect(BaseModel):
    stop_runtime: bool = True


class Attach(BaseModel):
    endpoint: str


class AppSettings(BaseModel):
    auto_stop_minutes: int | None = None
    github_token: str | None = None


# -- app ------------------------------------------------------------------------


def create_app() -> FastAPI:
    service = ColabService()
    mcp = create_mcp(service)
    mcp_app = mcp.http_app(path="/mcp", host_origin_protection=False)
    tasks: set[asyncio.Task[Any]] = set()

    async def _check_updates_periodically():
        while True:
            with contextlib.suppress(Exception):
                await updater.check()
            await asyncio.sleep(3600)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):
            if service.auth.status()["authenticated"]:
                with contextlib.suppress(Exception):
                    await service.runtimes.refresh()
            monitor = asyncio.create_task(service.monitor())
            update_checker = asyncio.create_task(_check_updates_periodically())
            yield
            monitor.cancel()
            update_checker.cancel()
            await service.aclose()

    app = FastAPI(title="ColabConnect", version=__version__, lifespan=lifespan)
    app.state.service = service

    # -- errors -------------------------------------------------------------

    def _err(status: int, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    app.add_exception_handler(NotAuthenticatedError, lambda r, e: _err(401, e))
    for exc_type in (NotebookNotFound, CellNotFound, RuntimeNotFound, FileNotFoundError):
        app.add_exception_handler(exc_type, lambda r, e: _err(404, e))
    app.add_exception_handler(ValueError, lambda r, e: _err(400, e))
    app.add_exception_handler(ColabApiError, lambda r, e: _err(502, e))
    app.add_exception_handler(KernelError, lambda r, e: _err(502, e))
    app.add_exception_handler(UpdateError, lambda r, e: _err(409, e))

    def background(coro: Any, *, notebook_id: str | None = None) -> None:
        async def runner():
            try:
                await coro
            except Exception as e:
                logger.exception("Background task failed")
                bus.publish("error", notebook_id=notebook_id, message=str(e))

        task = asyncio.create_task(runner())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    # -- info & auth ------------------------------------------------------------

    @app.get("/api/info")
    async def info(request: Request):
        base = f"http://{config.HOST}:{config.PORT}"
        return {
            "version": __version__,
            "mcp_url": f"{base}/mcp",
            "notebooks_dir": str(service.notebooks.root),
            "backend_dir": str(Path(__file__).resolve().parents[2]),
            "install_mode": install_mode(),
            "accelerators": ACCELERATORS,
        }

    @app.get("/api/activity")
    async def activity():
        return list(activity_log)

    @app.get("/api/auth/status")
    async def auth_status():
        return service.auth.status()

    @app.post("/api/auth/login/start")
    async def login_start():
        return {"url": service.auth.start_login()}

    @app.post("/api/auth/login/finish")
    async def login_finish(body: LoginFinish):
        status = await service.auth.finish_login(body.code)
        background(service.runtimes.refresh())
        return status

    @app.post("/api/auth/adc")
    async def login_adc():
        status = await service.auth.use_adc()
        if not status.get("email"):
            with contextlib.suppress(Exception):
                service.auth._email = await service.client.user_email()
                service.auth._save()
        return service.auth.status()

    @app.post("/api/auth/logout")
    async def logout():
        service.auth.logout()
        return service.auth.status()

    @app.get("/api/usage")
    async def usage():
        return await service.client.ccu_info()

    # -- runtimes ---------------------------------------------------------------

    @app.get("/api/runtimes")
    async def list_runtimes(refresh: bool = True):
        return await service.runtimes.refresh() if refresh else service.runtimes.list()

    @app.post("/api/runtimes")
    async def create_runtime(body: NewRuntime):
        return await service.runtimes.create(validate_accelerator(body.accelerator), body.high_mem)

    @app.delete("/api/runtimes/{endpoint}")
    async def stop_runtime(endpoint: str):
        await service.stop_runtime(endpoint)
        return {"ok": True}

    @app.websocket("/api/runtimes/{endpoint}/terminal")
    async def terminal(ws: WebSocket, endpoint: str):
        """Bridges the browser terminal to the runtime's /colab/tty websocket."""
        await ws.accept()
        try:
            async with service.runtimes.terminal(endpoint) as remote:

                async def upstream():
                    while True:
                        await remote.send(await ws.receive_text())

                async def downstream():
                    async for message in remote:
                        await ws.send_text(message if isinstance(message, str) else message.decode())

                tasks_ = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
                done, pending = await asyncio.wait(tasks_, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except WebSocketDisconnect:
            return
        except Exception as e:
            with contextlib.suppress(Exception):
                await ws.send_json({"data": f"\r\n\x1b[31m[ColabConnect] Terminal indisponible : {e}\x1b[0m\r\n"})
        with contextlib.suppress(Exception):
            await ws.close()

    # -- app settings ---------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings():
        return app_settings.public()

    @app.put("/api/settings")
    async def put_settings(body: AppSettings):
        app_settings.update(**body.model_dump(exclude_none=True))
        return app_settings.public()

    # -- self-update ----------------------------------------------------------------

    @app.get("/api/update")
    async def update_status():
        return updater.status()

    @app.post("/api/update/check")
    async def update_check():
        return await updater.check(force=True)

    @app.post("/api/update/install")
    async def update_install():
        result = await updater.install()
        restart = getattr(app.state, "request_restart", None)
        if restart is None:
            result["restart"] = "manual"
        else:
            asyncio.get_running_loop().call_later(1.0, restart)
            result["restart"] = "auto"
        return result

    @app.get("/api/runtimes/{endpoint}/resources")
    async def runtime_resources(endpoint: str):
        return await service.runtimes.resources(endpoint)

    @app.get("/api/runtimes/{endpoint}/files")
    async def list_files(endpoint: str, path: str = "content"):
        return await service.runtimes.list_files(endpoint, path)

    @app.post("/api/runtimes/{endpoint}/files")
    async def upload_file(endpoint: str, file: UploadFile = File(...), directory: str = Form("content")):
        remote = f"{directory.strip('/')}/{file.filename}"
        return await service.runtimes.upload_file(endpoint, remote, await file.read())

    @app.get("/api/runtimes/{endpoint}/files/download")
    async def download_file(endpoint: str, path: str):
        data = await service.runtimes.download_file(endpoint, path)
        name = path.rstrip("/").split("/")[-1]
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.delete("/api/runtimes/{endpoint}/files")
    async def delete_file(endpoint: str, path: str):
        await service.runtimes.delete_file(endpoint, path)
        return {"ok": True}

    # -- notebooks ----------------------------------------------------------------

    nbs = service.notebooks

    def notebook_payload(notebook_id: str) -> dict[str, Any]:
        data = nbs.to_json(notebook_id)
        endpoint = service.notebook_endpoint(notebook_id)
        data["runtime"] = service.runtimes.get(endpoint) if endpoint else None
        data["running_cell"] = service.running_cell(notebook_id)
        return data

    @app.get("/api/notebooks")
    async def list_notebooks():
        return nbs.list()

    @app.post("/api/notebooks")
    async def create_notebook(body: NewNotebook):
        notebook_id = nbs.create(
            body.name,
            settings={"accelerator": validate_accelerator(body.accelerator), "high_mem": body.high_mem},
        )
        return notebook_payload(notebook_id)

    @app.post("/api/notebooks/import")
    async def import_notebook(file: UploadFile = File(...)):
        name = (file.filename or "notebook").removesuffix(".ipynb")
        return notebook_payload(nbs.import_notebook(name, await file.read()))

    @app.get("/api/notebooks/{notebook_id}")
    async def get_notebook(notebook_id: str):
        return notebook_payload(notebook_id)

    @app.get("/api/notebooks/{notebook_id}/download")
    async def download_notebook(notebook_id: str):
        return Response(
            nbs.raw(notebook_id),
            media_type="application/x-ipynb+json",
            headers={"Content-Disposition": f'attachment; filename="{notebook_id}.ipynb"'},
        )

    @app.delete("/api/notebooks/{notebook_id}")
    async def delete_notebook(notebook_id: str):
        if service.notebook_endpoint(notebook_id):
            await service.disconnect(notebook_id, stop_runtime=True)
        nbs.delete(notebook_id)
        return {"ok": True}

    @app.post("/api/notebooks/{notebook_id}/rename")
    async def rename_notebook(notebook_id: str, body: Rename):
        endpoint = service.notebook_endpoint(notebook_id)
        new_id = nbs.rename(notebook_id, body.name)
        if endpoint:
            service.runtimes.bind_notebook(endpoint, new_id)
        return notebook_payload(new_id)

    @app.put("/api/notebooks/{notebook_id}/settings")
    async def update_settings(notebook_id: str, body: Settings):
        changes = body.model_dump(exclude_none=True)
        if "accelerator" in changes:
            changes["accelerator"] = validate_accelerator(changes["accelerator"])
        return nbs.update_settings(notebook_id, **changes)

    @app.post("/api/notebooks/{notebook_id}/cells")
    async def add_cell(notebook_id: str, body: NewCell):
        return {"cell_id": nbs.add_cell(notebook_id, body.source, body.cell_type, body.index)}

    @app.patch("/api/notebooks/{notebook_id}/cells/{cell_id}")
    async def patch_cell(notebook_id: str, cell_id: str, body: CellPatch):
        nbs.update_cell(notebook_id, cell_id, body.source, body.cell_type)
        return {"ok": True}

    @app.delete("/api/notebooks/{notebook_id}/cells/{cell_id}")
    async def delete_cell(notebook_id: str, cell_id: str):
        nbs.delete_cell(notebook_id, cell_id)
        return {"ok": True}

    @app.post("/api/notebooks/{notebook_id}/cells/{cell_id}/move")
    async def move_cell(notebook_id: str, cell_id: str, body: Move):
        nbs.move_cell(notebook_id, cell_id, body.index)
        return {"ok": True}

    # -- execution -----------------------------------------------------------------

    @app.post("/api/notebooks/{notebook_id}/cells/{cell_id}/run", status_code=202)
    async def run_cell(notebook_id: str, cell_id: str):
        nbs.get_cell(notebook_id, cell_id)
        background(service.run_cell(notebook_id, cell_id), notebook_id=notebook_id)
        return {"accepted": True}

    @app.post("/api/notebooks/{notebook_id}/run-all", status_code=202)
    async def run_all(notebook_id: str, body: RunAll):
        nbs.load(notebook_id)
        background(
            service.run_all(notebook_id, body.stop_on_error, None, body.from_cell_id),
            notebook_id=notebook_id,
        )
        return {"accepted": True}

    @app.post("/api/notebooks/{notebook_id}/connect", status_code=202)
    async def connect(notebook_id: str):
        nbs.load(notebook_id)
        background(service.connect(notebook_id), notebook_id=notebook_id)
        return {"accepted": True}

    @app.post("/api/notebooks/{notebook_id}/attach")
    async def attach(notebook_id: str, body: Attach):
        await service.attach(notebook_id, body.endpoint)
        return notebook_payload(notebook_id)

    @app.post("/api/notebooks/{notebook_id}/disconnect")
    async def disconnect(notebook_id: str, body: Disconnect):
        await service.disconnect(notebook_id, body.stop_runtime)
        return notebook_payload(notebook_id)

    @app.post("/api/notebooks/{notebook_id}/interrupt")
    async def interrupt(notebook_id: str):
        await service.interrupt(notebook_id)
        return {"ok": True}

    @app.post("/api/notebooks/{notebook_id}/restart", status_code=202)
    async def restart(notebook_id: str):
        background(service.restart(notebook_id), notebook_id=notebook_id)
        return {"accepted": True}

    @app.post("/api/notebooks/{notebook_id}/clear-outputs")
    async def clear_outputs(notebook_id: str):
        nbs.clear_outputs(notebook_id)
        return {"ok": True}

    # -- live events -------------------------------------------------------------------

    @app.websocket("/api/events")
    async def events(ws: WebSocket):
        await ws.accept()
        with bus.subscribe() as queue:
            try:
                while True:
                    event = await queue.get()
                    await ws.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                pass

    # -- MCP + frontend -------------------------------------------------------------------

    if FRONTEND_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = (FRONTEND_DIST / path).resolve()
            if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return LocalDispatcher(app, mcp_app)


class LocalDispatcher:
    """Top-level ASGI app: routes /mcp to the MCP server, the rest to FastAPI.

    Also rejects non-local Host/Origin headers (DNS rebinding, cross-site
    requests): this server can run arbitrary code on Colab with the user's
    Google credentials.
    """

    def __init__(self, api: FastAPI, mcp_app: Any) -> None:
        self.api = api
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            if not _is_local(headers.get("host"), headers.get("origin")):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Requête non locale refusée"}, status_code=403)(
                        scope, receive, send
                    )
                return
            path = scope["path"]
            if path == "/mcp" or path.startswith("/mcp/"):
                await self.mcp_app(scope, receive, send)
                return
        await self.api(scope, receive, send)
