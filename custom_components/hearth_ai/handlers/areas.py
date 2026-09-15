"""Structure handlers: floors, and creating areas and moving them between floors (AgDR-0022).

Opt-in (`areas.manage`) and deliberately narrow. Hearth can add a room or a floor and say which floor
a room is on. It cannot delete or rename either, and it does not move devices: removing a room the
owner made, or relabelling it behind their back, is a different decision from building out a plan.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, floor_registry as fr

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


def register(d: Dispatcher) -> None:
    d.register("floors.list", floors_list)
    d.register("floors.create", floors_create)
    d.register("areas.create", areas_create)
    d.register("areas.set_floor", areas_set_floor)
