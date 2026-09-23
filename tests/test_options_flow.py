"""Options flow: connection, capabilities, assistant; saving reloads with the new settings."""

from __future__ import annotations

import json
import pathlib

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.const import DOMAIN
from custom_components.hearth_ai.options import HearthOptions
from custom_components.hearth_ai.rpc import RpcError

from .test_conversation import ENTRY_DATA


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    e = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA)
    e.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


async def test_menu_and_capabilities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"connection", "capabilities", "assistant"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "capabilities"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "capabilities"
    keys = {str(k) for k in result["data_schema"].schema}
    assert keys == {
        "cap_entities_read",
        "cap_entities_subscribe",
        "cap_entities_history",
        "cap_helpers_manage",
        "cap_labels_manage",
        "cap_notify_send",
        "cap_entities_manage",
        "cap_access_control",
        "cap_vision_view",
        "cap_vision_live",
        "cap_services_read",
        "cap_automations_read",
        "cap_automations_write",
        "cap_scenes_read",
        "cap_scenes_write",
        "cap_scripts_read",
        "cap_scripts_write",
        "cap_devices_control",
        "cap_routines_run",
        "cap_integrations_manage",
        "cap_areas_manage",
    }
    # Operating the home is off until the owner opts in; everything else defaults on.
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["cap_devices_control"] is False
    assert defaults["cap_routines_run"] is False
    assert defaults["cap_integrations_manage"] is False
    assert defaults["cap_areas_manage"] is False
    assert defaults["cap_entities_read"] is True
    # Pushing state grants no access polling did not already have, so it is on like the other reads.
    assert defaults["cap_entities_subscribe"] is True
    # Reading the past is only a read and still starts off: a month of it is a pattern, not a state.
    assert defaults["cap_entities_history"] is False
    assert defaults["cap_helpers_manage"] is False
    assert defaults["cap_labels_manage"] is False
    assert defaults["cap_notify_send"] is False

    with patch("custom_components.hearth_ai.HearthClient.start") as start:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {k: (k != "cap_automations_write") for k in keys}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cap_automations_write"] is False
    # entry reloaded: new client announces the reduced set, dispatcher gates it
    assert start.called
    caps = entry.runtime_data.client.capabilities
    assert "automations.write" not in caps and "automations.read" in caps and "conversation" in caps
    assert entry.runtime_data.client._dispatcher.capabilities == frozenset(caps)
    with pytest.raises(RpcError):
        await entry.runtime_data.client._dispatcher.dispatch("automations.create", {"config": {}})


async def test_connection_toggle(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "connection"})
    assert result["description_placeholders"]["status"] == "disconnected"
    with patch("custom_components.hearth_ai.HearthClient.start") as start:
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"connection_enabled": False})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not start.called  # disabled: never dials out
    assert HearthOptions.from_entry(entry).connection_enabled is False


async def test_assistant_step_live_and_offline(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = entry.runtime_data.client
    client.connected = True
    client.async_call = AsyncMock(
        return_value={"plan": "family", "managed": True, "active": True, "status": "active", "period_end": "2026-10-04T00:00:00Z", "models": ["claude-opus-5", "claude-sonnet-5"], "default_model": "claude-opus-5", "dashboard_url": "https://h/app", "mcp_url": "https://h/mcp"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "assistant"})
    assert result["type"] is FlowResultType.FORM
    ph = result["description_placeholders"]
    assert ph["plan"] == "family" and ph["active"] == "yes" and ph["period_end"] == "2026-10-04" and ph["models_source"] == "live"
    assert ph["mcp_url"] == "https://h/mcp"

    # the model selector only offers what the hub allows (an unknown value is rejected by the schema itself)
    model_field = next(k for k in result["data_schema"].schema if str(k) == "model")
    offered = [o["value"] for o in result["data_schema"].schema[model_field].config["options"]]
    assert offered == ["", "claude-opus-5", "claude-sonnet-5"]
    with patch("custom_components.hearth_ai.HearthClient.start"):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"assistant_mode": "byo", "model": ""})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["assistant_mode"] == "byo"
    # byo: conversation agent removed, capabilities no longer include conversation
    assert not [s for s in hass.states.async_all("conversation") if "hearth" in s.entity_id]
    assert "conversation" not in entry.runtime_data.client.capabilities

    # offline hub -> static defaults
    entry.runtime_data.client.connected = False
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "assistant"})
    assert result["description_placeholders"]["models_source"] == "offline defaults"
    assert result["description_placeholders"]["plan"] == "unknown"

    # hub error -> static defaults too
    entry.runtime_data.client.connected = True
    entry.runtime_data.client.async_call = AsyncMock(side_effect=RpcError("timeout", "slow"))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "assistant"})
    assert result["description_placeholders"]["models_source"] == "offline defaults"


def test_assistant_description_has_no_literal_escapes() -> None:
    """A `\\n` that survived JSON escaping renders to the customer as visible backslash-n.

    The description is the one long piece of prose the options flow shows, and it is written in two
    files that must agree. Nothing asserted its text until a review caught exactly that escape.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "hearth_ai"
    texts = []
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        text = data["options"]["step"]["assistant"]["description"]
        assert "\\n" not in text, f"{name}: literal backslash-n in the description"
        assert "\n\n" in text, f"{name}: the paragraph break was lost"
        assert "{plan}" in text and "{tier}" not in text, f"{name}: still names the old tier"
        texts.append(text)
    assert texts[0] == texts[1], "strings.json and translations/en.json disagree"
