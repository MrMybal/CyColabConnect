from __future__ import annotations

import base64

import httpx
import pytest
from fastmcp import Client

from colabconnect.colab_client import ColabApiError
from colabconnect.events import bus
from colabconnect.mcp_server import create_mcp

pytestmark = pytest.mark.asyncio

PNG_1PX = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode()


async def test_run_notebook_with_setup(service, fake_colab):
    nb = service.notebooks.create(
        "e2e",
        cells=[
            {"cell_type": "markdown", "source": "# titre"},
            {"cell_type": "code", "source": "import os\nprint(os.environ['FOO'])\n21 * 2"},
            {"cell_type": "code", "source": (
                "from IPython.display import Image, display\nimport base64\n"
                f"display(Image(data=base64.b64decode('{PNG_1PX}')))"
            )},
            {"cell_type": "code", "source": "1/0"},
            {"cell_type": "code", "source": "print('never')"},
        ],
        settings={"env": {"FOO": "bar"}, "setup_code": "SETUP_DONE = True"},
    )
    try:
        with bus.subscribe() as events:
            results = await service.run_all(nb, stop_on_error=True)
        statuses = [r.status for _, r in results]
        assert statuses == ["ok", "ok", "error"]

        first = results[0][1].outputs
        assert first[0] == {"output_type": "stream", "name": "stdout", "text": "bar\n"}
        assert first[1]["output_type"] == "execute_result"
        assert first[1]["data"]["text/plain"] == "42"
        assert "image/png" in results[1][1].outputs[0]["data"]
        assert results[2][1].outputs[0]["ename"] == "ZeroDivisionError"

        # outputs persisted into the .ipynb
        saved = service.notebooks.to_json(nb)
        assert saved["cells"][1]["execution_count"] == results[0][1].execution_count
        assert saved["cells"][1]["outputs"][1]["data"]["text/plain"] == "42"

        # setup code ran, kernel state is shared with scratch execution
        scratch = await service.run_code(nb, "SETUP_DONE")
        assert scratch.outputs[0]["data"]["text/plain"] == "True"

        seen = []
        while not events.empty():
            seen.append(events.get_nowait())
        kinds = {(e["type"], e.get("status")) for e in seen}
        assert ("cell_status", "running") in kinds
        assert ("cell_status", "skipped") in kinds
        assert ("runtime_status", "ready") in kinds

        endpoint = service.notebook_endpoint(nb)
        assert endpoint in fake_colab[1]["assignments"]

        # files API
        await service.runtimes.upload_file(endpoint, "content/data.txt", b"hello")
        names = [f["name"] for f in await service.runtimes.list_files(endpoint, "content")]
        assert "data.txt" in names
        assert await service.runtimes.download_file(endpoint, "content/data.txt") == b"hello"
        await service.runtimes.delete_file(endpoint, "content/data.txt")
    finally:
        await service.disconnect(nb, stop_runtime=True)
        await service.aclose()
    assert service.notebook_endpoint(nb) is None
    assert not fake_colab[1]["assignments"]


async def test_interrupt_long_running_cell(service):
    import asyncio

    nb = service.notebooks.create("interrupt", cells=[{"cell_type": "code", "source": "import time\nfor _ in range(600):\n    time.sleep(0.1)"}])
    cell_id = service.notebooks.to_json(nb)["cells"][0]["id"]
    try:
        await service.connect(nb)
        task = asyncio.create_task(service.run_cell(nb, cell_id))
        await asyncio.sleep(2)
        await service.interrupt(nb)
        result = await asyncio.wait_for(task, 20)
        assert result.status == "error"
        assert result.outputs[-1]["ename"] == "KeyboardInterrupt"
    finally:
        await service.disconnect(nb)
        await service.aclose()


async def test_accelerator_errors(service):
    assign_calls = service.client
    with pytest.raises(ColabApiError, match="H100"):
        await assign_calls.assign("H100")
    with pytest.raises(ValueError):
        await assign_calls.assign("RTX4090")
    await service.aclose()


async def test_mcp_tools(service, fake_colab):
    mcp = create_mcp(service)
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
        assert {"create_notebook", "add_cell", "run_cell", "run_notebook", "connect_runtime",
                "list_runtimes", "upload_file", "configure_notebook"} <= tools

        created = (await client.call_tool("create_notebook", {
            "name": "from agent", "accelerator": "t4", "pip_packages": [],
        })).structured_content
        nb = created["id"]
        assert created["settings"]["accelerator"] == "T4"

        cell = (await client.call_tool("add_cell", {"notebook_id": nb, "source": "print('hi from mcp')"})).structured_content
        run = await client.call_tool("run_cell", {"notebook_id": nb, "cell_id": cell["cell_id"]})
        text = run.content[0].text
        assert "status=ok" in text and "hi from mcp" in text

        runtimes = (await client.call_tool("list_runtimes", {})).structured_content
        assert any(r["accelerator"] == "T4" and r["notebook_id"] == nb for r in runtimes["result"])

        got = (await client.call_tool("get_notebook", {"notebook_id": nb})).structured_content
        assert "hi from mcp" in got["cells"][-1]["output_text"]

        await client.call_tool("disconnect_runtime", {"notebook_id": nb})
    await service.aclose()
    assert not fake_colab[1]["assignments"]


async def test_http_rejects_non_local_hosts():
    from colabconnect.api import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8420") as http:
        assert (await http.get("/api/info")).status_code == 200
        assert (await http.get("/api/info", headers={"host": "evil.example"})).status_code == 403
        assert (await http.get("/api/info", headers={"origin": "https://evil.example"})).status_code == 403
        assert (await http.get("/api/info", headers={"origin": "http://localhost:5173"})).status_code == 200
        assert (await http.post("/mcp", headers={"origin": "https://evil.example"}, json={})).status_code == 403
