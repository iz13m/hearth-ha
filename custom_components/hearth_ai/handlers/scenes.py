"""Scene CRUD mirroring homeassistant.components.config.scene."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN, PLATFORM_SCHEMA as SCENE_PLATFORM_SCHEMA
from homeassistant.components.script import DOMAIN as SCRIPT_DOMAIN_
from homeassistant.config import SCENE_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..policy import TARGET_BEARING_KEYS, _entity_ids, _fold, _service_name, DENIED_ENTITY_DOMAINS, find_policy_violations, find_reference_violations, find_scene_policy_violations
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


async def find_config_violations(hass: HomeAssistant, config: Any) -> list[str]:
    """Every policy check a written automation or script must pass, over one config.

    Kept here because `automations.py` and `scripts.py` must ask exactly the same question — the
    pair of `_validate` functions drifting is the shape of bug this module already exists to stop.
    """
    return (
        find_policy_violations(config)
        + find_reference_violations(config, "config")
        + await find_nested_scene_violations(hass, config)
        + await find_nested_script_violations(hass, config)
    )


# Starting a script is every `script.*` service except these two, plus the per-script service Home
# Assistant registers for each script as `script.<its id>` — which is why this is keyed on the
# domain rather than a list of service names (#220's `fansOut` derivation, same reasoning).
_SCRIPT_NOT_STARTING = ("reload", "turn_off")


def _script_raw_config(hass: HomeAssistant, entity_id: str) -> Any:
    """The config Home Assistant holds for a loaded script, whatever file defined it.

    **The running instance is the authority; a file is a partial view of it.** Reading
    `scripts.yaml` would have refused every script an owner keeps in `packages/` or defines inline
    in `configuration.yaml` — Hearth cannot read those files, but HA has already parsed them and
    keeps each script's config on the entity. Verified on a running instance rather than assumed:
    `raw_config` is populated identically for a `scripts.yaml` script and an inline one.

    This is consistency with an existing design rather than a departure: `_members` above reads a
    scene's membership off the entity for the same reason. The difference is that a scene's
    attribute gives membership *only*, so that resolver needs `scenes.yaml` as well — `raw_config`
    is the whole config, so this one needs nothing else.

    `raw_config` is also **blueprint-expanded** — a `use_blueprint` script's config holds the
    substituted sequence rather than the reference, so unlike #226's automation path there is no
    second pass to do — while still being pre-*validation*, so a templated action name is still a
    string here and AgDR-0042's refusal applies. Both halves in one, checked rather than assumed.

    `referenced_blueprint` (a property, not a method) is **informational only** here — the body is already in `raw_config`,
    so unlike #226's automation path there is nothing to expand and nothing to reach for it with.
    That asymmetry between the two is pinned by
    `test_a_script_raw_config_is_blueprint_expanded_but_not_validated`, because it rests on an HA
    implementation detail and #226's hole would reappear here silently if it changed.

    Returns `None` when HA has no such script, which is the only genuinely unresolvable case —
    `UnavailableScriptEntity` carries no `raw_config` at all.
    """
    component = hass.data.get(SCRIPT_DOMAIN_)
    entity = component.get_entity(entity_id) if component is not None else None
    return getattr(entity, "raw_config", None) if entity is not None else None


def _script_targets(config: Any, path: str) -> list[tuple[str, str]]:
    """Every script a config starts, as (entity_id, path) pairs."""
    out: list[tuple[str, str]] = []
    for node in _nodes(config):
        action = _fold(str(_service_name(node) or ""))
        if not action.startswith(f"{SCRIPT_DOMAIN_}."):
            continue
        service = action.split(".", 1)[1]
        if service in _SCRIPT_NOT_STARTING:
            continue
        if service in ("turn_on", "toggle"):
            ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node)
        else:
            # `script.my_script` — the per-script service names the script itself.
            ids = [action]
        for eid in ids:
            # **Drop nothing silently.** An id in a `script.*` target is either a script, one of
            # HA's two sentinels, or a mistake — and all three want an answer. Skipping the ones
            # that did not start with `script.` is what let `entity_id: all` through: it reached
            # every script in the house, including one that unlocks a door, while the scene
            # resolver refuses exactly that thirty lines below.
            #
            # `comp_entity_ids` is `Any(All(Lower, Any("all", "none")), entity_ids)`, so the
            # sentinel is **case-insensitive** — `ALL` folds to `all`, which is why this compares
            # the folded form rather than the literal.
            out.append((_fold(eid), path))
    return out


async def find_nested_script_violations(hass: HomeAssistant, config: Any) -> list[str]:
    """
    Why a script or automation may not be written: it starts a script that reaches a denied domain.

    A script is **not** checked when it runs (AgDR-0005), so `script.turn_on` at a script whose body
    unlocks a door was the last container without a mechanism — the same shape as `scene.turn_on`
    before #126, one name removed.

    **Fail closed**, and this is the load-bearing rule: a script Home Assistant does not have is
    refused rather than allowed. That is the opposite of `script_run_violations`, deliberately, and
    AgDR-0044's sentence about unreadable scripts is *scoped to run time* rather than overturned by
    this. The questions differ: at run time the script already exists, is exposed, and `routines.run`
    gates it, so refusing breaks a household's own scripts and grants nothing. At authoring time a
    model is acquiring an **unattended** capability — a `time_pattern` trigger starting a script
    nobody can inspect, with no human and no exposure gate in the loop — which is exactly what
    AgDR-0005 concedes it cannot check.

    Because `_script_raw_config` reads the running instance rather than `scripts.yaml`, "cannot
    read" now means Home Assistant itself has no such script, not "not in the file we happened to
    read" — so a household keeping scripts in `packages/` is unaffected.
    """
    problems: list[str] = []
    checked: set[str] = set()

    async def visit(entity_id: str, path: str) -> None:
        if entity_id in checked:  # a script that starts itself, or a diamond
            return
        checked.add(entity_id)
        if entity_id in ("all", "none"):
            problems.append(f"{path}: starting a script by '{entity_id}' names every script in the house rather than one")
            return
        if not entity_id.startswith(f"{SCRIPT_DOMAIN_}."):
            problems.append(f"{path}: {entity_id} is not a script, so what starting it would do cannot be checked")
            return
        raw = _script_raw_config(hass, entity_id)
        if not isinstance(raw, dict):
            problems.append(f"{path}: {entity_id} is a script Home Assistant does not have, so what it would do cannot be checked")
            return
        # `isinstance(raw, dict)` is **not** the fail-closed condition. `config.py` sets
        # `raw_config = dict(config)` *before* validation and `_minimal_config` keeps it for a
        # FAILED_BLUEPRINT script, so a script whose `use_blueprint` points at a **missing**
        # blueprint has a perfectly good dict holding the *unexpanded* reference. Walking that
        # finds nothing — #226's bug, inside the fix for #225. The script is `unavailable` and
        # cannot run now, so this is a TOCTOU rather than an immediate capability: the owner
        # restores the blueprint later and the automation is already written.
        if "use_blueprint" in raw:
            problems.append(f"{path}: {entity_id} is built from a blueprint Home Assistant could not load, so what it would do cannot be checked")
            return
        where = f"{path}->{entity_id}"
        # **What a script can do, not what it mentions.** Deliberately *without*
        # `find_reference_violations`, which the scene precedent also omits — `scene_run_violations`
        # checks membership domains only. Running it here refused an ordinary household script for
        # a `condition: state` on `lock.front_door`, or for the words "check lock.front_door" in a
        # notification, and took every Hearth automation that calls that script with it. Reading
        # whether a lock is locked is not reaching a lock: `HIDDEN_DOMAINS` already stops the model
        # learning the state, and the household wrote the condition themselves.
        problems.extend(find_policy_violations(raw, where))
        problems.extend(await find_nested_scene_violations(hass, raw))
        for nested, _ in _script_targets(raw, where):
            await visit(nested, where)

    for entity_id, path in _script_targets(config, "config"):
        await visit(entity_id, path)
    return list(dict.fromkeys(problems))


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
        # Folded, for the reason `policy._fold` gives: HA lower-cases a service name itself, so a
        # `Scene.Turn_On` would otherwise walk straight past the one check that reads what a scene
        # holds (AgDR-0012). The `homeassistant.turn_on` alias needs no branch here — the fan-out
        # rule refuses it at a scene outright, before this check is reached (#220).
        if _fold(str(_service_name(node) or "")) != f"{SCENE_DOMAIN}.turn_on":
            continue
        target = node.get("target") if isinstance(node.get("target"), dict) else {}
        for key in _UNRESOLVABLE_TARGET_KEYS:
            if key in target or key in node:
                problems.append(f"scene.turn_on by {key}: Hearth cannot tell which scenes that would activate")
        for eid in [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node) + _entity_ids({"entity_id": node.get("scene")}):
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
