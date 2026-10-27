"""Tests for the Email MCP Bridge custom integration (config flow + webhook proxy)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.components.cloud.client import CloudClient
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.email_mcp_bridge.const import (
    CONF_CLOUDHOOK_INJECT,
    CONF_TOKEN,
    CONF_URL,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.email_mcp_bridge.proxy import bearer_matches

from .conftest import FAKE_TOKEN

WEBHOOK_ID = "a" * 64
INIT = {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}


async def _setup(hass: HomeAssistant, url: str, options: dict | None = None) -> MockConfigEntry:
    assert await async_setup_component(hass, "webhook", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_URL: url, CONF_TOKEN: FAKE_TOKEN, CONF_WEBHOOK_ID: WEBHOOK_ID},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _cloudhook_call(hass, method="POST", body="", headers=None) -> dict:
    """Drive HA Core's *real* cloudhook relay code (CloudClient.async_webhook_message)."""
    fake_client = SimpleNamespace(
        _hass=hass,
        _prefs=SimpleNamespace(cloudhooks={WEBHOOK_ID: {"webhook_id": WEBHOOK_ID, "cloudhook_id": "ch-1"}}),
    )
    payload = {"cloudhook_id": "ch-1", "body": body, "headers": headers or {}, "method": method, "query": ""}
    return await CloudClient.async_webhook_message(fake_client, payload)


# ---------------- unit ----------------

def test_bearer_matches():
    assert bearer_matches(f"Bearer {FAKE_TOKEN}", FAKE_TOKEN)
    assert bearer_matches(f"bearer {FAKE_TOKEN}", FAKE_TOKEN)
    assert not bearer_matches(None, FAKE_TOKEN)
    assert not bearer_matches("", FAKE_TOKEN)
    assert not bearer_matches(f"Basic {FAKE_TOKEN}", FAKE_TOKEN)
    assert not bearer_matches(f"Bearer {FAKE_TOKEN}x", FAKE_TOKEN)
    assert not bearer_matches("Bearer ", FAKE_TOKEN)


# ---------------- config flow ----------------

async def test_config_flow_success(hass, fake_upstream):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.email_mcp_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: fake_upstream.url + "/", CONF_TOKEN: FAKE_TOKEN}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == fake_upstream.url
    assert len(result["data"][CONF_WEBHOOK_ID]) == 64


async def test_config_flow_invalid_auth(hass, fake_upstream):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake_upstream.url, CONF_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_cannot_connect(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://127.0.0.1:1", CONF_TOKEN: FAKE_TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------- webhook (real aiohttp path: /api/webhook/<id>) ----------------

async def test_webhook_rejects_missing_or_bad_token(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url)
    client = await hass_client_no_auth()
    for hdrs in ({}, {"Authorization": "Bearer nope"}):
        resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json=INIT, headers=hdrs)
        assert resp.status == 401
        assert "Bearer" in resp.headers.get("WWW-Authenticate", "")
    assert fake_upstream.seen == []  # never reached the add-on


async def test_webhook_forwards_request_and_response(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url)
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        data=json.dumps(INIT),
        headers={
            "Authorization": f"Bearer {FAKE_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Session-Id": "client-sess",
            "Mcp-Protocol-Version": "2025-06-18",
            "Cookie": "secret=1",
            "X-Random": "drop",
        },
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert resp.headers["Mcp-Session-Id"] == "sess-1"
    assert "X-Internal" not in resp.headers
    data = await resp.json()
    assert data["result"] == {"echo": "initialize", "http_method": "POST"}
    seen = fake_upstream.seen[-1]
    h = seen["headers"]
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == INIT
    assert h["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert h["Mcp-Session-Id"] == "client-sess"
    assert h["Mcp-Protocol-Version"] == "2025-06-18"
    assert h["Accept"] == "application/json, text/event-stream"
    assert h["Content-Type"] == "application/json"
    assert "Cookie" not in h and "X-Random" not in h


async def test_webhook_get_allowed_put_rejected(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url)
    client = await hass_client_no_auth()
    auth = {"Authorization": f"Bearer {FAKE_TOKEN}"}
    resp = await client.get(f"/api/webhook/{WEBHOOK_ID}", headers=auth)
    assert resp.status == 200
    assert fake_upstream.seen[-1]["method"] == "GET"
    resp = await client.put(f"/api/webhook/{WEBHOOK_ID}", headers=auth, data=b"{}")
    assert resp.status == 405


async def test_webhook_status_passthrough_and_html_sanitized(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url)
    client = await hass_client_no_auth()
    auth = {"Authorization": f"Bearer {FAKE_TOKEN}"}
    fake_upstream.mode = "status202"
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers=auth, json={"jsonrpc": "2.0", "method": "n"})
    assert resp.status == 202
    fake_upstream.mode = "html"
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers=auth, json=INIT)
    assert resp.headers["Content-Type"].startswith("application/json")


async def test_webhook_streams_sse_on_real_http(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url)
    client = await hass_client_no_auth()
    fake_upstream.mode = "sse"
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers={"Authorization": f"Bearer {FAKE_TOKEN}"}, json=INIT)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert b"data: " in await resp.read()


async def test_webhook_upstream_down_returns_502(hass, hass_client_no_auth, socket_enabled):
    await _setup(hass, "http://127.0.0.1:1")
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers={"Authorization": f"Bearer {FAKE_TOKEN}"}, json=INIT)
    assert resp.status == 502


async def test_unload_unregisters_webhook(hass, hass_client_no_auth, fake_upstream):
    entry = await _setup(hass, fake_upstream.url)
    assert WEBHOOK_ID in hass.data[webhook.DOMAIN]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert WEBHOOK_ID not in hass.data[webhook.DOMAIN]
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers={"Authorization": f"Bearer {FAKE_TOKEN}"}, json=INIT)
    assert resp.status == 200 and await resp.text() == ""  # HA's generic "unknown webhook" reply
    assert fake_upstream.seen == []


