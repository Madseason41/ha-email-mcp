"""Unit tests for the add-on's bearer gate (no Docker needed)."""

import asyncio
import importlib.util
import json
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

P = Path(__file__).resolve().parents[2] / "email_mcp/rootfs/opt/email_mcp/authproxy.py"
spec = importlib.util.spec_from_file_location("email_mcp_authproxy", P)
ap = importlib.util.module_from_spec(spec)
sys.modules["email_mcp_authproxy"] = ap
spec.loader.exec_module(ap)

TOKEN = "fake-token-0123456789abcdef-NOT-SECRET"


def test_bearer_ok():
    t = TOKEN.encode()
    assert ap._bearer_ok(f"Bearer {TOKEN}", t)
    assert not ap._bearer_ok(None, t)
    assert not ap._bearer_ok(f"Basic {TOKEN}", t)
    assert not ap._bearer_ok(f"Bearer {TOKEN[:-1]}", t)
    assert not ap._bearer_ok("Bearer", t)


def test_rejects_short_token():
    with pytest.raises(ValueError):
        ap.AuthProxy("short", "http://127.0.0.1:1")


async def test_gate_and_header_hygiene():
    seen = {}

    async def mcp(request):
        seen.update(dict(request.headers))
        return JSONResponse({"ok": True}, headers={"mcp-session-id": "s1"})

    upstream = Starlette(routes=[Route("/mcp", mcp, methods=["POST", "GET"])])
    proxy = ap.AuthProxy(TOKEN, "http://upstream:9557")
    proxy._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        assert (await c.get("/healthz")).status_code == 200
        r = await c.post("/mcp", json={})
        assert r.status_code == 401 and "bearer" in r.headers["www-authenticate"].lower()
        assert seen == {}
        r = await c.post("/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil",
                                                   "Cookie": "a=b", "Mcp-Session-Id": "c1"})
        assert r.status_code == 200 and r.headers["mcp-session-id"] == "s1"
        assert "authorization" not in seen and "origin" not in seen and "cookie" not in seen
        assert seen["host"] == "upstream:9557" and seen["mcp-session-id"] == "c1"
        big = b"x" * (ap.MAX_BODY + 10)
        r = await c.post("/mcp", content=big, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 413
    await proxy._client.aclose()


def _proxy_over(upstream_app, *, started: float | None = None):
    proxy = ap.AuthProxy(TOKEN, "http://upstream:9557")
    proxy._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app), trust_env=False)
    if started is not None:
        proxy._started = started
    return proxy


async def test_get_mcp_is_405_and_does_not_reach_upstream():
    seen = []

    async def mcp(request):
        seen.append(request.method)
        return JSONResponse({"ok": True})

    upstream = Starlette(routes=[Route("/mcp", mcp, methods=["POST", "GET"])])
    proxy = _proxy_over(upstream)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        denied = await c.get("/mcp")
        assert denied.status_code == 401
        refused = await c.get("/mcp", headers=auth)
        assert refused.status_code == 405
        assert refused.headers["allow"] == "POST"
        assert refused.json() == {"error": "method not allowed"}
        # Trailing slash is the same endpoint.
        assert (await c.get("/mcp/", headers=auth)).status_code == 405
        posted = await c.post("/mcp", json={"method": "tools/list"}, headers=auth)
        assert posted.status_code == 200
    assert seen == ["POST"]
    await proxy._client.aclose()


class _RaiseClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def build_request(self, *args, **kwargs):
        return httpx.Request("POST", "http://upstream:9557/mcp")

    async def send(self, *args, **kwargs):
        raise self._exc

    def stream(self, *args, **kwargs):
        raise self._exc


