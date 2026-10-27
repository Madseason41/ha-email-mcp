"""Fixtures for Email MCP Bridge tests (pytest-homeassistant-custom-component)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from aiohttp import web

pytest_plugins = ["pytest_homeassistant_custom_component"]

FAKE_TOKEN = "fake-bearer-token-for-tests-0123456789abcdef"  # not a secret


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@dataclass
class FakeUpstream:
    url: str = ""
    seen: list = field(default_factory=list)
    mode: str = "json"  # json | sse | html | status202


@pytest.fixture
async def fake_upstream(socket_enabled):
    """A stand-in for the add-on's auth proxy + MCP server on 127.0.0.1."""
    state = FakeUpstream()

    async def mcp(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        state.seen.append({"method": request.method, "headers": dict(request.headers), "body": body})
        if request.headers.get("Authorization") != f"Bearer {FAKE_TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if state.mode == "status202":
            return web.Response(status=202)
        if state.mode == "html":
            return web.Response(text="<script>alert(1)</script>", content_type="text/html")
        if state.mode == "sse":
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "sess-1"})
            await resp.prepare(request)
            await resp.write(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
            await resp.write_eof()
            return resp
        msg = json.loads(body or b"{}")
        if msg.get("method") == "tools/list":
            result = {"tools": [{"name": "list_emails_metadata"}]}
        else:
            result = {"echo": msg.get("method"), "http_method": request.method}
        return web.json_response(
            {"jsonrpc": "2.0", "id": msg.get("id"), "result": result},
            headers={"Mcp-Session-Id": "sess-1", "X-Internal": "drop-me"},
        )

    app = web.Application()
    app.router.add_route("*", "/mcp", mcp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state.url = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()
