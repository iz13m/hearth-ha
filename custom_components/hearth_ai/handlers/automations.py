"""Automation CRUD mirroring homeassistant.components.config.automation (the UI editor)."""

from __future__ import annotations

from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..policy import find_policy_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml

ORDERED_KEYS = ("alias", "description", "triggers", "trigger", "conditions", "condition", "actions", "action")


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(AUTOMATION_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, key)


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


def _write_value(data: list[dict[str, Any]], key: str, new_value: dict[str, Any]) -> None:
    """Verbatim port of EditAutomationConfigView._write_value."""
    updated_value: dict[str, Any] = {CONF_ID: key}
    for k in ORDERED_KEYS:
        if k in new_value:
            updated_value[k] = new_value[k]
    updated_value.update(new_value)
    updated_value[CONF_ID] = key  # the id is ours, never the caller's
    updated = False
    for index, cur in enumerate(data):
        if CONF_ID not in cur:
            cur[CONF_ID] = uuid.uuid4().hex
        elif cur[CONF_ID] == key:
            data[index] = updated_value
            updated = True
    if not updated:
        data.append(updated_value)


async def automations_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    file_ids = {str(item.get(CONF_ID)) for item in data if isinstance(item, dict) and item.get(CONF_ID)}
    out: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(AUTOMATION_DOMAIN), key=lambda s: s.entity_id):
        auto_id = state.attributes.get("id")
        out.append(
            {
                "id": str(auto_id) if auto_id else state.entity_id,
                "entity_id": state.entity_id,
                "alias": state.name,
                "description": None,
                "state": state.state,
                "last_triggered": (lt.isoformat() if (lt := state.attributes.get("last_triggered")) else None),
                "editable": bool(auto_id) and str(auto_id) in file_ids,
            }
        )
    return out


async def automations_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = require_str(params, "id")
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    for item in data:
        if isinstance(item, dict) and str(item.get(CONF_ID)) == key:
            return {"id": key, "config": plain(item)}
    raise RpcError("not_found", f"no editable automation with id {key}")


async def automations_validate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    config = require_config(params)
    config.pop(CONF_ID, None)
    return await _validate(hass, "validate", config)


async def _save(hass: HomeAssistant, key: str, config: dict[str, Any], *, must_exist: bool) -> dict[str, Any]:
    config.pop(CONF_ID, None)
    result = await _validate(hass, key, config)
    if not result["ok"]:
        raise RpcError("validation_failed", result.get("error") or "invalid automation", {"status": result.get("status")})
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
        exists = any(isinstance(i, dict) and str(i.get(CONF_ID)) == key for i in data)
        if must_exist and not exists:
            raise RpcError("not_found", f"no editable automation with id {key}")
        _write_value(data, key, config)
        await write_yaml(hass, path, data)
    await hass.services.async_call(AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: key}, blocking=True)
    return {"id": key, "entity_id": _entity_id(hass, key)}


async def automations_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, uuid.uuid4().hex, require_config(params), must_exist=False)


async def automations_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, require_str(params, "id"), require_config(params), must_exist=True)


async def automations_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    key = require_str(params, "id")
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
        idx = next((i for i, v in enumerate(data) if isinstance(v, dict) and str(v.get(CONF_ID)) == key), None)
        if idx is None:
            raise RpcError("not_found", f"no editable automation with id {key}")
        data.pop(idx)
        await write_yaml(hass, path, data)
    ent_reg = er.async_get(hass)
    if entity_id := _entity_id(hass, key):
        ent_reg.async_remove(entity_id)
    return {}


def register(d: Dispatcher) -> None:
    d.register("automations.list", automations_list)
    d.register("automations.get", automations_get)
    d.register("automations.validate", automations_validate)
    d.register("automations.create", automations_create)
    d.register("automations.update", automations_update)
    d.register("automations.delete", automations_delete)