# ---------------- cloudhook path (HA Cloud relay -> MockRequest) ----------------

async def test_cloudhook_relay_with_auth_header(hass, fake_upstream):
    await _setup(hass, fake_upstream.url)
    out = await _cloudhook_call(
        hass, body=json.dumps(INIT),
        headers={"Authorization": f"Bearer {FAKE_TOKEN}", "Content-Type": "application/json"},
    )
    assert out["status"] == 200
    assert out["headers"] == {"Content-Type": "application/json"}  # all HA Cloud relays back
    assert json.loads(out["body"])["result"]["echo"] == "initialize"


async def test_cloudhook_relay_rejects_without_auth_by_default(hass, fake_upstream):
    await _setup(hass, fake_upstream.url)
    out = await _cloudhook_call(hass, body=json.dumps(INIT), headers={"Content-Type": "application/json"})
    assert out["status"] == 401
    assert fake_upstream.seen == []


async def test_cloudhook_inject_option_allows_headerless(hass, fake_upstream):
    await _setup(hass, fake_upstream.url, options={CONF_CLOUDHOOK_INJECT: True})
    out = await _cloudhook_call(hass, body=json.dumps(INIT), headers={})
    assert out["status"] == 200
    assert fake_upstream.seen[-1]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    # a WRONG token is still rejected even with injection on
    out = await _cloudhook_call(hass, body=json.dumps(INIT), headers={"Authorization": "Bearer wrong"})
    assert out["status"] == 401


async def test_cloudhook_sse_is_buffered(hass, fake_upstream):
    await _setup(hass, fake_upstream.url)
    fake_upstream.mode = "sse"
    out = await _cloudhook_call(hass, body=json.dumps(INIT), headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert out["status"] == 200
    assert out["headers"]["Content-Type"] == "text/event-stream"
    assert "data: " in out["body"]


async def test_inject_option_does_not_affect_real_http(hass, hass_client_no_auth, fake_upstream):
    await _setup(hass, fake_upstream.url, options={CONF_CLOUDHOOK_INJECT: True})
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json=INIT)
    assert resp.status == 401


# ---------------- cloudhook creation / URL exposure ----------------

async def test_cloudhook_created_and_shown_in_options(hass, fake_upstream):
    hass.config.components.add("cloud")
    with (
        patch("homeassistant.components.cloud.async_active_subscription", return_value=True),
        patch(
            "homeassistant.components.cloud.async_get_or_create_cloudhook",
            AsyncMock(return_value="https://cloud-hook.example/FAKE-CLOUDHOOK"),
        ) as create,
        patch("homeassistant.components.cloud.async_remote_ui_url", return_value="https://remote-ui.example"),
        patch("homeassistant.components.cloud.async_listen_connection_change", return_value=lambda: None),
    ):
        entry = await _setup(hass, fake_upstream.url)
        create.assert_awaited_with(hass, WEBHOOK_ID)
        assert entry.runtime_data.cloudhook_url == "https://cloud-hook.example/FAKE-CLOUDHOOK"
        result = await hass.config_entries.options.async_init(entry.entry_id)
        ph = result["description_placeholders"]
        assert ph["cloudhook_url"] == "https://cloud-hook.example/FAKE-CLOUDHOOK"
        assert ph["remote_ui_url"] == f"https://remote-ui.example/api/webhook/{WEBHOOK_ID}"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_CLOUDHOOK_INJECT: True})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.options[CONF_CLOUDHOOK_INJECT] is True
        assert entry.runtime_data.runtime.cloudhook_inject_token is True  # reloaded


async def test_remove_entry_deletes_cloudhook(hass, fake_upstream):
    hass.config.components.add("cloud")
    with (
        patch("homeassistant.components.cloud.async_active_subscription", return_value=False),
        patch("homeassistant.components.cloud.async_remote_ui_url", side_effect=Exception),
        patch("homeassistant.components.cloud.async_listen_connection_change", return_value=lambda: None),
        patch("homeassistant.components.cloud.async_delete_cloudhook", AsyncMock()) as delete,
    ):
        from homeassistant.components.cloud import CloudNotAvailable

        with patch("homeassistant.components.cloud.async_remote_ui_url", side_effect=CloudNotAvailable):
            entry = await _setup(hass, fake_upstream.url)
        await hass.config_entries.async_remove(entry.entry_id)
        delete.assert_awaited_with(hass, WEBHOOK_ID)


async def test_secrets_not_logged(hass, hass_client_no_auth, fake_upstream, caplog):
    caplog.set_level("DEBUG")
    hass.config.components.add("cloud")
    with (
        patch("homeassistant.components.cloud.async_active_subscription", return_value=True),
        patch(
            "homeassistant.components.cloud.async_get_or_create_cloudhook",
            AsyncMock(return_value="https://cloud-hook.example/FAKE-CLOUDHOOK"),
        ),
        patch("homeassistant.components.cloud.async_remote_ui_url", return_value="https://remote-ui.example"),
        patch("homeassistant.components.cloud.async_listen_connection_change", return_value=lambda: None),
    ):
        await _setup(hass, fake_upstream.url)
        client = await hass_client_no_auth()
        await client.post(f"/api/webhook/{WEBHOOK_ID}", json=INIT)
        await client.post(f"/api/webhook/{WEBHOOK_ID}", json=INIT, headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.email_mcp_bridge"))
    assert "Email MCP Bridge ready" in ours
    assert FAKE_TOKEN not in caplog.text
    assert "FAKE-CLOUDHOOK" not in caplog.text
    assert "remote-ui.example" not in caplog.text
