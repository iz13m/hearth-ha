"""The arrangement an admin authored in the Hearth panel (AgDR-0046).

Mostly about what the box refuses to store. The hub clamps an arrangement against what it already
decided, but a refusal here is the only one an admin actually sees, so it has to be the loud one.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai.presentation import ProfileError, async_load, async_save, validate, warnings
from custom_components.hearth_ai.rpc import build_dispatcher


def _entry(core: HomeAssistant) -> MockConfigEntry:
    """One config entry per test. Devices are identified *within* one, so two entities registered
    under two entries never share a device however alike their identifiers look."""
    if (entry := core.data.get("_test_entry")) is None:
        entry = MockConfigEntry(domain="test")
        entry.add_to_hass(core)
        core.data["_test_entry"] = entry
    return entry


def _register(core: HomeAssistant, domain: str, object_id: str, device: str | None = None) -> str:
    """A registry entry plus a state, which is what a real device looks like to the panel."""
    entry = _entry(core)
    device_id = None
    if device is not None:
        device_id = dr.async_get(core).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", device)}, name=device).id
    ent = er.async_get(core).async_get_or_create(
        domain, "test", f"{domain}-{object_id}", suggested_object_id=object_id, device_id=device_id, config_entry=entry
    )
    core.states.async_set(ent.entity_id, "off")
    return ent.entity_id


def _soundbar(core: HomeAssistant) -> dict:
    """The owner's LG soundbar: two entities of one device, and a scene for its power."""
    _register(core, "switch", "soundbar_mute", device="sb")
    _register(core, "sensor", "soundbar_input_format", device="sb")
    core.states.async_set("scene.soundbar_power", "unknown")
    return {
        "version": 1,
        "entities": [],
        "tiles": [
            {
                "id": "t1",
                "name": "Soundbar",
                "icon": "mdi:speaker",
                "area_id": "living_room",
                "primary": "switch.soundbar_mute",
                "members": [
                    {"entity_id": "switch.soundbar_mute", "slot": "main"},
                    {"entity_id": "sensor.soundbar_input_format", "slot": "reading"},
                ],
                "actions": [{"entity_id": "scene.soundbar_power"}],
            }
        ],
    }


async def test_stores_a_tile_and_hands_it_to_the_hub(core: HomeAssistant) -> None:
    profile = validate(core, _soundbar(core))
    await async_save(core, profile)
    out = await build_dispatcher(core).dispatch("presentation.get", {})
    assert out["tiles"][0]["name"] == "Soundbar"
    assert [m["slot"] for m in out["tiles"][0]["members"]] == ["main", "reading"]
    assert out["tiles"][0]["actions"] == [{"entity_id": "scene.soundbar_power"}]


async def test_a_home_that_never_opened_the_panel_has_an_empty_arrangement(core: HomeAssistant) -> None:
    assert await build_dispatcher(core).dispatch("presentation.get", {}) == {"version": 1, "entities": [], "tiles": []}


async def test_reads_back_what_was_saved(core: HomeAssistant) -> None:
    await async_save(core, validate(core, _soundbar(core)))
    assert (await async_load(core))["tiles"][0]["primary"] == "switch.soundbar_mute"


@pytest.mark.parametrize("domain", ["lock", "camera", "alarm_control_panel", "device_tracker"])
async def test_refuses_a_kind_hearth_never_arranges(core: HomeAssistant, domain: str) -> None:
    """A lock or a camera is reached through a grant held by one person; a tile is the household's.

    Refused rather than dropped: an admin who cannot tell the difference between "saved and ignored"
    and "saved" is exactly the person this panel exists for.
    """
    core.states.async_set(f"{domain}.front", "off")
    _register(core, "switch", "lamp", device="d")
    raw = {
        "version": 1,
        "entities": [],
        "tiles": [
            {
                "id": "t1",
                "primary": "switch.lamp",
                "members": [{"entity_id": "switch.lamp", "slot": "main"}, {"entity_id": f"{domain}.front", "slot": "main"}],
            }
        ],
    }
    with pytest.raises(ProfileError, match="never arranges"):
        validate(core, raw)


async def test_refuses_an_entity_this_home_does_not_have(core: HomeAssistant) -> None:
    raw = {"version": 1, "entities": [], "tiles": [{"id": "t1", "primary": "switch.ghost", "members": [{"entity_id": "switch.ghost", "slot": "main"}]}]}
    with pytest.raises(ProfileError, match="not in this Home Assistant"):
        validate(core, raw)


