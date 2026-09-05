"""Hearth AI — lets an LLM author automations, scenes, and scripts via the Hearth hub."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .client import HearthClient
from .const import CONF_INSTALL_SECRET, CONF_WS_URL, DOMAIN
from .options import HearthOptions
from .rpc import build_dispatcher

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CONVERSATION, Platform.SWITCH]
SIGNAL_STATUS = f"{DOMAIN}_status"


@dataclass
class HearthData:
    client: HearthClient
    options: HearthOptions


type HearthConfigEntry = ConfigEntry[HearthData]


def device_info(entry: ConfigEntry) -> dr.DeviceInfo:
    """One device groups the connection switch, connectivity sensor, and conversation agent."""
    return dr.DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Hearth AI", manufacturer="Hearth", entry_type=dr.DeviceEntryType.SERVICE)


async def async_setup_entry(hass: HomeAssistant, entry: HearthConfigEntry) -> bool:
    """Set up from a config entry (re-run on every options change via OptionsFlowWithReload)."""
    options = HearthOptions.from_entry(entry)
    capabilities = options.capabilities
    dispatcher = build_dispatcher(hass, frozenset(capabilities))

    def _status(connected: bool) -> None:
        async_dispatcher_send(hass, f"{SIGNAL_STATUS}_{entry.entry_id}", connected)

    client = HearthClient(
        hass,
        ws_url=entry.data[CONF_WS_URL],
        install_secret=entry.data[CONF_INSTALL_SECRET],
        dispatcher=dispatcher,
        on_status=_status,
        capabilities=capabilities,
    )
    entry.runtime_data = HearthData(client=client, options=options)

    if not options.managed:
        # Don't leave a stale, permanently-unavailable conversation agent behind.
        ent_reg = er.async_get(hass)
        if entity_id := ent_reg.async_get_entity_id("conversation", DOMAIN, entry.entry_id):
            ent_reg.async_remove(entity_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if options.connection_enabled:
        client.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HearthConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.client.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
