"""Connectivity sensor: is the outbound link to the Hearth hub up?"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_STATUS, HearthConfigEntry, device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HearthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([HearthConnectedSensor(entry)])


class HearthConnectedSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: HearthConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self._entry.runtime_data.client.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        client = self._entry.runtime_data.client
        return {
            "installation_id": client.installation_id,
            "last_error": client.last_error,
            "capabilities": client.capabilities,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"{SIGNAL_STATUS}_{self._entry.entry_id}", self._status_changed)
        )

    @callback
    def _status_changed(self, _connected: bool) -> None:
        self.async_write_ha_state()
