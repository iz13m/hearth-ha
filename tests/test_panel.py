"""The Hearth panel's own WebSocket commands (AgDR-0046).

The panel is local to Home Assistant: it reads this home's registries and writes the arrangement, and
never talks to the hub. So what matters here is that it is admin-only, that it refuses what the store
refuses, and that sharing goes through the same gate `entities.expose` does.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai import panel as panel_mod
from custom_components.hearth_ai.presentation import async_load


@pytest.fixture
async def panel(core: HomeAssistant) -> HomeAssistant:
    """Just the commands. The sidebar entry itself needs the frontend package, which tests lack."""
    assert await async_setup_component(core, "websocket_api", {})
    panel_mod._register_commands(core)
    await core.async_block_till_done()
    return core


def _register(core: HomeAssistant, domain: str, object_id: str, device: str | None = None) -> str:
    if (entry := core.data.get("_test_entry")) is None:
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(core)
        core.data["_test_entry"] = entry
    device_id = None
    if device is not None:
        device_id = dr.async_get(core).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", device)}, name=device).id
    ent = er.async_get(core).async_get_or_create(
        domain, "test", f"{domain}-{object_id}", suggested_object_id=object_id, device_id=device_id, config_entry=entry
    )
    core.states.async_set(ent.entity_id, "off")
    return ent.entity_id


async def _send(client, payload: dict[str, Any]) -> dict[str, Any]:
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def test_state_lists_the_home_with_no_state_or_attributes(panel: HomeAssistant, hass_ws_client) -> None:
    """Choosing what to share is half of what the panel is for, so it lists unshared entities too —
    which is exactly why it must not carry what they read."""
    _register(panel, "switch", "soundbar_mute", device="sb")
    client = await hass_ws_client(panel)
    msg = await _send(client, {"type": "hearth_ai/panel/state"})
    assert msg["success"]
    row = next(e for e in msg["result"]["entities"] if e["entity_id"] == "switch.soundbar_mute")
    assert row["device_name"] == "sb"
    assert "state" not in row and "attributes" not in row
    assert msg["result"]["profile"] == {"version": 1, "entities": [], "tiles": []}


async def test_state_lists_an_entity_that_has_no_registry_entry(panel: HomeAssistant, hass_ws_client) -> None:
    """A YAML template or MQTT entity without a unique_id. `entities.list` returns it, so the app can
    already be showing it — a panel that refused to admit it exists would be the confusing half."""
    panel.states.async_set("sensor.template_power", "12", {"friendly_name": "Template power"})
    client = await hass_ws_client(panel)
    msg = await _send(client, {"type": "hearth_ai/panel/state"})
    row = next(e for e in msg["result"]["entities"] if e["entity_id"] == "sensor.template_power")
    assert row["name"] == "Template power"
    assert "state" not in row


async def test_state_leaves_scenes_and_scripts_out_of_the_device_list(panel: HomeAssistant, hass_ws_client) -> None:
    """They are Run rows, not devices, and the store refuses them as tile members."""
    panel.states.async_set("scene.evening", "unknown", {"friendly_name": "Evening"})
    client = await hass_ws_client(panel)
    msg = await _send(client, {"type": "hearth_ai/panel/state"})
    assert not any(e["entity_id"] == "scene.evening" for e in msg["result"]["entities"])
    assert any(r["entity_id"] == "scene.evening" for r in msg["result"]["routines"])


@pytest.mark.parametrize("domain", ["lock", "camera"])
async def test_state_never_lists_a_kind_hearth_may_not_arrange(panel: HomeAssistant, hass_ws_client, domain: str) -> None:
    _register(panel, domain, "front", device="door")
    client = await hass_ws_client(panel)
    msg = await _send(client, {"type": "hearth_ai/panel/state"})
    assert not any(e["entity_id"].startswith(f"{domain}.") for e in msg["result"]["entities"])


async def test_saving_stores_the_arrangement_and_reports_what_is_worth_knowing(panel: HomeAssistant, hass_ws_client) -> None:
    _register(panel, "switch", "soundbar_mute", device="sb")
    _register(panel, "sensor", "soundbar_input_format", device="sb")
    client = await hass_ws_client(panel)
    profile = {
        "version": 1,
        "entities": [],
        "tiles": [
            {
                "id": "t1",
                "name": "Soundbar",
                "primary": "switch.soundbar_mute",
                "members": [
                    {"entity_id": "switch.soundbar_mute", "slot": "main"},
                    {"entity_id": "sensor.soundbar_input_format", "slot": "reading"},
                ],
            }
        ],
    }
    msg = await _send(client, {"type": "hearth_ai/panel/save", "profile": profile})
    assert msg["success"]
    assert msg["result"]["profile"]["tiles"][0]["name"] == "Soundbar"
    assert (await async_load(panel))["tiles"][0]["primary"] == "switch.soundbar_mute"


async def test_saving_refuses_what_the_store_refuses_and_changes_nothing(panel: HomeAssistant, hass_ws_client) -> None:
    client = await hass_ws_client(panel)
    bad = {"version": 1, "entities": [], "tiles": [{"id": "t1", "primary": "switch.ghost", "members": [{"entity_id": "switch.ghost", "slot": "main"}]}]}
    msg = await _send(client, {"type": "hearth_ai/panel/save", "profile": bad})
    assert not msg["success"]
    assert "not in this Home Assistant" in msg["error"]["message"]
    assert (await async_load(panel))["tiles"] == []


async def test_sharing_goes_through_the_same_gate_the_hub_uses(panel: HomeAssistant, hass_ws_client) -> None:
    """A lock is refused here exactly as `entities.expose` refuses it — one helper, one answer."""
    _register(panel, "switch", "lamp", device="d")
    _register(panel, "lock", "front", device="door")
    client = await hass_ws_client(panel)
    msg = await _send(client, {"type": "hearth_ai/panel/expose", "entity_ids": ["switch.lamp", "lock.front"], "expose": True})
    assert msg["success"]
    assert msg["result"]["changed"] == ["switch.lamp"]
    assert [r["entity_id"] for r in msg["result"]["refused"]] == ["lock.front"]
    assert async_should_expose(panel, "conversation", "switch.lamp") is True


@pytest.mark.parametrize("command", ["state", "save", "expose"])
async def test_every_command_is_admin_only(panel: HomeAssistant, hass_ws_client, hass_admin_user, command: str) -> None:
    """The sidebar entry is `require_admin`, but a WebSocket command is reachable without it —
    so each one says so itself rather than relying on the panel not being offered."""
    hass_admin_user.groups = []
    client = await hass_ws_client(panel)
    payloads = {
        "state": {},
        "save": {"profile": {"version": 1, "entities": [], "tiles": []}},
        "expose": {"entity_ids": ["switch.lamp"], "expose": True},
    }
    msg = await _send(client, {"type": f"hearth_ai/panel/{command}", **payloads[command]})
    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"
