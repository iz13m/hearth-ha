"""Script CRUD mirroring homeassistant.components.config.script (key-based scripts.yaml)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import voluptuous as vol

from homeassistant.components.script import DOMAIN as SCRIPT_DOMAIN
from homeassistant.components.script.config import async_validate_config_item
from homeassistant.config import SCRIPT_CONFIG_PATH
from homeassistant.const import SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.util import slugify

from ..labels import async_mark_hearth_scene, is_hearth_scene
from ..policy import (
    _TEMPLATE_RE,
    INDIRECT_TARGET_KEYS,
    TARGET_BEARING_KEYS,
    _data_is_variables,
    _service_name,
    _domain,
    _target_selection,
    fans_out,
    find_policy_violations,
    find_reference_violations,
)
from ..rpc import Dispatcher, RpcError
from .common import check_hearth_scene_kept, lock_for, plain, read_yaml, require_config, require_str, write_yaml
from .registry import GateMap, _gates, run_fields, visible
from .resolve import resolve_registry_ids
from .gated import find_gate_violations
from .remotes import find_remote_violations
from .scenes import (
    _config_for_entity,
    _script_raw_config,
    _script_targets,
    find_config_violations,
    find_nested_scene_violations,
    find_scene_run_violations,
    gated_refusals,
)

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


async def scripts_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
    file_keys = {str(k) for k in data} if isinstance(data, dict) else set()
    out = []
    ent_reg = er.async_get(hass)
    gates = _gates(hass)
    for state in sorted(hass.states.async_all(SCRIPT_DOMAIN), key=lambda s: s.entity_id):
        key = state.entity_id.split(".", 1)[1]
        ent = ent_reg.async_get(state.entity_id)
        # `Hearth: scene` — this script is a scene with steps or a schedule (AgDR-0044).
        labelled = is_hearth_scene(hass, state.entity_id)
        # Listed either way: an un-exposed or hidden script can still be read and edited, just not
        # run. `visible()`, as `scripts.run` asks it (#644), and for a labelled script the run-time
        # scan `scripts.run` makes too (#759), in the same order: a script nobody can see is not read.
        shown = visible(hass, state.entity_id, ent, gates)
        # The copy read above, as `scenes_list` passes `by_id`: one file read for the whole list.
        refused = shown and labelled and bool(await script_run_violations(hass, state.entity_id, data))
        out.append(
            {
                "id": key,
                "entity_id": state.entity_id,
                "alias": state.name,
                "description": None,
                "editable": key in file_keys,
                **run_fields(hass, state.entity_id, ent, gates, shown=shown, refused_contents=refused),
                "hearth_scene": labelled,
            }
        )
    return out


def _named_ids(node: Any) -> Iterator[str]:
    """Every string in a config that could be an entity id, mapping keys and comma lists included."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _named_ids(key)
            yield from _named_ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from _named_ids(value)
    elif isinstance(node, str):
        yield from (part.strip() for part in node.split(",") if "." in part)


UNKNOWABLE_TARGET = "this scene's target cannot be known before it runs, so it cannot be activated"
_SELECTOR_KEYS = ("entity_id", *INDIRECT_TARGET_KEYS)
_MATCH_ANY = frozenset({"all", "none"})


