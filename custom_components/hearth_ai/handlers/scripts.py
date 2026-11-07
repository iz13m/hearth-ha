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
    _target_selection,
    find_policy_violations,
    find_reference_violations,
)
from ..rpc import Dispatcher, RpcError
from .common import lock_for, plain, read_yaml, require_config, require_str, write_yaml
from .registry import GateMap, _exposed, _gates
from .resolve import resolve_registry_ids
from .gated import find_gate_violations
from .scenes import _script_raw_config, _script_targets, find_config_violations, find_nested_scene_violations, gated_refusals

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

    **Registry ids are resolved (#663), and one that does not resolve is not refused here.** Authoring
    refuses it because a stored config waits, and HA gives a deleted entity's id back on restore. A
    run is now: HA resolves each step's `target.entity_id` as the step executes and raises on an id it
    cannot resolve (`helpers/service.py:305-319`, `async_validate_entity_ids`), so that step reaches
    nothing. Refusing would only stop the steps before it, in the household's own script.

    The residual case is a restore *during* the run: the owner restores the deleted entity while the
    labelled script is partway through, in a `delay` say, and HA resolves the later step to it.
    """
    key = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    path = _path(hass)
    async with lock_for(path):
        data = await read_yaml(hass, path, {})
    config = plain(data[key]) if isinstance(data, dict) and key in data else None
    if config is None:
        return []
    config, _unresolved = resolve_registry_ids(hass, config)  # unresolved is allowed, see above
    problems = find_policy_violations(config) + find_reference_violations(config, "config")
    # Literal, the two above: a switch a `switch_as_x` helper presents as a lock, siren or valve, or a
    # group over one, is just a switch to them (#643). Every id-shaped string counts, a condition's
    # included: this is a scene, and a scene is refused for what it touches.
    gates = _gates(hass)
    problems += gated_refusals(gates, _named_ids(config))
    # Templated, match-all and indirect targets, which the literal scan above cannot see (ruling 10).
    problems += _selection_refusals(hass, gates, config)
    problems += await find_nested_scene_violations(hass, config)
    # And what a script it starts can do to a gated relay (#643): a scene that runs `script.turn_on` is
    # still a scene, and the started script is not checked when it runs (AgDR-0005).
    problems += _started_script_gate_violations(hass, gates, config)
    return list(dict.fromkeys(problems))


def _started_script_gate_violations(hass: HomeAssistant, gates: GateMap, config: Any) -> list[str]:
    """The `switch_as_x` gate half of `find_nested_script_violations`, over every script a scene-script starts.

    **Only the gate half, and nothing in a home with no gated wrapper.** The authoring check fails closed
    on a script Home Assistant does not have, a missing blueprint or an unresolved registry id, and runs
    the literal walk; `script_run_violations` lets those run (AgDR-0044, #663), because at run time
    the script already exists, is exposed, and `routines.run` gates it. So only what #643 adds is asked
    here: what each started script moves, and how it selects it (`find_gate_violations`, `nested`). A
    script that cannot be read, or an id that is not a script, is skipped rather than refused.
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
        todo += _script_targets(raw, where)
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
