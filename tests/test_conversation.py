"""Conversation entity forwards utterances to the hub and degrades gracefully."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.const import (
    CONF_HUB_URL,
    CONF_INSTALL_SECRET,
    CONF_INSTALLATION_ID,
    CONF_WS_URL,
    DOMAIN,
)
from custom_components.hearth_ai.rpc import RpcError


ENTRY_DATA = {
    CONF_HUB_URL: "http://hub.test",
    CONF_INSTALLATION_ID: "inst-1",
    CONF_INSTALL_SECRET: "his_test",
    CONF_WS_URL: "ws://hub.test/ws/integration",
}


async def make_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    e = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA, options=options or {})
    e.add_to_hass(hass)
    # Don't actually open sockets in tests.
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    return await make_entry(hass)


def _client(entry: MockConfigEntry):
    return entry.runtime_data.client


async def _ask(hass: HomeAssistant, text: str) -> conversation.ConversationResult:
    entity_id = next(s.entity_id for s in hass.states.async_all("conversation") if "hearth" in s.entity_id)
    return await conversation.async_converse(hass, text, None, Context(user_id="u1"), agent_id=entity_id)


async def test_entity_registered_and_unavailable_when_offline(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    states = hass.states.async_all("conversation")
    assert any("hearth" in s.entity_id for s in states)
    result = await _ask(hass, "hello")
    assert result.response.error_code is not None
    assert "not connected" in result.response.speech["plain"]["speech"]


async def test_forwards_to_hub_when_connected(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client(entry)
    client.connected = True
    client.async_call = AsyncMock(return_value={"text": "Done: created the automation.", "conversation_id": "c1"})
    result = await _ask(hass, "turn on the hallway light at sunset")
    assert result.response.speech["plain"]["speech"] == "Done: created the automation."
    method, params = client.async_call.call_args.args[:2]
    assert method == "chat.process"
    assert params["text"] == "turn on the hallway light at sunset"
    assert params["language"]
    assert result.conversation_id


async def test_hub_error_is_spoken_not_raised(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = _client(entry)
    client.connected = True
    client.async_call = AsyncMock(side_effect=RpcError("timeout", "slow"))
    result = await _ask(hass, "hi")
    assert result.response.error_code is not None
    assert "not connected" in result.response.speech["plain"]["speech"]


async def test_no_agent_in_byo_mode(hass: HomeAssistant) -> None:
    await make_entry(hass, {"assistant_mode": "byo"})
    assert not [s for s in hass.states.async_all("conversation") if "hearth" in s.entity_id]


async def test_model_preference_is_forwarded(hass: HomeAssistant) -> None:
    entry = await make_entry(hass, {"model": "claude-sonnet-5"})
    client = _client(entry)
    client.connected = True
    client.async_call = AsyncMock(return_value={"text": "ok", "conversation_id": "c1"})
    await _ask(hass, "hi")
    assert client.async_call.call_args.args[1]["model"] == "claude-sonnet-5"
