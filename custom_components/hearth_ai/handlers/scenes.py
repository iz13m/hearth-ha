"""Scene CRUD mirroring homeassistant.components.config.scene."""

from __future__ import annotations

from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN, PLATFORM_SCHEMA as SCENE_PLATFORM_SCHEMA
from homeassistant.config import SCENE_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..policy import find_scene_policy_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCENE_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(SCENE_DOMAIN, HOMEASSISTANT_DOMAIN, key)


def _validate(config: dict[str, Any]) -> None:
    if violations := find_scene_policy_violations(config):
        raise RpcError("validation_failed", "policy: " + "; ".join(violations))
    try:
        SCENE_PLATFORM_SCHEMA(config)
    except vol.Invalid as err:
        raise RpcError("validation_failed", str(err)) from err


def _write_value(data: list[dict[str, Any]], key: str, new_value: dict[str, Any]) -> None:
    updated_value: dict[str, Any] = {CONF_ID: key}
    for k in ("name", "entities"):
        if k in new_value:
            updated_value[k] = new_value[k]
    updated_value.update(new_value)
    updated_value[CONF_ID] = key
    updated = False
    for index, cur in enumerate(data):
        if CONF_ID not in cur:
            cur[CONF_ID] = uuid.uuid4().hex
        elif cur[CONF_ID] == key:
            data[index] = updated_value
            updated = True
    if not updated:
        data.append(updated_value)


async def scenes_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    file_ids = {str(i.get(CONF_ID)) for i in data if isinstance(i, dict) and i.get(CONF_ID)}
    out = []
    for state in sorted(hass.states.async_all(SCENE_DOMAIN), key=lambda s: s.entity_id):
        scene_id = state.attributes.get("id")
        out.append(
            {
                "id": str(scene_id) if scene_id else state.entity_id,
                "entity_id": state.entity_id,
                "name": state.name,
                "editable": bool(scene_id) and str(scene_id) in file_ids,
            }
        )
    return out


async def scenes_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = require_str(params, "id")
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    for item in data:
        if isinstance(item, dict) and str(item.get(CONF_ID)) == key:
            return {"id": key, "config": plain(item)}
    raise RpcError("not_found", f"no editable scene with id {key}")


async def _save(hass: HomeAssistant, key: str, config: dict[str, Any], *, must_exist: bool) -> dict[str, Any]:
    config.pop(CONF_ID, None)
    _validate({CONF_ID: key, **config})
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
        exists = any(isinstance(i, dict) and str(i.get(CONF_ID)) == key for i in data)
        if must_exist and not exists:
            raise RpcError("not_found", f"no editable scene with id {key}")
        _write_value(data, key, config)
        await write_yaml(hass, path, data)
    await hass.services.async_call(SCENE_DOMAIN, SERVICE_RELOAD, blocking=True)
    return {"id": key, "entity_id": _entity_id(hass, key)}


async def scenes_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, uuid.uuid4().hex, require_config(params), must_exist=False)


async def scenes_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, require_str(params, "id"), require_config(params), must_exist=True)


async def scenes_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = require_str(params, "id")
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
        idx = next((i for i, v in enumerate(data) if isinstance(v, dict) and str(v.get(CONF_ID)) == key), None)
        if idx is None:
            raise RpcError("not_found", f"no editable scene with id {key}")
        data.pop(idx)
        await write_yaml(hass, path, data)
    if entity_id := _entity_id(hass, key):
        er.async_get(hass).async_remove(entity_id)
    return {}


def register(d: Dispatcher) -> None:
    d.register("scenes.list", scenes_list)
    d.register("scenes.get", scenes_get)
    d.register("scenes.create", scenes_create)
    d.register("scenes.update", scenes_update)
    d.register("scenes.delete", scenes_delete)
