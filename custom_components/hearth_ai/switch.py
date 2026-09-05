"""Connection switch: lets dashboards and automations pause the link to Hearth."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HearthConfigEntry, device_info
from .const import OPT_CONNECTION_ENABLED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HearthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([HearthConnectionSwitch(entry)])


class HearthConnectionSwitch(SwitchEntity):
    """Mirrors the `connection_enabled` option; toggling it reloads the entry (same path as the options form)."""

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cloud-sync"

    def __init__(self, entry: HearthConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(OPT_CONNECTION_ENABLED, True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        if self.is_on == enabled:
            return
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, OPT_CONNECTION_ENABLED: enabled})
        self.async_write_ha_state()
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
