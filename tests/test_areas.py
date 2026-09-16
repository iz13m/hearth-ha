"""Structure writes: floors listed, areas and floors created and moved (AgDR-0022), renamed, deleted,
and devices put in rooms (AgDR-0031)."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher


async def test_floors_list_includes_floors_with_no_areas(core: HomeAssistant) -> None:
    floors = fr.async_get(core)
    floors.async_create("Upstairs", level=1)
    floors.async_create("Ground", level=0)
    floors.async_create("Loft")  # no level
    out = await build_dispatcher(core).dispatch("floors.list", {})
    # Storey order, levelled floors first; an empty floor is listed like any other.
    assert [f["name"] for f in out] == ["Ground", "Upstairs", "Loft"]
    assert out[2]["level"] is None


async def test_create_floor_goes_above_the_highest_unless_told(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    first = await d.dispatch("floors.create", {"name": "Ground"})
    assert first["level"] == 0
    attic = await d.dispatch("floors.create", {"name": "  Attic "})
    assert attic == {"floor_id": attic["floor_id"], "name": "Attic", "level": 1}
    cellar = await d.dispatch("floors.create", {"name": "Cellar", "level": -1})
    assert cellar["level"] == -1


async def test_create_area_on_a_floor_and_refuse_duplicates(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    ground = await d.dispatch("floors.create", {"name": "Ground", "level": 0})
    kitchen = await d.dispatch("areas.create", {"name": "Kitchen", "floor_id": ground["floor_id"]})
    assert kitchen["name"] == "Kitchen"
    assert kitchen["floor_id"] == ground["floor_id"]
    assert kitchen["floor_name"] == "Ground"
    assert ar.async_get(core).async_get_area(kitchen["area_id"]) is not None

    with pytest.raises(RpcError) as err:
        await d.dispatch("areas.create", {"name": "kitchen"})
    assert err.value.code == "validation_failed"

    shed = await d.dispatch("areas.create", {"name": "Shed"})
    assert shed["floor_id"] is None


async def test_create_refuses_bad_names_and_unknown_floors(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    for bad in ["", "   ", "Two\nlines", "x" * 65, 3]:
        with pytest.raises(RpcError) as err:
            await d.dispatch("areas.create", {"name": bad})
        assert err.value.code == "invalid_params"
    with pytest.raises(RpcError) as err:
        await d.dispatch("areas.create", {"name": "Den", "floor_id": "nowhere"})
    assert err.value.code == "not_found"
    # Nothing was created by the refused calls.
    assert [a.name for a in ar.async_get(core).async_list_areas()] == []
    with pytest.raises(RpcError) as err:
        await d.dispatch("floors.create", {"name": "Roof", "level": 99})
    assert err.value.code == "invalid_params"


async def test_set_floor_moves_only_the_floor(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    ground = await d.dispatch("floors.create", {"name": "Ground", "level": 0})
    upstairs = await d.dispatch("floors.create", {"name": "Upstairs", "level": 1})
    areas = ar.async_get(core)
    study = areas.async_create("Study", floor_id=ground["floor_id"], icon="mdi:desk", aliases={"office"})

    moved = await d.dispatch("areas.set_floor", {"area_id": study.id, "floor_id": upstairs["floor_id"]})
    assert moved["floor_id"] == upstairs["floor_id"]
    after = areas.async_get_area(study.id)
    assert after is not None
    assert (after.name, after.icon, after.aliases) == ("Study", "mdi:desk", {"office"})

    off = await d.dispatch("areas.set_floor", {"area_id": study.id, "floor_id": None})
    assert off["floor_id"] is None

    for params, code in [
        ({"area_id": "nope", "floor_id": None}, "not_found"),
        ({"area_id": study.id, "floor_id": "nowhere"}, "not_found"),
        ({"area_id": study.id}, "invalid_params"),
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch("areas.set_floor", params)
        assert err.value.code == code


async def test_structure_writes_are_off_until_the_owner_turns_them_on(core: HomeAssistant) -> None:
    from custom_components.hearth_ai.options import HearthOptions

    caps = frozenset(HearthOptions().capabilities)
    assert "areas.manage" not in caps
    d = build_dispatcher(core, caps)
    for method, params in [
        ("areas.create", {"name": "Den"}),
        ("floors.create", {"name": "Roof"}),
        ("areas.set_floor", {"area_id": "x", "floor_id": None}),
        ("areas.update", {"area_id": "x", "name": "Den"}),
        ("areas.delete", {"area_id": "x"}),
        ("floors.update", {"floor_id": "x", "name": "Roof"}),
        ("floors.delete", {"floor_id": "x"}),
        ("areas.assign", {"entity_id": "light.x", "area_id": None}),
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch(method, params)
        assert err.value.code == "method_not_allowed"
    # Reading floors is part of reading entities, which is on by default.
    assert await d.dispatch("floors.list", {}) == []


# --------------------------------------------------------------------------- AgDR-0031


def _plate(core: HomeAssistant, *, area_id: str | None) -> tuple[dr.DeviceEntry, list[str]]:
    """A two-gang switch plate: one device, two entities, as a real integration would register it."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(core)
    device = dr.async_get(core).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "plate-1")}, name="Plate")
    if area_id is not None:
        dr.async_get(core).async_update_device(device.id, area_id=area_id)
    ents = er.async_get(core)
    ids = []
    for gang in ("left", "right"):
        e = ents.async_get_or_create("light", "test", f"plate-{gang}", device_id=device.id, config_entry=entry, suggested_object_id=f"plate_{gang}")
        ids.append(e.entity_id)
    return device, ids


