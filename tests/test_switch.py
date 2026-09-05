"""Connection switch + connectivity sensor."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai import SIGNAL_STATUS
from custom_components.hearth_ai.const import DOMAIN

from .test_conversation import ENTRY_DATA

SWITCH = "switch.hearth_ai_connection"
SENSOR = "binary_sensor.hearth_ai_connected"


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    e = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA, options=options or {})
    e.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


async def test_entities_and_toggle(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get(SENSOR).state == "off"
    assert hass.states.get("conversation.hearth_ai") is not None

    # connectivity follows the client's status signal
    entry.runtime_data.client.connected = True
    async_dispatcher_send(hass, f"{SIGNAL_STATUS}_{entry.entry_id}", True)
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "on"
    assert hass.states.get(SENSOR).attributes["capabilities"] == entry.runtime_data.client.capabilities

    # turn off -> option saved, entry reloaded without dialing out
    with patch("custom_components.hearth_ai.HearthClient.start") as start:
        await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: SWITCH}, blocking=True)
        await hass.async_block_till_done()
    assert entry.options["connection_enabled"] is False
    assert hass.states.get(SWITCH).state == "off"
    assert not start.called
    assert hass.states.get(SENSOR).state == "off"
    assert hass.states.get("conversation.hearth_ai").state == "unavailable"

    # turn on -> reload starts the client again
    with patch("custom_components.hearth_ai.HearthClient.start") as start:
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: SWITCH}, blocking=True)
        await hass.async_block_till_done()
    assert entry.options["connection_enabled"] is True
    assert start.called


async def test_disabled_at_setup_never_connects(hass: HomeAssistant) -> None:
    with patch("custom_components.hearth_ai.HearthClient.start") as start:
        await _setup(hass, {"connection_enabled": False})
    assert not start.called
    assert hass.states.get(SWITCH).state == "off"
