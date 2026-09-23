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

from ..labels import async_mark_hearth_scene, is_hearth_scene
from ..policy import find_policy_violations, find_reference_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml
from .scenes import find_config_violations, find_nested_scene_violations

ORDERED_KEYS = ("alias", "description", "triggers", "trigger", "conditions", "condition", "actions", "action")


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(AUTOMATION_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, key)


async def _validate(hass: HomeAssistant, key: str, config: dict[str, Any]) -> dict[str, Any]:
    # Walked **twice**: once as written, once as Home Assistant will run it (#220).
    #
    # Neither pass subsumes the other, which is why this is not simply reordered. The raw config is
    # the only place a templated action name is still a *string* — `async_validate_config_item`
    # compiles it into a `Template` object the walk would skip, losing AgDR-0042's refusal. And the
    # validated config is the only place a blueprint's body exists at all: a config that is nothing
    # but `{"use_blueprint": {...}}` walks clean raw, while HA expands it into whatever the
    # blueprint does — `shell_command.rm` and `lock.unlock` in the case that prompted this.
    # Checked against the pinned 2026.9.3 rather than assumed.
    if violations := await find_config_violations(hass, config):
        return {"ok": False, "status": "policy", "error": "; ".join(violations)}
    try:
        validated = await async_validate_config_item(hass, key, config)
    except (vol.Invalid, HomeAssistantError) as err:
        return {"ok": False, "status": "failed_schema", "error": str(err)}
    status = getattr(validated, "validation_status", "ok")
    status_s = str(getattr(status, "value", status))
    if status_s != "ok":
        return {"ok": False, "status": status_s, "error": getattr(validated, "validation_error", None) or "invalid"}
    if isinstance(validated, dict) and (violations := await find_config_violations(hass, dict(validated))):
        return {"ok": False, "status": "policy", "error": "; ".join(violations)}
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
                # `Hearth: scene` — this automation is a scene's schedule (AgDR-0044).
                "hearth_scene": is_hearth_scene(hass, state.entity_id),
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


def _hearth_scene(params: dict[str, Any]) -> bool | None:
    """Whether to label this routine a Hearth scene. Absent means leave the label as it is."""
    value = params.get("hearth_scene")
    return bool(value) if value is not None else None


async def _save(
    hass: HomeAssistant, key: str, config: dict[str, Any], *, must_exist: bool, hearth_scene: bool | None = None
) -> dict[str, Any]:
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
    entity_id = _entity_id(hass, key)
    # After the reload, because a new automation has no registry entry until it exists.
    if hearth_scene is not None and entity_id:
        await async_mark_hearth_scene(hass, entity_id, hearth_scene)
    return {"id": key, "entity_id": entity_id}


async def automations_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, uuid.uuid4().hex, require_config(params), must_exist=False, hearth_scene=_hearth_scene(params))


async def automations_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(hass, require_str(params, "id"), require_config(params), must_exist=True, hearth_scene=_hearth_scene(params))


async def automations_set_enabled(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """
    Switch one automation on or off. The **only** place Hearth touches an automation's state.

    It is not a way to run one: `automation.trigger` is in `DENIED_ACTIONS` and always will be
    (AgDR-0042), and `turn_off` is called with `stop_actions: false` so switching a rule off never
    interrupts a sequence already part-way through. A scene's schedule is an automation (AgDR-0044),
    and turning the schedule off without deleting it is the thing a person actually wants.

    Scoped to automations in `automations.yaml`: those are the ones Hearth wrote or could write, and
    an automation from a package or a blueprint elsewhere is the owner's own arrangement.
    """
    key = require_str(params, "id")
    enabled = bool(params.get("enabled"))
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    if not any(isinstance(item, dict) and str(item.get(CONF_ID)) == key for item in data):
        raise RpcError("not_found", f"no editable automation with id {key}")
    entity_id = _entity_id(hass, key)
    if not entity_id:
        raise RpcError("not_found", f"automation {key} has no entity yet")
    await hass.services.async_call(
        AUTOMATION_DOMAIN,
        "turn_on" if enabled else "turn_off",
        {"entity_id": entity_id} if enabled else {"entity_id": entity_id, "stop_actions": False},
        blocking=True,
    )
    state = hass.states.get(entity_id)
    return {"id": key, "entity_id": entity_id, "state": state.state if state else None}


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
    d.register("automations.set_enabled", automations_set_enabled)