async def test_rename_an_area_and_a_floor_and_refuse_taken_names(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    areas = ar.async_get(core)
    kitchen = areas.async_create("Kitchen", icon="mdi:pot")
    areas.async_create("Study")
    out = await d.dispatch("areas.update", {"area_id": kitchen.id, "name": " Kitchen & dining "})
    assert out["name"] == "Kitchen & dining"
    after = areas.async_get_area(kitchen.id)
    assert after is not None and after.icon == "mdi:pot"  # only the name changed

    with pytest.raises(RpcError) as err:
        await d.dispatch("areas.update", {"area_id": kitchen.id, "name": "study"})
    assert err.value.code == "validation_failed"
    # The refused rename left the area exactly as it was, and the index still works.
    assert areas.async_get_area(kitchen.id).name == "Kitchen & dining"  # type: ignore[union-attr]
    assert areas.async_get_area_by_name("Kitchen & dining") is not None

    ground = fr.async_get(core).async_create("Ground", level=0)
    fr.async_get(core).async_create("Upstairs", level=1)
    assert (await d.dispatch("floors.update", {"floor_id": ground.floor_id, "name": "Ground floor"}))["name"] == "Ground floor"
    with pytest.raises(RpcError) as err:
        await d.dispatch("floors.update", {"floor_id": ground.floor_id, "name": "upstairs"})
    assert err.value.code == "validation_failed"

    for method, params, code in [
        ("areas.update", {"area_id": "nope", "name": "X"}, "not_found"),
        ("floors.update", {"floor_id": "nope", "name": "X"}, "not_found"),
        ("areas.update", {"area_id": kitchen.id, "name": ""}, "invalid_params"),
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch(method, params)
        assert err.value.code == code


async def test_delete_an_area_leaves_its_devices_in_no_area_and_says_how_many(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    hall = ar.async_get(core).async_create("Hall")
    device, (left, right) = _plate(core, area_id=hall.id)
    # One entity carries its own override into the hall too.
    loose = er.async_get(core).async_get_or_create("switch", "test", "loose", suggested_object_id="loose")
    er.async_get(core).async_update_entity(loose.entity_id, area_id=hall.id)

    out = await d.dispatch("areas.delete", {"area_id": hall.id})
    assert out == {"area_id": hall.id, "devices_unassigned": 1, "entities_unassigned": 1}
    assert ar.async_get(core).async_get_area(hall.id) is None
    # Home Assistant's cascade: nulled, never removed.
    assert dr.async_get(core).async_get(device.id).area_id is None  # type: ignore[union-attr]
    assert er.async_get(core).async_get(left) is not None
    assert er.async_get(core).async_get(loose.entity_id).area_id is None  # type: ignore[union-attr]

    with pytest.raises(RpcError) as err:
        await d.dispatch("areas.delete", {"area_id": hall.id})
    assert err.value.code == "not_found"


async def test_delete_a_floor_keeps_its_areas_on_no_floor_before_it_returns(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    upstairs = fr.async_get(core).async_create("Upstairs", level=1)
    areas = ar.async_get(core)
    bed = areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    bath = areas.async_create("Bathroom", floor_id=upstairs.floor_id)
    areas.async_create("Kitchen")

    out = await d.dispatch("floors.delete", {"floor_id": upstairs.floor_id})
    assert out == {"floor_id": upstairs.floor_id, "areas_unassigned": 2}
    # True immediately, not once Home Assistant's own listener gets round to it.
    assert areas.async_get_area(bed.id).floor_id is None  # type: ignore[union-attr]
    assert areas.async_get_area(bath.id).floor_id is None  # type: ignore[union-attr]
    assert fr.async_get(core).async_get_floor(upstairs.floor_id) is None
    assert await d.dispatch("floors.list", {}) == []


async def test_assign_moves_the_whole_device_so_a_plate_stays_together(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    areas = ar.async_get(core)
    hall = areas.async_create("Hall")
    kitchen = areas.async_create("Kitchen")
    device, (left, right) = _plate(core, area_id=hall.id)

    out = await d.dispatch("areas.assign", {"entity_id": left, "area_id": kitchen.id})
    assert out == {"entity_id": left, "area_id": kitchen.id, "scope": "device", "moved": sorted([left, right])}
    assert dr.async_get(core).async_get(device.id).area_id == kitchen.id  # type: ignore[union-attr]

    # And what Hearth lists agrees with Home Assistant for both gangs. Exposed first: `entities.list`
    # only returns what Assist can see, and without this the check would pass by listing nothing.
    # It also walks live states, so the gangs need one.
    for gang in (left, right):
        core.states.async_set(gang, "off")
        async_expose_entity(core, "conversation", gang, True)
    listed = {e["entity_id"]: e["area_id"] for e in await d.dispatch("entities.list", {"limit": 500})}
    assert listed[left] == kitchen.id
    assert listed[right] == kitchen.id


async def test_assign_clears_the_tapped_entitys_own_override_so_it_follows(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    areas = ar.async_get(core)
    hall, kitchen, study = areas.async_create("Hall"), areas.async_create("Kitchen"), areas.async_create("Study")
    device, (left, right) = _plate(core, area_id=hall.id)
    ents = er.async_get(core)
    ents.async_update_entity(left, area_id=study.id)   # the tapped one, pinned elsewhere
    ents.async_update_entity(right, area_id=study.id)  # a sibling someone pinned on purpose

    out = await d.dispatch("areas.assign", {"entity_id": left, "area_id": kitchen.id})
    assert out["area_id"] == kitchen.id
    assert ents.async_get(left).area_id is None  # type: ignore[union-attr]
    # The sibling keeps its deliberate pin, and is not reported as moved.
    assert ents.async_get(right).area_id == study.id  # type: ignore[union-attr]
    assert out["moved"] == [left]


async def test_assign_an_entity_with_no_device_uses_its_own_override(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    den = ar.async_get(core).async_create("Den")
    helper = er.async_get(core).async_get_or_create("switch", "test", "helper", suggested_object_id="helper")

    out = await d.dispatch("areas.assign", {"entity_id": helper.entity_id, "area_id": den.id})
    assert out == {"entity_id": helper.entity_id, "area_id": den.id, "scope": "entity", "moved": [helper.entity_id]}
    off = await d.dispatch("areas.assign", {"entity_id": helper.entity_id, "area_id": None})
    assert off["area_id"] is None and off["moved"] == [helper.entity_id]

    for params, code in [
        ({"entity_id": helper.entity_id, "area_id": "nowhere"}, "not_found"),
        ({"entity_id": helper.entity_id}, "invalid_params"),
        ({"entity_id": "switch.not_registered", "area_id": den.id}, "not_editable"),
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch("areas.assign", params)
        assert err.value.code == code


async def test_an_entity_on_a_child_device_is_listed_in_its_parents_area(core: HomeAssistant) -> None:
    """A child device with no area of its own inherits its parent's, as Home Assistant says.

    Hearth used to read `device.area_id` alone and list such an entity in no room while Home Assistant
    had it in one. Moving devices between rooms would have made that gap visible.
    """
    d = build_dispatcher(core)
    garage = ar.async_get(core).async_create("Garage")
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(core)
    devices = dr.async_get(core)
    hub = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "hub")}, name="Hub")
    devices.async_update_device(hub.id, area_id=garage.id)
    child = devices.async_get_or_create_child(config_entry_id=entry.entry_id, identifiers={("test", "sensor")}, parent_device_id=hub.id)
    ent = er.async_get(core).async_get_or_create("switch", "test", "child", device_id=child.id, config_entry=entry, suggested_object_id="child")
    core.states.async_set(ent.entity_id, "off")
    async_expose_entity(core, "conversation", ent.entity_id, True)

    listed = {e["entity_id"]: e["area_id"] for e in await d.dispatch("entities.list", {"limit": 500})}
    assert listed[ent.entity_id] == garage.id
    assert er.async_get_effective_area_id(core, ent) == garage.id
