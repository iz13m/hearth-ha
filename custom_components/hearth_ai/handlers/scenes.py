"""Scene CRUD mirroring homeassistant.components.config.scene."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN, PLATFORM_SCHEMA as SCENE_PLATFORM_SCHEMA
from homeassistant.config import SCENE_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..policy import DENIED_ENTITY_DOMAINS, _entity_ids, find_reference_violations, find_scene_policy_violations
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml
from .registry import _exposed


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCENE_CONFIG_PATH)


def _entity_id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(SCENE_DOMAIN, HOMEASSISTANT_DOMAIN, key)


def _validate(config: dict[str, Any]) -> None:
    if violations := find_scene_policy_violations(config) + find_reference_violations(config, "config"):
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


def _members(hass: HomeAssistant, entity_id: str) -> list[str]:
    """
    The entities a scene sets, as the scene entity itself reports them.

    Home Assistant's own scene platform publishes this as the `entity_id` attribute, which means a
    scene's membership can be inspected without reading scenes.yaml — so it works for scenes we
    cannot edit too. Scenes contributed by other integrations may publish nothing, and for those
    this returns [] and we learn nothing.
    """
    state = hass.states.get(entity_id)
    raw = state.attributes.get("entity_id") if state else None
    if isinstance(raw, str):
        return [raw]
    return [e for e in raw if isinstance(e, str)] if isinstance(raw, list) else []


def find_scene_run_violations(hass: HomeAssistant, entity_id: str, config: dict[str, Any] | None) -> list[str]:
    """
    Why a scene may not be activated. Empty means it may.

    `find_scene_policy_violations` only ever ran over scenes *Hearth writes*, so a scene the owner
    wrote by hand could set a lock and activating it applied that — routing around the promise that
    locks are refused everywhere. Both sources are checked because neither is complete: the config
    is authoritative but only exists for scenes in scenes.yaml, and the attribute covers the rest.
    """
    problems = [
        f"{eid}: scenes that set {eid.split('.', 1)[0]} entities cannot be activated"
        for eid in _members(hass, entity_id)
        if eid.split(".", 1)[0] in DENIED_ENTITY_DOMAINS
    ]
    if config:
        problems += find_scene_policy_violations(config)
    return list(dict.fromkeys(problems))


async def _config_for_entity(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """The scenes.yaml entry behind a scene entity, when there is one."""
    state = hass.states.get(entity_id)
    scene_id = state.attributes.get("id") if state else None
    if not scene_id:
        return None
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    for item in data:
        if isinstance(item, dict) and str(item.get(CONF_ID)) == str(scene_id):
            return plain(item)
    return None


async def scene_run_violations(hass: HomeAssistant, entity_id: str) -> list[str]:
    return find_scene_run_violations(hass, entity_id, await _config_for_entity(hass, entity_id))


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Every mapping inside a config — the same reach the policy walker has."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _nodes(v)


# A target Hearth cannot turn into a list of named scenes. Refused rather than ignored, for the same
# reason a templated action name is (AgDR-0038): what it would activate is unknowable until it runs.
_UNRESOLVABLE_TARGET_KEYS = ("device_id", "area_id", "floor_id", "label_id")


async def find_nested_scene_violations(hass: HomeAssistant, config: Any) -> list[str]:
    """
    Why a script or automation may not be written: it activates a scene that sets a denied entity.

    Scenes are checked when they run (AgDR-0012), and `find_policy_violations` checks the actions a
    config carries — but neither sees through `scene.turn_on`. The action is allowed, its target is
    in `scene`, and what that scene *contains* is only knowable on the box, from the entity's
    membership attribute or scenes.yaml. So a model with `scripts.write` could write
    `scene.turn_on: scene.gate` and let a hand-written scene do the unlocking. This check has no
    mirror on the hub for the same reason: the hub cannot see a scene's contents.

    Async because it reuses `scene_run_violations` unchanged — authoring and activation must never
    disagree about the same scene. Both callers are already coroutines and validate before taking
    the file lock, which is the order to keep.
    """
    problems: list[str] = []
    checked: set[str] = set()
    for node in _nodes(config):
        if node.get("action", node.get("service")) != f"{SCENE_DOMAIN}.turn_on":
            continue
        target = node.get("target") if isinstance(node.get("target"), dict) else {}
        for key in _UNRESOLVABLE_TARGET_KEYS:
            if key in target or key in node:
                problems.append(f"scene.turn_on by {key}: Hearth cannot tell which scenes that would activate")
        for eid in _entity_ids(target) + _entity_ids(node.get("data")) + _entity_ids(node):
            if eid in checked:
                continue
            checked.add(eid)
            # `entity_id: all` is ENTITY_MATCH_ALL — every scene in the house, including one that
            # sets a lock — and a templated id is unknowable. Neither is a named scene.
            if not eid.startswith(f"{SCENE_DOMAIN}."):
                problems.append(f"scene.turn_on {eid}: only a named scene entity may be activated from a config")
                continue
            problems += await scene_run_violations(hass, eid)
    return list(dict.fromkeys(problems))


async def scenes_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, [])
    file_ids = {str(i.get(CONF_ID)) for i in data if isinstance(i, dict) and i.get(CONF_ID)}
    by_id = {str(i.get(CONF_ID)): plain(i) for i in data if isinstance(i, dict) and i.get(CONF_ID)}
    out = []
    for state in sorted(hass.states.async_all(SCENE_DOMAIN), key=lambda s: s.entity_id):
        scene_id = state.attributes.get("id")
        # Listed but not runnable, rather than hidden: the catalog is also how a scene is found in
        # order to *edit* it, and editing a scene the owner never exposed to Assist is fine.
        can_run = _exposed(hass, state.entity_id) and not find_scene_run_violations(
            hass, state.entity_id, by_id.get(str(scene_id)) if scene_id else None
        )
        out.append(
            {
                "id": str(scene_id) if scene_id else state.entity_id,
                "entity_id": state.entity_id,
                "name": state.name,
                "editable": bool(scene_id) and str(scene_id) in file_ids,
                "can_run": can_run,
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
