"""Bearer-token gate in front of mcp-email-server (streamable HTTP).

Runs inside the add-on container on 0.0.0.0:<listen_port> and forwards
authenticated requests to the MCP server bound to 127.0.0.1:<upstream_port>.

* Every path except GET /healthz requires ``Authorization: Bearer <token>``;
  the comparison is constant time (hmac.compare_digest).
* The token is read from a file (never argv/env of a child) and never logged.
* The Authorization header is NOT forwarded upstream (the MCP server does not
  need it), and Host/Origin are rewritten/stripped so the server's
  DNS-rebinding protection (which only trusts loopback) stays enabled.
* Responses are streamed through unchanged, so SSE works if the server emits it.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys

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
    def __init__(self, token: str, upstream: str) -> None:
        if len(token) < 16:
            raise ValueError("bearer token too short")
        self._token = token.encode()
        self._upstream = upstream.rstrip("/")
        self._upstream_host = httpx.URL(self._upstream).netloc.decode()
        self._client: httpx.AsyncClient | None = None

    async def _send_simple(self, send, status: int, body: bytes, extra=()) -> None:
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        headers.extend(extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=60, pool=30))
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
            await self._send_simple(send, 200, b'{"status":"ok"}')
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
        except httpx.HTTPError as err:
            _LOGGER.error("Upstream MCP server unavailable: %s", type(err).__name__)
            await self._send_simple(send, 502, b'{"error":"upstream unavailable"}')
            return
        try:
            out_headers = [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in resp.headers.multi_items()
                if k.lower() not in HOP_BY_HOP
            ]
            await send({"type": "http.response.start", "status": resp.status_code, "headers": out_headers})
            async for chunk in resp.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await resp.aclose()


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
