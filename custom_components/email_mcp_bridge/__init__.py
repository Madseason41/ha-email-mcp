"""Email MCP Bridge: expose the Email MCP add-on through an HA webhook / Nabu Casa cloudhook."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_CLOUDHOOK_INJECT, CONF_TOKEN, CONF_URL, CONF_WEBHOOK_ID, DOMAIN
from .proxy import ProxyRuntime, build_target_url, make_handler

_LOGGER = logging.getLogger(__name__)


@dataclass
class BridgeData:
    """Runtime data stored on the config entry."""

    runtime: ProxyRuntime
    webhook_id: str
    cloudhook_url: str | None = None
    remote_ui_url: str | None = None
    unsubs: list = field(default_factory=list)


type EmailMcpConfigEntry = ConfigEntry[BridgeData]


def _cloud_loaded(hass: HomeAssistant) -> bool:
    return "cloud" in hass.config.components


async def async_refresh_urls(hass: HomeAssistant, entry: EmailMcpConfigEntry) -> None:
    """(Re)discover the cloudhook and remote-UI URLs. Never logs them."""
    data = entry.runtime_data
    if not _cloud_loaded(hass):
        return
    from homeassistant.components import cloud  # noqa: PLC0415

    try:
        if cloud.async_active_subscription(hass):
            data.cloudhook_url = await cloud.async_get_or_create_cloudhook(hass, data.webhook_id)
    except cloud.CloudNotAvailable:
        _LOGGER.info("Home Assistant Cloud not connected yet; cloudhook not created")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not create Nabu Casa cloudhook: %s", type(err).__name__)
    try:
        data.remote_ui_url = (
            cloud.async_remote_ui_url(hass).rstrip("/") + f"/api/webhook/{data.webhook_id}"
        )
    except cloud.CloudNotAvailable:
        data.remote_ui_url = None


async def async_setup_entry(hass: HomeAssistant, entry: EmailMcpConfigEntry) -> bool:
    """Register the webhook and (if available) the Nabu Casa cloudhook."""
    webhook_id: str = entry.data[CONF_WEBHOOK_ID]
    # HA's shared session (closed by HA on shutdown); per-request timeouts in proxy.py.
    session = async_get_clientsession(hass)
    runtime = ProxyRuntime(
        target_url=build_target_url(entry.data[CONF_URL]),
        token=entry.data[CONF_TOKEN],
        session=session,
        cloudhook_inject_token=entry.options.get(CONF_CLOUDHOOK_INJECT, False),
    )
    entry.runtime_data = BridgeData(runtime=runtime, webhook_id=webhook_id)

    @callback
    def _get_runtime(wh_id: str) -> ProxyRuntime | None:
        return runtime if wh_id == webhook_id else None

    webhook.async_register(
        hass,
        DOMAIN,
        "Email MCP Bridge",
        webhook_id,
        make_handler(hass, _get_runtime),
        local_only=False,
        allowed_methods=["POST", "GET"],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    await async_refresh_urls(hass, entry)
    if _cloud_loaded(hass) and entry.runtime_data.cloudhook_url is None:
        from homeassistant.components import cloud  # noqa: PLC0415

        async def _on_cloud_state(state) -> None:
            if state is cloud.CloudConnectionState.CLOUD_CONNECTED:
                await async_refresh_urls(hass, entry)

        entry.async_on_unload(cloud.async_listen_connection_change(hass, _on_cloud_state))

    _LOGGER.info(
        "Email MCP Bridge ready (cloudhook: %s, remote UI URL: %s). "
        "Open the integration's Configure dialog to view the connector URLs.",
        "yes" if entry.runtime_data.cloudhook_url else "no",
        "yes" if entry.runtime_data.remote_ui_url else "no",
    )
    return True


async def _async_reload(hass: HomeAssistant, entry: EmailMcpConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EmailMcpConfigEntry) -> bool:
    """Unregister the webhook (done by the async_on_unload callbacks)."""
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the Nabu Casa cloudhook when the entry is deleted (rollback)."""
    if not _cloud_loaded(hass):
        return
    from homeassistant.components import cloud  # noqa: PLC0415

    try:
        await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    except Exception as err:  # noqa: BLE001
        _LOGGER.info("No cloudhook removed (%s)", type(err).__name__)
