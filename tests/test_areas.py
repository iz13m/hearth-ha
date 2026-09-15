"""Structure writes (AgDR-0022): floors listed, areas and floors created, an area moved between floors."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, floor_registry as fr

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
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch(method, params)
        assert err.value.code == "method_not_allowed"
    # Reading floors is part of reading entities, which is on by default.
    assert await d.dispatch("floors.list", {}) == []
