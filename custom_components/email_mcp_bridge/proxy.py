"""Webhook -> add-on MCP proxy.

Two ways a request reaches this handler:

* ``https://<remote-ui-host>/api/webhook/<webhook_id>`` (or any HA URL): a
  real aiohttp request. Headers in and out work and SSE could stream.
* ``https://<cloud-webhook-host>/<cloudhook_id>`` (cloudhook): HA Cloud relays the
  call over its websocket as ``homeassistant.util.aiohttp.MockRequest``
  (no ``read()``), and returns only ``body``, ``status`` and ``Content-Type``
  to the caller (see ``homeassistant/components/cloud/client.py``
  ``async_webhook_message``). So nothing can stream, ``Mcp-Session-Id`` /
  ``WWW-Authenticate`` never reach the client, and inbound headers may be
  stripped by the relay. The add-on therefore runs stateless + JSON-response.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass

import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.util.aiohttp import MockRequest

from .const import (
    ALLOWED_CONTENT_TYPES,
    CLOUDHOOK_REPLY_SECONDS,
    FORWARD_REQUEST_HEADERS,
    FORWARD_RESPONSE_HEADERS,
    MAX_REQUEST_BYTES,
    MCP_PATH,
)

_LOGGER = logging.getLogger(__name__)
# No wall-clock total (an SSE stream may be long-lived); bound connect + idle read.
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=300)


@dataclass
class ProxyRuntime:
    """Per-entry state needed by the webhook handler."""

    target_url: str  # http://local-email-mcp:9560/mcp
    token: str
    session: aiohttp.ClientSession
    cloudhook_inject_token: bool = False


def bearer_matches(header_value: str | None, token: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer`` header."""
    if not header_value:
        return False
    scheme, _, presented = header_value.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def build_target_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(MCP_PATH) else base + MCP_PATH


def _json_error(status: int, message: str, *, challenge: bool = False) -> web.Response:
    headers = {"WWW-Authenticate": 'Bearer realm="email-mcp"'} if challenge else None
    return web.Response(
        status=status,
        text='{"error":"%s"}' % message,
        content_type="application/json",
        charset="utf-8",
        headers=headers,
    )


async def _read_body(request: web.Request) -> bytes | None:
    """Read at most MAX_REQUEST_BYTES; None if larger."""
    if isinstance(request, MockRequest):
        # Cloudhook relay: body already fully in memory; no ``read()``.
        data = await request.content.read()
    else:
        if (request.content_length or 0) > MAX_REQUEST_BYTES:
            return None
        data = await request.read()
    if len(data) > MAX_REQUEST_BYTES:
        return None
    return data


async def handle_webhook(
    runtime: ProxyRuntime, request: web.Request
) -> web.StreamResponse:
    """Relay one MCP streamable-HTTP request to the add-on."""
    via_cloudhook = isinstance(request, MockRequest)
    auth = request.headers.get("Authorization")

    if not bearer_matches(auth, runtime.token):
        allow_injected = via_cloudhook and runtime.cloudhook_inject_token and not auth
        if not allow_injected:
            _LOGGER.debug("Rejected request without valid bearer (cloudhook=%s)", via_cloudhook)
            return _json_error(401, "unauthorized", challenge=not via_cloudhook)

    body = await _read_body(request)
    if body is None:
        return _json_error(413, "request too large")

    headers = {h: v for h in FORWARD_REQUEST_HEADERS if (v := request.headers.get(h))}
    # The add-on re-checks the token. We always send our stored token, so a
    # cloudhook relay that drops headers still authenticates when (and only
    # when) the user opted into cloudhook token injection.
    headers["Authorization"] = f"Bearer {runtime.token}"

    try:
        upstream_cm = runtime.session.request(
            request.method,
            runtime.target_url,
            headers=headers,
            data=body or None,
            allow_redirects=False,
            timeout=UPSTREAM_TIMEOUT,
        )
        async with upstream_cm as upstream:
            return await _relay(request, upstream, via_cloudhook)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Email MCP add-on unreachable: %s", type(err).__name__)
        return _json_error(502, "upstream unavailable")


async def _relay(
    request: web.Request, upstream: aiohttp.ClientResponse, via_cloudhook: bool
) -> web.StreamResponse:
    content_type = upstream.headers.get("Content-Type", "")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in ALLOWED_CONTENT_TYPES:
        mime = "application/json"  # never let the upstream serve HTML via HA's origin
    out_headers = {
        h: v for h in FORWARD_RESPONSE_HEADERS if (v := upstream.headers.get(h))
    }
    out_headers["Cache-Control"] = "no-cache, no-transform"

    if mime == "text/event-stream" and not via_cloudhook:
        # Real HTTP path: stream SSE through (unused while the add-on runs in
        # JSON-response mode, kept for completeness).
        out_headers["Content-Type"] = "text/event-stream"
        out_headers["X-Accel-Buffering"] = "no"
        response = web.StreamResponse(status=upstream.status, headers=out_headers)
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response

    try:
        async with asyncio.timeout(CLOUDHOOK_REPLY_SECONDS if via_cloudhook else None):
            payload = await upstream.read()
    except TimeoutError:
        return _json_error(504, "upstream reply too slow for cloudhook relay")
    return web.Response(
        status=upstream.status,
        body=payload,
        content_type=mime,
        charset="utf-8",
        headers=out_headers,
    )


def make_handler(hass: HomeAssistant, get_runtime):
    """Adapter with the signature ``webhook.async_register`` expects."""

    async def _handler(hass: HomeAssistant, webhook_id: str, request: web.Request):
        runtime = get_runtime(webhook_id)
        if runtime is None:
            return _json_error(404, "not found")
        return await handle_webhook(runtime, request)

    return _handler