async def test_refuses_a_tile_whose_main_device_is_not_in_it(core: HomeAssistant) -> None:
    _register(core, "switch", "lamp", device="d")
    _register(core, "sensor", "lux", device="d")
    raw = {
        "version": 1,
        "entities": [],
        "tiles": [{"id": "t1", "primary": "sensor.lux", "members": [{"entity_id": "switch.lamp", "slot": "main"}]}],
    }
    with pytest.raises(ProfileError, match="needs one of its own devices"):
        validate(core, raw)


async def test_refuses_the_same_entity_in_two_tiles(core: HomeAssistant) -> None:
    _register(core, "switch", "lamp", device="d")
    _register(core, "sensor", "lux", device="d")
    raw = {
        "version": 1,
        "entities": [],
        "tiles": [
            {"id": "t1", "primary": "switch.lamp", "members": [{"entity_id": "switch.lamp", "slot": "main"}, {"entity_id": "sensor.lux", "slot": "reading"}]},
            {"id": "t2", "primary": "sensor.lux", "members": [{"entity_id": "sensor.lux", "slot": "main"}]},
        ],
    }
    with pytest.raises(ProfileError, match="already in tile t1"):
        validate(core, raw)


async def test_refuses_a_scene_as_a_device_and_a_device_as_an_action(core: HomeAssistant) -> None:
    _register(core, "switch", "lamp", device="d")
    core.states.async_set("scene.evening", "unknown")
    as_member = {
        "version": 1,
        "entities": [],
        "tiles": [{"id": "t1", "primary": "switch.lamp", "members": [{"entity_id": "switch.lamp", "slot": "main"}, {"entity_id": "scene.evening", "slot": "main"}]}],
    }
    with pytest.raises(ProfileError, match="add it as an action"):
        validate(core, as_member)
    as_action = {
        "version": 1,
        "entities": [],
        "tiles": [{"id": "t1", "primary": "switch.lamp", "members": [{"entity_id": "switch.lamp", "slot": "main"}], "actions": [{"entity_id": "switch.lamp"}]}],
    }
    with pytest.raises(ProfileError, match="not a scene or a script"):
        validate(core, as_action)


async def test_refuses_a_placement_or_an_edit_it_does_not_know(core: HomeAssistant) -> None:
    _register(core, "switch", "lamp", device="d")
    with pytest.raises(ProfileError, match="is not a placement"):
        validate(core, {"version": 1, "entities": [{"entity_id": "switch.lamp", "placement": "enormous"}], "tiles": []})
    with pytest.raises(ProfileError, match="not something the app edits"):
        validate(core, {"version": 1, "entities": [{"entity_id": "switch.lamp", "editable": ["colour"]}], "tiles": []})


async def test_drops_a_row_that_says_nothing(core: HomeAssistant) -> None:
    """The panel writes one the moment a dialog opens; keeping them is how a file grows meaninglessly."""
    _register(core, "switch", "lamp", device="d")
    out = validate(core, {"version": 1, "entities": [{"entity_id": "switch.lamp"}], "tiles": []})
    assert out["entities"] == []


async def test_keeps_what_the_app_may_still_edit_in_a_fixed_order(core: HomeAssistant) -> None:
    _register(core, "switch", "lamp", device="d")
    out = validate(core, {"version": 1, "entities": [{"entity_id": "switch.lamp", "editable": ["hide", "name"]}], "tiles": []})
    assert out["entities"][0]["editable"] == ["name", "hide"]


async def test_arranges_an_entity_that_has_a_state_but_no_registry_entry(core: HomeAssistant) -> None:
    """A YAML template or MQTT entity without a unique_id. `entities.list` returns it, so this must too."""
    core.states.async_set("sensor.template_power", "12")
    out = validate(core, {"version": 1, "entities": [{"entity_id": "sensor.template_power", "placement": "read_only"}], "tiles": []})
    assert out["entities"][0]["placement"] == "read_only"


async def test_warns_about_taking_one_gang_of_a_plate(core: HomeAssistant) -> None:
    """Not a refusal: the other gangs still work, they just stay drawn on their own."""
    _register(core, "switch", "plate_l1", device="plate")
    _register(core, "switch", "plate_l2", device="plate")
    _register(core, "switch", "plate_l3", device="plate")
    profile = validate(
        core,
        {"version": 1, "entities": [], "tiles": [{"id": "t1", "primary": "switch.plate_l1", "members": [{"entity_id": "switch.plate_l1", "slot": "main"}]}]},
    )
    assert any("stay on their own" in w for w in warnings(core, profile))