async def test_pool_timeout_is_503_and_connect_error_is_502():
    auth = {"Authorization": f"Bearer {TOKEN}"}
    proxy = ap.AuthProxy(TOKEN, "http://upstream:9557")
    proxy._client = _RaiseClient(httpx.PoolTimeout("pool"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        r = await c.post("/mcp", json={"method": "tools/list"}, headers=auth)
        assert r.status_code == 503
        assert r.json() == {"error": "upstream unavailable"}
    proxy._client = _RaiseClient(httpx.ConnectError("refused"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        r = await c.post("/mcp", json={"method": "tools/list"}, headers=auth)
        assert r.status_code == 502
        assert r.json() == {"error": "upstream unavailable"}


async def test_healthz_grace_then_upstream():
    async def mcp(request):
        return JSONResponse({"ok": True})

    upstream = Starlette(routes=[Route("/mcp", mcp, methods=["POST", "GET"])])
    proxy = ap.AuthProxy(TOKEN, "http://upstream:9557")
    proxy._started = time.monotonic()
    proxy._client = _RaiseClient(httpx.ConnectError("down"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        # Boot grace does not probe, so a down mail server does not flap startup.
        assert (await c.get("/healthz")).status_code == 200
    proxy._started = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
        down = await c.get("/healthz")
        assert down.status_code == 503
        assert down.json()["status"] == "upstream unavailable"
    proxy._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream), trust_env=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://addon") as c:
            # A 404 from the mail server still means the process answered.
            assert (await c.get("/healthz")).status_code == 200
    finally:
        await proxy._client.aclose()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(app, port: int, *, lifespan: str):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False, lifespan=lifespan)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server, task
        if task.done():
            raise RuntimeError(f"uvicorn exited before listening: {task.exception()!r}")
        await asyncio.sleep(0.02)
    server.should_exit = True
    await asyncio.gather(task, return_exceptions=True)
    raise RuntimeError("uvicorn did not start")


async def _stop_servers(*servers) -> None:
    tasks = []
    for server, task in servers:
        server.should_exit = True
        tasks.append(task)
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _hanging_upstream(state: dict):
    """MCP stand-in: GET and {"hang": true} hold an SSE stream until disconnect."""

    async def _json(send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _hold(receive, send) -> None:
        state["streams"] += 1
        try:
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            })
            await send({"type": "http.response.body", "body": b": open\n\n", "more_body": True})
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
        finally:
            state["streams"] -= 1

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"].rstrip("/") != "/mcp":
            await _json(send, 404, b'{"error":"not found"}')
            return
        if scope["method"] == "GET":
            state["gets"] += 1
            await _hold(receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        if b"tools/list" in body:
            await _json(send, 200, b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"list_mailboxes"}]}}')
            return
        if b"hang" in body:
            await _hold(receive, send)
            return
        await _json(send, 200, b'{"ok":true}')

    return app


@pytest.mark.timeout(20)
async def test_abandoned_get_streams_do_not_exhaust_pool(socket_enabled):
    """More abandoned GET/SSE streams than the pool must still leave tools/list working.

    The upstream holds GET /mcp and a streaming POST open until the client
    disconnects, which is what mcp-email-server does for standalone SSE. The
    proxy pool is smaller than the number of attempts.
    """
    state = {"streams": 0, "gets": 0}
    upstream_port = _free_port()
    proxy_port = _free_port()
    pool_size = 2
    attempts = pool_size + 6
    proxy = ap.AuthProxy(
        TOKEN,
        f"http://127.0.0.1:{upstream_port}",
        limits=httpx.Limits(max_connections=pool_size, max_keepalive_connections=pool_size, keepalive_expiry=5.0),
        timeout=httpx.Timeout(connect=2.0, read=30.0, write=5.0, pool=0.5),
    )
    servers = []
    held: list[httpx.Response] = []
    # aiter_raw() is an async generator. Dropping it closes the response, which
    # looks like the client hung up. Keep each iterator until we abandon it.
    readers: list = []
    try:
        servers.append(await _serve(_hanging_upstream(state), upstream_port, lifespan="off"))
        servers.append(await _serve(proxy, proxy_port, lifespan="on"))
        auth = {"Authorization": f"Bearer {TOKEN}"}
        base = f"http://127.0.0.1:{proxy_port}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            trust_env=False,
        ) as client:
            gets = await asyncio.gather(*[client.get(f"{base}/mcp", headers=auth) for _ in range(attempts)])
            assert [r.status_code for r in gets] == [405] * attempts
            assert state["gets"] == 0 and state["streams"] == 0

            async def open_hang():
                req = client.build_request(
                    "POST", f"{base}/mcp",
                    headers={**auth, "content-type": "application/json"},
                    content=b'{"hang":true}',
                )
                resp = await client.send(req, stream=True)
                if resp.status_code == 503:
                    assert json.loads(await resp.aread()) == {"error": "upstream unavailable"}
                    await resp.aclose()
                    return None
                held.append(resp)
                reader = resp.aiter_raw()
                readers.append(reader)
                chunk = await asyncio.wait_for(reader.__anext__(), timeout=2)
                assert chunk
                return resp

            started = time.monotonic()
            opened = await asyncio.gather(*[open_hang() for _ in range(attempts)])
            # The extra attempts must fail while the pool is full, not hang.
            assert time.monotonic() - started < 8
            assert sum(resp is not None for resp in opened) == pool_size
            assert len(held) == pool_size
            assert state["streams"] == pool_size

            for reader in readers:
                await reader.aclose()
            readers.clear()
            for resp in held:
                await resp.aclose()
            held.clear()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and state["streams"] != 0:
                await asyncio.sleep(0.05)
            assert state["streams"] == 0

            listed = await client.post(
                f"{base}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers=auth,
            )
            assert listed.status_code == 200, listed.text
            assert "list_mailboxes" in listed.text
    finally:
        for reader in readers:
            await reader.aclose()
        for resp in held:
            await resp.aclose()
        if servers:
            await _stop_servers(*servers)
