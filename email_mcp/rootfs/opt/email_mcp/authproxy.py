"""Bearer-token gate in front of mcp-email-server (streamable HTTP).

Runs inside the add-on container on 0.0.0.0:<listen_port> and forwards
authenticated requests to the MCP server bound to 127.0.0.1:<upstream_port>.

* Every path except GET /healthz requires ``Authorization: Bearer <token>``;
  the comparison is constant time (hmac.compare_digest).
* The token is read from a file (never argv/env of a child) and never logged.
* The Authorization header is NOT forwarded upstream (the MCP server does not
  need it), and Host/Origin are rewritten/stripped so the server's
  DNS-rebinding protection (which only trusts loopback) stays enabled.
* GET /mcp is answered with 405. This server is stateless and does not push
  server-initiated messages; the streamable HTTP spec requires 405 when the
  server does not offer that SSE stream. Answering here keeps a client from
  pinning a pooled connection for the life of the process.
* Other responses are streamed. The upstream response is closed when the
  downstream client disconnects or the body ends, so a dropped stream cannot
  keep a connection-pool slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import sys
import time

import httpx
import uvicorn

_LOGGER = logging.getLogger("email_mcp.authproxy")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# Request headers we never pass to the MCP server.
STRIP_REQUEST = HOP_BY_HOP | {"authorization", "origin", "cookie", "x-forwarded-for", "x-forwarded-host"}
MAX_BODY = int(os.environ.get("EMAIL_MCP_MAX_BODY", str(4 * 1024 * 1024)))
# A leaked SSE stream used to occupy a slot until restart (httpx default is
# 100). Mail traffic is a handful of concurrent calls; a full pool should
# fail the next request quickly instead of wedging the add-on.
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=10, keepalive_expiry=15.0)
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=5.0)
# Health probes must not sit behind a long IMAP read, and must not follow a
# body that might be an SSE stream.
_PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
# Docker's HEALTHCHECK start-period covers boot. After this, /healthz reports
# the upstream mail server so a dead process fails the container health check.
_STARTUP_GRACE_SECONDS = 20.0
_UNAVAILABLE = b'{"error":"upstream unavailable"}'


def _bearer_ok(header_value: str | None, token: bytes) -> bool:
    if not header_value:
        return False
    scheme, _, presented = header_value.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    # compare_digest on bytes of possibly different length is still constant
    # time with respect to the secret's content.
    return hmac.compare_digest(presented.strip().encode(), token)


class AuthProxy:
    def __init__(
        self,
        token: str,
        upstream: str,
        *,
        limits: httpx.Limits | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        if len(token) < 16:
            raise ValueError("bearer token too short")
        self._token = token.encode()
        self._upstream = upstream.rstrip("/")
        self._upstream_host = httpx.URL(self._upstream).netloc.decode()
        self._limits = limits or _LIMITS
        self._timeout = timeout or _TIMEOUT
        self._client: httpx.AsyncClient | None = None
        self._started = time.monotonic()

    async def _send_simple(self, send, status: int, body: bytes, extra=()) -> None:
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers.extend(extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _upstream_alive(self) -> bool:
        """True when the mail server answers an HTTP request.

        Headers are enough: a healthy process returns them immediately, including
        a 404 for this path. The body is not read, so a handler that would hold
        an SSE stream cannot pin a pool slot. Any completed response counts;
        connect, pool, and read timeouts do not.
        """
        if self._client is None:
            return False
        try:
            async with self._client.stream(
                "GET",
                f"{self._upstream}/healthz",
                timeout=_PROBE_TIMEOUT,
                follow_redirects=False,
            ) as response:
                return response.status_code > 0
        except httpx.HTTPError as err:
            _LOGGER.warning("Upstream MCP server is not responding: %s", type(err).__name__)
            return False

    async def _relay(self, resp: httpx.Response, receive, send) -> None:
        """Stream ``resp`` to the client, and always release the pool slot.

        Uvicorn's ``send()`` returns without error after the client has gone
        away, and ``aiter_raw()`` blocks while the upstream is quiet or
        heartbeating. A concurrent ``receive()`` is what observes
        ``http.disconnect``; closing the upstream response is what unblocks
        the read and returns the connection to the pool.
        """
        disconnected = asyncio.Event()
        closed = False
        lock = asyncio.Lock()
        # Uvicorn answers receive() with http.disconnect once the response is
        # complete, even if the client is still connected. That is not a drop.
        finished = False

        async def close_upstream() -> None:
            nonlocal closed
            async with lock:
                if closed:
                    return
                closed = True
                try:
                    await resp.aclose()
                except Exception as err:  # noqa: BLE001 — release must not raise
                    _LOGGER.warning("Failed to close upstream response: %s", type(err).__name__)

        async def watch_disconnect() -> None:
            try:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        disconnected.set()
                        if not finished:
                            _LOGGER.debug("Client disconnected; closing upstream stream")
                            await close_upstream()
                        return
            except asyncio.CancelledError:
                raise

        watcher = asyncio.create_task(watch_disconnect())
        started = False
        try:
            out_headers = [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in resp.headers.multi_items()
                if k.lower() not in HOP_BY_HOP
            ]
            await send({"type": "http.response.start", "status": resp.status_code, "headers": out_headers})
            started = True
            async for chunk in resp.aiter_raw():
                if disconnected.is_set():
                    return
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if not disconnected.is_set():
                finished = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except httpx.HTTPError as err:
            if not disconnected.is_set():
                _LOGGER.warning("Upstream stream failed: %s", type(err).__name__)
            if not started:
                raise
        finally:
            try:
                await close_upstream()
            finally:
                if not watcher.done():
                    watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    # Loopback upstream. Never honor HTTP(S)_PROXY from the environment.
                    self._client = httpx.AsyncClient(
                        timeout=self._timeout, limits=self._limits, trust_env=False,
                    )
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    if self._client:
                        await self._client.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return

        method = scope["method"]
        path = scope["path"]
        if path == "/healthz" and method == "GET":
            # Immediate 200 during boot so a slow mail server cannot flap the
            # container health check. Afterwards a dead upstream is a real failure.
            if (time.monotonic() - self._started) < _STARTUP_GRACE_SECONDS or await self._upstream_alive():
                await self._send_simple(send, 200, b'{"status":"ok"}')
            else:
                await self._send_simple(send, 503, b'{"status":"upstream unavailable"}')
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not _bearer_ok(headers.get("authorization"), self._token):
            _LOGGER.warning("Rejected unauthenticated %s %s", method, path)
            await self._send_simple(
                send,
                401,
                b'{"error":"unauthorized"}',
                extra=[(b"www-authenticate", b'Bearer realm="email-mcp"')],
            )
            return

        # Stateless streamable HTTP: no server-initiated SSE. 405 tells the
        # client (mcp-remote and the MCP SDKs) to stop holding GET /mcp open.
        if method == "GET" and path.rstrip("/") == "/mcp":
            await self._send_simple(
                send,
                405,
                b'{"error":"method not allowed"}',
                extra=[(b"allow", b"POST")],
            )
            return

        # Buffer the request body (MCP JSON-RPC messages are small).
        body = bytearray()
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            more = msg.get("more_body", False)
            if len(body) > MAX_BODY:
                await self._send_simple(send, 413, b'{"error":"request too large"}')
                return

        fwd = {k: v for k, v in headers.items() if k not in STRIP_REQUEST}
        fwd["host"] = self._upstream_host
        qs = scope.get("query_string", b"").decode("latin-1")
        url = self._upstream + path + (f"?{qs}" if qs else "")
        assert self._client is not None
        try:
            req = self._client.build_request(method, url, headers=fwd, content=bytes(body) or None)
            resp = await self._client.send(req, stream=True)
        except httpx.PoolTimeout as err:
            # Waiting for a free slot must not hold one, and must not block
            # the client until the pool happens to drain.
            _LOGGER.error("Upstream MCP server unavailable: %s", type(err).__name__)
            await self._send_simple(send, 503, _UNAVAILABLE)
            return
        except httpx.HTTPError as err:
            _LOGGER.error("Upstream MCP server unavailable: %s", type(err).__name__)
            await self._send_simple(send, 502, _UNAVAILABLE)
            return
        await self._relay(resp, receive, send)


def main() -> None:
    logging.basicConfig(level=os.environ.get("EMAIL_MCP_LOG_LEVEL", "INFO").upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Never let HTTP client internals log at debug level.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    token_file = os.environ["EMAIL_MCP_TOKEN_FILE"]
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    listen_host = os.environ.get("EMAIL_MCP_LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("EMAIL_MCP_LISTEN_PORT", "9560"))
    upstream = os.environ.get("EMAIL_MCP_UPSTREAM", "http://127.0.0.1:9557")
    app = AuthProxy(token, upstream)
    # access_log=False: uvicorn access logs are not secret-bearing (no headers),
    # but keep the add-on log quiet; our own 401 warnings remain.
    uvicorn.run(app, host=listen_host, port=listen_port, access_log=False, log_level="warning",
                proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
