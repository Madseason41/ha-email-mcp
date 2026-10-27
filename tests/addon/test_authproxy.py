"""Unit tests for the add-on's bearer gate (no Docker needed)."""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
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
