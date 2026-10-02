from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets
from starlette.testclient import TestClient

from colabconnect import settings as app_settings
from colabconnect.events import bus

pytestmark = pytest.mark.asyncio


async def test_run_shell_and_install_packages(service):
    nb = service.notebooks.create("shell")
    try:
        result = await service.run_shell(nb, "echo bonjour-shell")
        text = "".join(o.get("text", "") for o in result.outputs)
        assert "bonjour-shell" in text
        assert "[exit code 0]" in text

        # Already installed locally: exercises %pip without downloading anything.
        result = await service.install_packages(nb, ["nbformat"])
        assert result.status == "ok"
        assert service.notebooks.settings(service.notebooks.load(nb))["pip_packages"] == ["nbformat"]
    finally:
        await service.disconnect(nb)
        await service.aclose()


async def test_auto_stop_idle_runtimes(service, fake_colab):
    app_settings.update(auto_stop_minutes=30)
    idle_nb = service.notebooks.create("idle")
    busy_nb = service.notebooks.create("busy", cells=[{"cell_type": "code", "source": "import time\nfor _ in range(40): time.sleep(0.1)"}])
    try:
        idle = (await service.connect(idle_nb))["endpoint"]
        busy = (await service.connect(busy_nb))["endpoint"]
        busy_cell = service.notebooks.to_json(busy_nb)["cells"][0]["id"]
        running = asyncio.create_task(service.run_cell(busy_nb, busy_cell))
        await asyncio.sleep(0.5)

        # Pretend both have been idle for an hour; only the idle one must stop.
        service.runtimes._last_activity[idle] = time.time() - 3600
        service.runtimes._last_activity[busy] = time.time() - 3600
        with bus.subscribe() as events:
            stopped = await service.monitor_once()
        assert stopped == [idle]
        assert idle not in fake_colab[1]["assignments"]
        assert service.notebook_endpoint(idle_nb) is None
        assert service.notebook_endpoint(busy_nb) == busy
        seen = []
        while not events.empty():
            seen.append(events.get_nowait())
        auto = [e for e in seen if e["type"] == "runtime_auto_stopped"]
        assert [(e["endpoint"], e["notebook_id"]) for e in auto] == [(idle, idle_nb)]

        await running
        app_settings.update(auto_stop_minutes=0)
        service.runtimes._last_activity[busy] = time.time() - 3600
        assert await service.monitor_once() == []
    finally:
        app_settings.update(auto_stop_minutes=60)
        await service.disconnect(busy_nb)
        await service.aclose()


async def test_runtime_reclaimed_by_colab(service, fake_colab):
    nb = service.notebooks.create("lost", cells=[{"cell_type": "code", "source": "1 + 1"}])
    cell = service.notebooks.to_json(nb)["cells"][0]["id"]
    try:
        endpoint = (await service.connect(nb))["endpoint"]
        assert (await service.run_cell(nb, cell)).status == "ok"

        # Colab reclaims the VM: assignment disappears and the proxy stops answering.
        fake_colab[1]["assignments"].pop(endpoint)
        await service.runtimes.kernel(endpoint).close()
        service.runtimes._records[endpoint]["proxy_url"] = "http://127.0.0.1:9"

        result = await service.run_cell(nb, cell)
        assert result.status == "error"
        assert result.outputs[0]["ename"] == "RuntimeLost"
        assert service.notebook_endpoint(nb) is None

        # Next run transparently allocates a fresh runtime.
        assert (await service.run_cell(nb, cell)).status == "ok"
        assert service.notebook_endpoint(nb) not in (None, endpoint)
    finally:
        await service.disconnect(nb)
        await service.aclose()


async def test_terminal_bridge(fake_colab, monkeypatch):
    """Browser <-> /api/runtimes/{ep}/terminal <-> fake /colab/tty."""
    import threading

    import httpx
    import uvicorn

    from colabconnect.api import create_app
    from conftest import FakeCreds, _free_port, _wait_http

    received: list[dict] = []

    async def fake_tty(ws):
        await ws.send(json.dumps({"data": "$ "}))
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            if "data" in msg:
                await ws.send(json.dumps({"data": msg["data"].upper()}))

    async with websockets.serve(fake_tty, "127.0.0.1", 0) as tty_server:
        tty_port = tty_server.sockets[0].getsockname()[1]

        app = create_app()
        service = app.api.state.service
        service.client.domain = fake_colab[0]
        service.auth._creds = FakeCreds()
        service.auth._method = "oauth"

        async def tty_url(_endpoint):
            return f"ws://127.0.0.1:{tty_port}/colab/tty"

        monkeypatch.setattr(service.runtimes, "tty_url", tty_url)

        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        await asyncio.to_thread(_wait_http, base + "/api/info")
        try:
            async with httpx.AsyncClient(base_url=base, timeout=30) as http:
                endpoint = (await http.post("/api/runtimes", json={"accelerator": "NONE"})).json()["endpoint"]
                async with websockets.connect(f"ws://127.0.0.1:{port}/api/runtimes/{endpoint}/terminal") as ws:
                    assert json.loads(await asyncio.wait_for(ws.recv(), 10)) == {"data": "$ "}
                    await ws.send(json.dumps({"cols": 100, "rows": 30}))
                    await ws.send(json.dumps({"data": "ls"}))
                    assert json.loads(await asyncio.wait_for(ws.recv(), 10)) == {"data": "LS"}
                    runtime = next(r for r in (await http.get("/api/runtimes?refresh=false")).json()
                                   if r["endpoint"] == endpoint)
                    assert runtime["terminals"] == 1 and runtime["busy"]
                assert {"cols": 100, "rows": 30} in received

                # Foreign origins cannot open a terminal.
                with pytest.raises(websockets.InvalidStatus):
                    async with websockets.connect(
                        f"ws://127.0.0.1:{port}/api/runtimes/{endpoint}/terminal",
                        origin="https://evil.example",
                    ):
                        pass
                await http.delete(f"/api/runtimes/{endpoint}")
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)
