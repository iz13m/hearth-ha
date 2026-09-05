"""Script CRUD mirroring homeassistant.components.config.script (key-based scripts.yaml)."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.components.script import DOMAIN as SCRIPT_DOMAIN
from homeassistant.components.script.config import async_validate_config_item
from homeassistant.config import SCRIPT_CONFIG_PATH
from homeassistant.const import SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import slugify

from ..policy import find_policy_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml

_SLUG = re.compile(r"^[a-z0-9_]+$")


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCRIPT_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(SCRIPT_DOMAIN, SCRIPT_DOMAIN, key)


def _check_key(key: str) -> str:
    try:
        return cv.slug(key)
    except vol.Invalid as err:
        raise RpcError("invalid_params", f"script id must be a slug: {err}") from err


async def _validate(hass: HomeAssistant, key: str, config: dict[str, Any]) -> dict[str, Any]:
    if violations := find_policy_violations(config):
        return {"ok": False, "status": "policy", "error": "; ".join(violations)}
    try:
        validated = await async_validate_config_item(hass, key, config)
    except (vol.Invalid, HomeAssistantError) as err:
        return {"ok": False, "status": "failed_schema", "error": str(err)}
    status = getattr(validated, "validation_status", "ok")
    status_s = str(getattr(status, "value", status))
    if status_s != "ok":
        return {"ok": False, "status": status_s, "error": getattr(validated, "validation_error", None) or "invalid"}
    return {"ok": True, "status": "ok"}


async def scripts_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
    file_keys = {str(k) for k in data} if isinstance(data, dict) else set()
    out = []
    for state in sorted(hass.states.async_all(SCRIPT_DOMAIN), key=lambda s: s.entity_id):
        key = state.entity_id.split(".", 1)[1]
        out.append(
            {
                "id": key,
                "entity_id": state.entity_id,
                "alias": state.name,
                "description": None,
                "editable": key in file_keys,
            }
        )
    return out


async def scripts_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = _check_key(require_str(params, "id"))
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
    if isinstance(data, dict) and key in data:
        return {"id": key, "config": plain(data[key])}
    raise RpcError("not_found", f"no editable script with id {key}")


async def scripts_validate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = _check_key(params.get("id") or "validate")
    return await _validate(hass, key, require_config(params))


async def _save(hass: HomeAssistant, key: str, config: dict[str, Any], *, must_exist: bool) -> dict[str, Any]:
    result = await _validate(hass, key, config)
    if not result["ok"]:
        raise RpcError("validation_failed", result.get("error") or "invalid script", {"status": result.get("status")})
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
        if not isinstance(data, dict):
            raise RpcError("ha_error", "scripts.yaml is not a mapping")
        if must_exist and key not in data:
            raise RpcError("not_found", f"no editable script with id {key}")
        data[key] = config
        await write_yaml(hass, path, data)
    await hass.services.async_call(SCRIPT_DOMAIN, SERVICE_RELOAD, blocking=True)
    return {"id": key, "entity_id": _entity_id(hass, key)}


async def scripts_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    config = require_config(params)
    key = params.get("id")
    if not key:
        base = slugify(str(config.get("alias") or "hearth_script")) or "hearth_script"
        key = base
        path = _path(hass)
        async with lock_for(path):
            data = await read_yaml(hass, path, {})
        n = 2
        while key in data or hass.states.get(f"{SCRIPT_DOMAIN}.{key}") is not None:
            key = f"{base}_{n}"
            n += 1
    key = _check_key(str(key))
    return await _save(hass, key, config, must_exist=False)


async def scripts_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, _check_key(require_str(params, "id")), require_config(params), must_exist=True)


async def scripts_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = _check_key(require_str(params, "id"))
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
        if not isinstance(data, dict) or key not in data:
            raise RpcError("not_found", f"no editable script with id {key}")
        data.pop(key)
        await write_yaml(hass, path, data)
    if entity_id := _entity_id(hass, key):
        er.async_get(hass).async_remove(entity_id)
    return {}


def register(d: Dispatcher) -> None:
    d.register("scripts.list", scripts_list)
    d.register("scripts.get", scripts_get)
    d.register("scripts.validate", scripts_validate)
    d.register("scripts.create", scripts_create)
    d.register("scripts.update", scripts_update)
    d.register("scripts.delete", scripts_delete)