def _action_nodes(node: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every service-call node in a config, nested sequences included, with the service it names."""
    if isinstance(node, list):
        for n in node:
            yield from _action_nodes(n)
    elif isinstance(node, dict):
        action = _service_name(node)
        if isinstance(action, str):
            yield action, node
        for value in node.values():
            yield from _action_nodes(value)


def _selector_values(node: dict[str, Any], action: str) -> dict[str, list[str]]:
    """Each selector key's values, from every carrier `_target_selection` reads, comma lists split."""
    carriers = [node.get("target")] if _data_is_variables(action) else [node.get(k) for k in TARGET_BEARING_KEYS]
    out: dict[str, list[str]] = {}
    for source in [*carriers, node]:
        if not isinstance(source, dict):
            continue
        for key in _SELECTOR_KEYS:
            value = source.get(key)
            values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            for v in values:
                if isinstance(v, str):
                    out.setdefault(key, []).extend(p.strip() for p in v.split(",") if p.strip())
    return out


def _selection_refusals(hass: HomeAssistant, gates: GateMap, config: Any) -> list[str]:
    """What the literal scan cannot see in a scene-script, in a home with a gated wrapper (ruling 10).

    `_named_ids` reads only literal `domain.object` text, and three more forms reach a relay:
    - **A target that cannot be known before it runs**: a whole-template `target`/`data`/
      `data_template`, a templated selector value, or `all`. Refused outright, on the fan-out rule's
      reasoning, in words that name neither the relay nor its gate.
    - **An indirect target** (`area_id`, `device_id`, `floor_id`, `label_id`): expanded with Home
      Assistant's own target helper, as HA expands it when the step runs, and the entities it
      reaches checked like named ones. `primary_entities_only=False` is the wider of HA's two
      expansions, so a settings entity on the relay's device is not skipped.

    Only where `gates` is not empty, ruling 2's home-wide condition: a home with no gated wrapper
    has nothing these forms could reach that the literal scan would miss.

    `find_scene_run_violations` cannot meet any of these in a `scenes.yaml` entry: HA's scene schema
    (`_convert_states`) keys `entities` by `cv.entity_id`, so a template, `all` or a registry UUID is
    not a key it loads, its states are static values HA never renders, and a scene has no target, so
    no area, device, floor or label selector.
    """
    if gates.empty:
        return []
    problems: list[str] = []
    reached: set[str] = set()
    for action, node in _action_nodes(config):
        _keys, _ids, unknowable = _target_selection(node, action)
        selected = _selector_values(node, action)
        values = [v for vs in selected.values() for v in vs]
        if unknowable or any(_TEMPLATE_RE.search(v) or v.lower() in _MATCH_ANY for v in values):
            problems.append(UNKNOWABLE_TARGET)
            continue
        indirect = {k: selected[k] for k in INDIRECT_TARGET_KEYS if k in selected}
        if indirect:
            found = async_extract_referenced_entity_ids(hass, TargetSelection(indirect), primary_entities_only=False)
            reached |= found.referenced | found.indirectly_referenced
    return problems + gated_refusals(gates, sorted(reached))


def _script_key(hass: HomeAssistant, entity_id: str) -> str:
    """The `scripts.yaml` key behind a script entity: its registry `unique_id` (#720).

    Not the entity id's object id: an owner who renames a script's entity id in Home Assistant keeps
    the key, and the label, so reading the key off the new id found no config and the script ran
    unchecked. The split is the fallback only for an id the registry does not know.
    """
    # For a `scripts.yaml` script the `unique_id` is its key; a script defined elsewhere has no
    # `scripts.yaml` config under any key and is skipped below, as before.
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None:
        return str(entry.unique_id)
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


async def script_run_violations(hass: HomeAssistant, entity_id: str, data: Any = None) -> list[str]:
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

    **Registry ids are resolved (#663), and one that does not resolve is not refused here.** Authoring
    refuses it because a stored config waits, and HA gives a deleted entity's id back on restore. A
    run is now: HA resolves each step's `target.entity_id` as the step executes and raises on an id it
    cannot resolve (`helpers/service.py:305-319`, `async_validate_entity_ids`), so that step reaches
    nothing. Refusing would only stop the steps before it, in the household's own script.

    The residual case is a restore *during* the run: the owner restores the deleted entity while the
    labelled script is partway through, in a `delay` say, and HA resolves the later step to it.

    `data` is `scripts.yaml` as the caller already parsed it, for `scripts.list`, which asks this of
    every labelled script and would otherwise read the file once per script. A run passes nothing and
    reads it fresh, so the check that refuses a run is never made against a stale copy.
    """
    key = _script_key(hass, entity_id)
    if data is None:
        path = _path(hass)
        async with lock_for(path):
            data = await read_yaml(hass, path, {})
    config = plain(data[key]) if isinstance(data, dict) and key in data else None
    if config is None:
        return []
    config, _unresolved = resolve_registry_ids(hass, config)  # unresolved is allowed, see above
    # Resolved above, and an id that did not resolve is allowed at run time, so the walker is told (#663).
    problems = find_policy_violations(config, registry_ids_resolved=True) + find_reference_violations(config, "config")
    # Literal, the two above: a switch a `switch_as_x` helper presents as a lock, siren or valve, or a
    # group over one, is just a switch to them (#643). Every id-shaped string counts, a condition's
    # included: this is a scene, and a scene is refused for what it touches.
    gates = _gates(hass)
    problems += gated_refusals(gates, _named_ids(config))
    # Which remote a `remote.send_command` or `learn_command` reaches (#568, F1), and which activity a remote starts (#765), as when it was authored.
    problems += find_remote_violations(hass, config)
    # Templated, match-all and indirect targets, which the literal scan above cannot see (ruling 10).
    problems += _selection_refusals(hass, gates, config)
    problems += await find_nested_scene_violations(hass, config)
    # And what a script it starts can do to a gated relay (#643): a scene that runs `script.turn_on` is
    # still a scene, and the started script is not checked when it runs (AgDR-0005).
    problems += await _started_script_gate_violations(hass, gates, config)
    return list(dict.fromkeys(problems))


async def _started_routine_violations(hass: HomeAssistant, gates: GateMap, config: Any, where: str) -> tuple[list[str], list[tuple[str, str]]]:
    """What a started script starts in turn, in a gated home: a scene it activates, and a target it cannot name (#716).

    `find_gate_violations` asks a started script what it moves, not what it activates, so a started
    script running `scene.turn_on scene.porch` reached a relay the scene sets while `scenes.activate`
    of that scene was refused. Every action whose reach is decided by its target (`fans_out`:
    `scene.turn_on`, every `script.*` that starts one, and `homeassistant.turn_on|turn_off|toggle`)
    is followed. Each named scene is asked `find_scene_run_violations`, the activation check itself;
    a script a `homeassistant.*` action names is returned for the caller to walk, since
    `_script_targets` reads only `script.*` actions; any other id is skipped rather than refused, as a
    started script that cannot be read is.

    Such an action aimed at `all`, `none`, a template or an indirect selector (`area_id`, `device_id`,
    `floor_id`, `label_id`) cannot be followed, so in a gated home it is refused as unknowable, as
    `_selection_refusals` refuses such a target at the top level.
    """
    problems: list[str] = []
    started: list[tuple[str, str]] = []
    for action, node in _action_nodes(config):
        action = action.lower()
        if not fans_out(action):
            continue
        keys, ids, unknowable = _target_selection(node, action)
        selected = _selector_values(node, action)
        values = [v for vs in selected.values() for v in vs]
        if (
            unknowable
            or any(k in keys for k in INDIRECT_TARGET_KEYS)
            or any(_TEMPLATE_RE.search(v) or v.lower() in _MATCH_ANY for v in values)
        ):
            problems.append(UNKNOWABLE_TARGET)
            continue
        if _domain(action) == "script":
            continue  # walked by the caller through `_script_targets`
        for eid in dict.fromkeys(i.lower() for i in ids):
            if eid.startswith("scene."):
                problems += find_scene_run_violations(hass, eid, await _config_for_entity(hass, eid), gates)
            elif eid.startswith("script.") and _domain(action) == "homeassistant":
                started.append((eid, where))
    return problems, started


async def _started_script_gate_violations(hass: HomeAssistant, gates: GateMap, config: Any) -> list[str]:
    """The `switch_as_x` gate half of `find_nested_script_violations`, over every script a scene-script starts.

    **Only the gate half, and nothing in a home with no gated wrapper.** The authoring check fails closed
    on a script Home Assistant does not have, a missing blueprint or an unresolved registry id, and runs
    the literal walk; `script_run_violations` lets those run (AgDR-0044, #663), because at run time
    the script already exists, is exposed, and `routines.run` gates it. So only what #643 adds is asked
    here: what each started script moves, and how it selects it (`find_gate_violations`, `nested`), and
    what it starts in turn (`_started_routine_violations`, #716): the scenes it activates, and a
    script or scene it starts by a target that cannot be named. A script that cannot be read, or an id
    that is not a script, is skipped rather than refused.
    """
    if gates.empty:
        return []
    problems: list[str] = []
    checked: set[str] = set()
    todo = [(entity_id, path) for entity_id, path in _script_targets(config, "config")]
    while todo:
        entity_id, path = todo.pop()
        if entity_id in checked or not entity_id.startswith("script."):
            continue
        checked.add(entity_id)
        raw = _script_raw_config(hass, entity_id)
        if not isinstance(raw, dict) or "use_blueprint" in raw:
            continue
        where = f"{path}->{entity_id}"
        raw, _unresolved = resolve_registry_ids(hass, raw, where)  # unresolved is allowed at run time, as above
        problems += find_gate_violations(hass, raw, where, mode="nested", gates=gates)
        found, started = await _started_routine_violations(hass, gates, raw, where)
        problems += found
        todo += _script_targets(raw, where) + started
    return problems


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


def _free_key(hass: HomeAssistant, data: dict[str, Any], base: str) -> str:
    """`base`, or `base_2`, `base_3`… — the first not in `scripts.yaml` and not a live script."""
    key = base
    n = 2
    while key in data or hass.states.get(f"{SCRIPT_DOMAIN}.{key}") is not None:
        key = f"{base}_{n}"
        n += 1
    return _check_key(key)


async def _save(
    hass: HomeAssistant,
    key: str,
    config: dict[str, Any],
    *,
    must_exist: bool,
    hearth_scene: bool | None = None,
    new_from: str | None = None,
) -> dict[str, Any]:
    """Write one script. With `new_from`, the key is a new one chosen from that base name.

    The free key is chosen under the same lock that writes `scripts.yaml`, not before it: choosing
    it, releasing the lock and writing later let two creates with the same name pick the same key,
    and the second overwrote the first (#173).
    """
    # A key chosen from `new_from` is not known yet, so that create is checked below, once it is.
    if new_from is None:
        check_hearth_scene_kept(hass, _entity_id(hass, key), hearth_scene)
    result = await _validate(hass, key, config)
    if not result["ok"]:
        raise RpcError("validation_failed", result.get("error") or "invalid script", {"status": result.get("status")})
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
        if not isinstance(data, dict):
            raise RpcError("ha_error", "scripts.yaml is not a mapping")
        if new_from is not None:
            key = _free_key(hass, data, new_from)
            # `_free_key` checks `scripts.yaml` and live states, not the registry, so the key may still
            # carry a registry entry left by a labelled script deleted from the file, and the reload
            # re-attaches it, label and all (#720). Checked before the write, as above.
            check_hearth_scene_kept(hass, _entity_id(hass, key), hearth_scene)
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
    if key:
        return await _save(hass, _check_key(str(key)), config, must_exist=False, hearth_scene=_hearth_scene(params))
    base = _check_key(slugify(str(config.get("alias") or "hearth_script")) or "hearth_script")
    # The key itself is chosen inside `_save`, under the lock that writes it.
    return await _save(hass, base, config, must_exist=False, hearth_scene=_hearth_scene(params), new_from=base)


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
