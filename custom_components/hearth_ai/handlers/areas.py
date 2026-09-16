"""Structure handlers: floors and areas in Home Assistant, from the Hearth app.

Opt-in (`areas.manage`), admin-only on the hub, and never offered to a model. AgDR-0022 made this
create-and-move only. AgDR-0031 added renaming and deleting rooms and floors and putting a device in
a room, because each of those was otherwise a trip into Home Assistant for the one person the app is
meant to spare it.

Every write here changes Home Assistant itself — what automations, dashboards and Assist see — not
Hearth's own display names (AgDR-0021). Deleting does what Home Assistant does: it never refuses a
room with things in it, it leaves them in no room. The counts returned say how many.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from ..rpc import Dispatcher, RpcError
from .registry import area_dto

MAX_NAME = 64
MIN_LEVEL = -4
MAX_LEVEL = 12


def _name(params: dict[str, Any]) -> str:
    v = params.get("name")
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > MAX_NAME or "\n" in v or "\r" in v:
        raise RpcError("invalid_params", f"name must be one line of 1-{MAX_NAME} characters")
    return v.strip()


def _floor_id(params: dict[str, Any], floors: fr.FloorRegistry, *, required: bool) -> str | None:
    if "floor_id" not in params:
        if required:
            raise RpcError("invalid_params", "floor_id is required (null for no floor)")
        return None
    v = params["floor_id"]
    if v is None:
        return None
    if not isinstance(v, str) or not v:
        raise RpcError("invalid_params", "floor_id must be a string or null")
    if floors.async_get_floor(v) is None:
        raise RpcError("not_found", f"no floor {v}")
    return v


def floor_dto(floor: fr.FloorEntry) -> dict[str, Any]:
    return {"floor_id": floor.floor_id, "name": floor.name, "level": floor.level if isinstance(floor.level, int) else None}


async def floors_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every floor, including floors no area is on yet. Storey order first, then name."""
    floors = fr.async_get(hass).async_list_floors()
    ordered = sorted(floors, key=lambda f: (f.level is None, f.level if isinstance(f.level, int) else 0, f.name.lower()))
    return [floor_dto(f) for f in ordered]


async def floors_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    name = _name(params)
    reg = fr.async_get(hass)
    level = params.get("level")
    if level is None:
        # "Above the highest": a new floor is most often a storey nobody had mapped yet on top.
        levels = [f.level for f in reg.async_list_floors() if isinstance(f.level, int)]
        level = min(max(levels) + 1, MAX_LEVEL) if levels else 0
    elif not isinstance(level, int) or isinstance(level, bool) or not MIN_LEVEL <= level <= MAX_LEVEL:
        raise RpcError("invalid_params", f"level must be an integer {MIN_LEVEL}..{MAX_LEVEL}")
    try:
        floor = reg.async_create(name, level=level)
    except ValueError as err:
        # Home Assistant refuses a name already in use (compared normalised, so "Attic" == "attic").
        raise RpcError("validation_failed", str(err)) from err
    return floor_dto(floor)


async def areas_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    name = _name(params)
    floors = fr.async_get(hass)
    floor_id = _floor_id(params, floors, required=False)
    try:
        area = ar.async_get(hass).async_create(name, floor_id=floor_id)
    except ValueError as err:
        raise RpcError("validation_failed", str(err)) from err
    return area_dto(area, floors)


