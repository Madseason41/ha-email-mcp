"""Config flow for Email MCP Bridge."""

from __future__ import annotations

import secrets
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CLOUDHOOK_INJECT,
    CONF_TOKEN,
    CONF_URL,
    CONF_WEBHOOK_ID,
    DEFAULT_URL,
    DOMAIN,
)
from .proxy import build_target_url

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


async def validate(hass, url: str, token: str) -> str | None:
    """Return an error key, or None when the add-on answers an authenticated tools/list."""
    session = async_get_clientsession(hass)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    try:
        async with session.post(
            build_target_url(url),
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status != 200:
                return "cannot_connect"
            data = await resp.json(content_type=None)
            if "result" not in data:
                return "cannot_connect"
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return "cannot_connect"
    return None


class EmailMcpBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the add-on URL + bearer token."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            token = user_input[CONF_TOKEN].strip()
            if not url.startswith(("http://", "https://")):
                errors[CONF_URL] = "invalid_url"
            elif (err := await validate(self.hass, url, token)) is not None:
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title="Email MCP Bridge",
                    data={
                        CONF_URL: url,
                        CONF_TOKEN: token,
                        CONF_WEBHOOK_ID: secrets.token_hex(32),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_URL): URL,
                vol.Required(CONF_TOKEN): PASSWORD,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change URL/token (e.g. after rotating the add-on token); keeps the webhook id."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            token = user_input[CONF_TOKEN].strip()
            if not url.startswith(("http://", "https://")):
                errors[CONF_URL] = "invalid_url"
            elif (err := await validate(self.hass, url, token)) is not None:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_URL: url, CONF_TOKEN: token}
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=entry.data[CONF_URL]): URL,
                vol.Required(CONF_TOKEN): PASSWORD,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return EmailMcpBridgeOptionsFlow()


class EmailMcpBridgeOptionsFlow(OptionsFlow):
    """Shows the connector URLs (admin-only UI) and the cloudhook auth option."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry = self.config_entry
        rd = getattr(entry, "runtime_data", None)
        if rd is not None:
            from . import async_refresh_urls  # noqa: PLC0415

            await async_refresh_urls(self.hass, entry)
        none = "(not available: Home Assistant Cloud not logged in / remote UI off)"
        placeholders = {
            "cloudhook_url": (rd.cloudhook_url if rd and rd.cloudhook_url else none),
            "remote_ui_url": (rd.remote_ui_url if rd and rd.remote_ui_url else none),
            "local_path": f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
        }
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CLOUDHOOK_INJECT,
                    default=entry.options.get(CONF_CLOUDHOOK_INJECT, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, description_placeholders=placeholders)
