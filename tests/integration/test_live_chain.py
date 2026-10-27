"""Live chain: HA test instance -> webhook/cloudhook -> real add-on container -> GreenMail.

Skipped unless EMAIL_MCP_LIVE_URL is set (e.g. http://127.0.0.1:19560) and
EMAIL_MCP_LIVE_TOKEN holds the add-on's (fake) test token. Box-only.
"""

from __future__ import annotations

import json
import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .test_bridge import WEBHOOK_ID, _cloudhook_call

LIVE_URL = os.environ.get("EMAIL_MCP_LIVE_URL")
LIVE_TOKEN = os.environ.get("EMAIL_MCP_LIVE_TOKEN", "")
pytestmark = pytest.mark.skipif(not LIVE_URL, reason="live add-on not configured")


async def _setup_live(hass):
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.email_mcp_bridge.const import CONF_TOKEN, CONF_URL, CONF_WEBHOOK_ID, DOMAIN

    assert await async_setup_component(hass, "webhook", {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: LIVE_URL, CONF_TOKEN: LIVE_TOKEN, CONF_WEBHOOK_ID: WEBHOOK_ID})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_live_mcp_client_through_ha_webhook(hass, hass_client_no_auth, socket_enabled):
    await _setup_live(hass)
    client = await hass_client_no_auth()
    url = str(client.make_url(f"/api/webhook/{WEBHOOK_ID}"))
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {LIVE_TOKEN}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.serverInfo.name
            tools = await s.list_tools()
            assert "list_emails_metadata" in {t.name for t in tools.tools}
            res = await s.call_tool("list_emails_metadata", {"account_name": "alice", "page_size": 10})
            assert not res.isError
            text = json.dumps(res.structuredContent)
            assert "Quarterly report" in text
            blocked = await s.call_tool("send_email", {"account_name": "alice", "recipients": ["bob@test.local"],
                                                       "subject": "VIA-HA-BLOCKED", "body": "x"})
            assert blocked.isError
    # and without the token HA itself answers 401
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status == 401


async def test_live_raw_jsonrpc_through_cloudhook_relay(hass, socket_enabled):
    await _setup_live(hass)
    hdrs = {"Authorization": f"Bearer {LIVE_TOKEN}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cloudhook-sim", "version": "0"}}}
    out = await _cloudhook_call(hass, body=json.dumps(init), headers=hdrs)
    assert out["status"] == 200 and out["headers"] == {"Content-Type": "application/json"}
    assert json.loads(out["body"])["result"]["serverInfo"]["name"]
    # No Mcp-Session-Id can come back over a cloudhook; stateless server must accept the next call anyway.
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_emails_metadata", "arguments": {"account_name": "alice"}}}
    out = await _cloudhook_call(hass, body=json.dumps(call), headers=hdrs)
    assert out["status"] == 200, out
    result = json.loads(out["body"])["result"]
    assert not result.get("isError")
    assert "Dinner on Friday?" in json.dumps(result)