async def areas_set_floor(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    area_id = params.get("area_id")
    if not isinstance(area_id, str) or not area_id:
        raise RpcError("invalid_params", "area_id must be a non-empty string")
    floors = fr.async_get(hass)
    floor_id = _floor_id(params, floors, required=True)
    reg = ar.async_get(hass)
    if reg.async_get_area(area_id) is None:
        raise RpcError("not_found", f"no area {area_id}")
    # Only the floor: every other field keeps its UNDEFINED default, so nothing else is touched.
    area = reg.async_update(area_id, floor_id=floor_id)
    return area_dto(area, floors)


def _id(params: dict[str, Any], key: str) -> str:
    v = params.get(key)
    if not isinstance(v, str) or not v:
        raise RpcError("invalid_params", f"{key} must be a non-empty string")
    return v


async def areas_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Rename an area. Only the name: every other field keeps its UNDEFINED default."""
    area_id = _id(params, "area_id")
    name = _name(params)
    reg = ar.async_get(hass)
    if reg.async_get_area(area_id) is None:
        raise RpcError("not_found", f"no area {area_id}")
    try:
        # `async_update` has no explicit duplicate check, unlike `async_create`; the normalised-name
        # index raises the same ValueError before the entry is replaced, so the store is untouched.
        area = reg.async_update(area_id, name=name)
    except ValueError as err:
        raise RpcError("validation_failed", str(err)) from err
    return area_dto(area, fr.async_get(hass))


async def areas_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Delete an area, leaving whatever was in it in no area — Home Assistant's own behaviour.

    Counted before the delete, because afterwards there is nothing left to count by. Devices are
    counted by their *own* area (child devices inheriting a parent's are not moved by this), and
    entities by their own override: those are exactly the rows Home Assistant's cascade clears.
    """
    area_id = _id(params, "area_id")
    reg = ar.async_get(hass)
    if reg.async_get_area(area_id) is None:
        # `async_delete` would raise KeyError on an unknown id.
        raise RpcError("not_found", f"no area {area_id}")
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    devices = sum(1 for dev in dr.async_entries_for_area(dev_reg, area_id) if dev.area_id == area_id)
    entities = len(er.async_entries_for_area(ent_reg, area_id))
    reg.async_delete(area_id)
    return {"area_id": area_id, "devices_unassigned": devices, "entities_unassigned": entities}


async def floors_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    floor_id = _id(params, "floor_id")
    name = _name(params)
    reg = fr.async_get(hass)
    if reg.async_get_floor(floor_id) is None:
        raise RpcError("not_found", f"no floor {floor_id}")
    try:
        floor = reg.async_update(floor_id, name=name)
    except ValueError as err:
        raise RpcError("validation_failed", str(err)) from err
    return floor_dto(floor)


async def floors_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Delete a floor. Its areas are kept, on no floor.

    Home Assistant clears the areas itself, but from an event listener. The areas are cleared here
    first, explicitly, so the result is true by the time it is returned and a listing the hub makes
    straight afterwards cannot still show them on a floor that no longer exists.
    """
    floor_id = _id(params, "floor_id")
    floors = fr.async_get(hass)
    if floors.async_get_floor(floor_id) is None:
        raise RpcError("not_found", f"no floor {floor_id}")
    areas = ar.async_get(hass)
    on_floor = [a.id for a in areas.async_list_areas() if a.floor_id == floor_id]
    for area_id in on_floor:
        areas.async_update(area_id, floor_id=None)
    floors.async_delete(floor_id)
    return {"floor_id": floor_id, "areas_unassigned": len(on_floor)}


async def areas_assign(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Put a device in a room: its device when it has one, the entity on its own when it does not.

    "This switch is in the kitchen" is a statement about the thing on the wall, so the device moves,
    and every entity on it with it — all gangs of a plate together, as Home Assistant's own UI does.
    The tapped entity's own override is cleared so it follows its device; siblings that someone
    deliberately pinned elsewhere keep their pin. An entity with no device (a helper, a YAML entity)
    has nothing else to move, so it gets the override.
    """
    entity_id = _id(params, "entity_id")
    if "area_id" not in params:
        raise RpcError("invalid_params", "area_id is required (null for no room)")
    area_id = params["area_id"]
    if area_id is not None and (not isinstance(area_id, str) or not area_id):
        raise RpcError("invalid_params", "area_id must be a string or null")
    if area_id is not None and ar.async_get(hass).async_get_area(area_id) is None:
        raise RpcError("not_found", f"no area {area_id}")

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry is None:
        # A registry-less entity (YAML without a unique_id) has nowhere Home Assistant keeps an area.
        raise RpcError("not_editable", f"{entity_id} has no registry entry, so Home Assistant cannot give it a room")

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(entry.device_id) if entry.device_id else None
    affected = er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True) if device else [entry]
    before = {e.entity_id: er.async_get_effective_area_id(hass, e) for e in affected}

    if device is not None:
        if isinstance(device, dr.ChildDeviceEntry):
            dev_reg.async_update_child_device(device.id, area_id=area_id)
        else:
            dev_reg.async_update_device(device.id, area_id=area_id)
        if entry.area_id is not None:
            ent_reg.async_update_entity(entity_id, area_id=None)
        scope = "device"
    else:
        ent_reg.async_update_entity(entity_id, area_id=area_id)
        scope = "entity"

    moved = []
    for e in affected:
        fresh = ent_reg.async_get(e.entity_id)
        if fresh is not None and er.async_get_effective_area_id(hass, fresh) != before[e.entity_id]:
            moved.append(e.entity_id)
    final = ent_reg.async_get(entity_id)
    return {
        "entity_id": entity_id,
        "area_id": er.async_get_effective_area_id(hass, final) if final else None,
        "scope": scope,
        "moved": sorted(moved),
    }


def register(d: Dispatcher) -> None:
    d.register("floors.list", floors_list)
    d.register("floors.create", floors_create)
    d.register("areas.create", areas_create)
    d.register("areas.set_floor", areas_set_floor)
    d.register("areas.update", areas_update)
    d.register("areas.delete", areas_delete)
    d.register("floors.update", floors_update)
    d.register("floors.delete", floors_delete)
    d.register("areas.assign", areas_assign)
