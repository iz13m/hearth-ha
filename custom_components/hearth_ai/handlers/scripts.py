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

from ..labels import async_mark_hearth_scene, is_hearth_scene
from ..policy import find_policy_violations, find_reference_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml
from .registry import _exposed
from .scenes import find_nested_scene_violations

_SLUG = re.compile(r"^[a-z0-9_]+$")


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCRIPT_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(SCRIPT_DOMAIN, SCRIPT_DOMAIN, key)


def _hearth_scene(params: dict[str, Any]) -> bool | None:
    """Whether to label this routine a Hearth scene. Absent means leave the label as it is."""
    value = params.get("hearth_scene")
    return bool(value) if value is not None else None


def _check_key(key: str) -> str:
    try:
        return cv.slug(key)
    except vol.Invalid as err:
        raise RpcError("invalid_params", f"script id must be a slug: {err}") from err


async def _validate(hass: HomeAssistant, key: str, config: dict[str, Any]) -> dict[str, Any]:
    if violations := find_policy_violations(config) + find_reference_violations(config, "config"):
        return {"ok": False, "status": "policy", "error": "; ".join(violations)}
    if violations := await find_nested_scene_violations(hass, config):
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
                # Listed either way: an un-exposed script can still be read and edited, just not run.
                "can_run": _exposed(hass, state.entity_id),
                # `Hearth: scene` — this script is a scene with steps or a schedule (AgDR-0044).
                "hearth_scene": is_hearth_scene(hass, state.entity_id),
            }
        )
    return out


async def script_run_violations(hass: HomeAssistant, entity_id: str) -> list[str]:
    """
    Why a script Hearth presents as a scene may not run. Empty means it may.

    The run-time half of #126, which that change deliberately left to this one. A scene is checked
    when it runs (AgDR-0012); a script is not (AgDR-0005). So a scene stored as a labelled script
    would have been the one way to spend an unchecked run on scene-shaped contents — the same hole
    `find_nested_scene_violations` closed at *authoring* time, from the other end.

    **Bounded by what can be read.** A script in `scripts.yaml` is checked whole. A script the owner
    wrote somewhere Hearth cannot read — a package, a blueprint — has no config here, so nothing is
    found and it runs exactly as AgDR-0005 always let a script run. That is not a new hole: labelling
    such a script grants it nothing it could not already do through `scripts.run`.
    """
    key = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
    config = plain(data[key]) if isinstance(data, dict) and key in data else None
    if config is None:
        return []
    problems = find_policy_violations(config) + find_reference_violations(config, "config")
    problems += await find_nested_scene_violations(hass, config)
    return list(dict.fromkeys(problems))


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


async def _save(
    hass: HomeAssistant, key: str, config: dict[str, Any], *, must_exist: bool, hearth_scene: bool | None = None
) -> dict[str, Any]:
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
    entity_id = _entity_id(hass, key)
    # After the reload, because a new script has no registry entry until it exists.
    if hearth_scene is not None and entity_id:
        await async_mark_hearth_scene(hass, entity_id, hearth_scene)
    return {"id": key, "entity_id": entity_id}


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
    return await _save(hass, key, config, must_exist=False, hearth_scene=_hearth_scene(params))


async def scripts_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _save(
        hass, _check_key(require_str(params, "id")), require_config(params), must_exist=True, hearth_scene=_hearth_scene(params)
    )


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
