"""What a written config may do with a switch a `switch_as_x` helper presents as a lock, siren or valve (#643).

`GateMap` (`registry.py`) says which entities carry a gate: the relay under a gated wrapper, every other
wrapper on it, a group over any of them, and the members of a gated group. The readers, `devices.call`
and the run checks ask it since #656. This module is the **authoring** half: an automation, script,
scene or config-flow form the model writes is checked against the same map before it is saved, because
the literal walkers in `policy.py` read the domain off the id and see a lock's relay as a switch.

Three checks, and which of them a caller runs is the caller's `mode`:

- **Named anywhere** (`authoring`, `form`). An id whose gate is in `OFF_LIMITS` (a lock or a siren) is
  refused wherever it appears, condition, trigger and template text included, as
  `find_reference_violations` refuses a `lock.*`: its state is the lock's state. A form refuses every
  gated id, a valve's too (ruling 6): a group or a template switch over the relay is a new way to move
  it, and a form value has no "only reading" position to tell apart.
- **Acted on** (every mode). An id a service call targets, a scene sets or a device action names is
  refused when its gate is not its own domain. A `valve.*` wrapper is left to the literal valve rule,
  which lets it close; anything else that drives a valve is refused both ways, since `invert` makes
  the direction unknowable (ruling 1). Registry ids are resolved before this runs (`resolve.py`), so a
  UUID is checked as the id it names.
- **Selected indirectly** (every mode; ruling 2, and ruling 10's forms). In a home with a gated wrapper,
  a service in a domain that some gated entity is literally in (always `switch`, and a sibling
  wrapper's `cover`, `fan` or `light`) may not select by `area_id`, `device_id`, `floor_id` or
  `label_id`, nor by a templated target or `all`/`none`. **Home-wide, and fails closed, on purpose:**
  resolving the area now goes stale as soon as the area changes, so the selection is refused rather
  than expanded. A home with no gated wrapper is unaffected.

`scene` mode is `authoring` for a scene, whose own `entities` keys are what it sets. `nested` mode is
what a started script is asked (`find_nested_script_violations`): what it can do, not what it
mentions, the same line that function already draws for the literal walk.

**Wording.** A lock- or siren-gated id is refused in words that name neither the id nor the gate,
because every reader answers for that relay as for an unknown id (#648, #651). A valve's relay is
still listed, so it is named with its gate. The refusal itself still tells the model that one of the
ids it wrote drives something Hearth does not operate: the stated limit ruling 10 recorded for scenes,
and no wording closes it.

**No hub mirror**, as for `find_nested_scene_violations`: the hub has no registry and no state, so it
cannot know which switch a wrapper holds. `POLICY_WALK_CASES` pins that the hub allows these shapes.

**Residual limit (ruling 2):** the check runs when the config is written. A config written before the
wrapper existed is not re-checked; nor is one an owner wrote, apart from what the run checks catch.
"""

from __future__ import annotations

from collections.abc import Iterator
import re
from typing import Any

from homeassistant.core import HomeAssistant

from ..policy import (
    _TEMPLATE_RE,
    INDIRECT_TARGET_KEYS,
    TARGET_BEARING_KEYS,
    _data_is_variables,
    _domain,
    _entity_ids,
    _service_name,
    _target_selection,
)
from .registry import OFF_LIMITS, GateMap, _gates

GATED_NAMED = "this names something Hearth does not operate"

# Every place in a string an entity id could start: after anything that cannot be part of one, so
# `states.switch.relay.state` offers `switch.relay` as well as `states.switch`. A lookahead, so the
# candidates overlap; the map answers None for any that is not an entity.
_ID_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_/\-])(?=([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+))")
_MATCH_ANY = frozenset({"all", "none"})
_SELECTOR_KEYS = ("entity_id", *INDIRECT_TARGET_KEYS)

MODES = frozenset({"authoring", "scene", "form", "nested"})


def _nodes(node: Any, path: str) -> Iterator[tuple[dict[str, Any], str]]:
    if isinstance(node, dict):
        yield node, path
        for k, v in node.items():
            yield from _nodes(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _nodes(v, f"{path}[{i}]")


def _strings(node: Any) -> Iterator[str]:
    """Every string in a config, mapping keys included."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node
    elif isinstance(getattr(node, "template", None), str):
        # A `Template` in the validated pass: a blueprint's body arrives compiled, not as a string.
        yield node.template


def _candidates(text: str) -> Iterator[str]:
    for m in _ID_CANDIDATE.finditer(text):
        yield m.group(1)


def _own_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].lower()


def _named(gates: GateMap, entity_id: str) -> str | None:
    """The gate an id carries that is not its own domain, or None."""
    gate = gates.get(entity_id)
    return gate if gate is not None and gate != _own_domain(entity_id) else None


def _refusal(gate: str, entity_id: str, path: str, why: str) -> str:
    if gate in OFF_LIMITS:
        return GATED_NAMED
    return f"{path}: {entity_id}: this drives a {gate}, {why}"


def _acted_on(node: dict[str, Any], action: Any) -> list[str]:
    """The ids a node moves: a service call's targets and a scene service's entities, or a device action's entity."""
    if isinstance(action, str):
        ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node)
        for src in (node, node.get("data"), node.get("data_template")):
            if isinstance(src, dict):
                entities = src.get("entities")
                if isinstance(entities, dict):
                    ids += [str(k) for k in entities]
                else:
                    ids += _entity_ids({"entity_id": entities})
                ids += _entity_ids({"entity_id": src.get("snapshot_entities")})
        return ids
    # A device action, trigger or condition: `{device_id, domain: switch, type: turn_on, entity_id}`
    # carries no service, and HA resolves `entity_id` (a UUID here, resolved already) to the relay.
    # Triggers and conditions are not told apart, as `find_policy_violations` does not for a valve.
    if "type" in node and isinstance(node.get("domain"), str):
        return _entity_ids(node)
    return []


def _selector_values(node: dict[str, Any], action: str) -> tuple[list[str], bool]:
    """Each selector value, comma lists split, and whether any carrier or value is not plain data.

    Anything that is not a `str`, `list` or `dict` where a target is read is opaque, and opaque is
    unknowable: in the validated pass a blueprint's body arrives with its templates compiled into
    `Template` objects, so `target: "{{ 'switch.' ~ w }}"` is neither a string nor a mapping there,
    and reading only those two shapes let it through (Security on db974825).
    """
    carriers = [node.get("target")] if _data_is_variables(action) else [node.get(k) for k in TARGET_BEARING_KEYS]
    out: list[str] = []
    opaque = any(c is not None and not isinstance(c, (dict, str)) for c in carriers)
    for source in [*carriers, node]:
        if not isinstance(source, dict):
            continue
        for key in _SELECTOR_KEYS:
            value = source.get(key)
            if value is None:
                continue
            values = [value] if isinstance(value, str) else value if isinstance(value, list) else None
            if values is None:
                opaque = True
                continue
            for v in values:
                if isinstance(v, str):
                    out += [p.strip() for p in v.split(",") if p.strip()]
                else:
                    opaque = True
    return out, opaque


def find_gate_violations(hass: HomeAssistant, config: Any, path: str = "config", *, mode: str, gates: GateMap | None = None) -> list[str]:
    """Why a config the model wrote may not be saved, for what a `switch_as_x` gate covers. Empty means it may.

    `config` must already have its registry ids resolved. See the module docstring for the three checks
    and which `mode` runs which.
    """
    if mode not in MODES:
        raise ValueError(mode)
    gates = _gates(hass) if gates is None else gates
    if gates.empty:
        return []
    problems: list[str] = []

    if mode in ("authoring", "scene", "form"):
        for text in _strings(config):
            for candidate in _candidates(text):
                gate = _named(gates, candidate)
                if gate is None:
                    continue
                if gate in OFF_LIMITS:
                    problems.append(GATED_NAMED)
                elif mode == "form":
                    problems.append(_refusal(gate, candidate, path, "so Hearth will not write it into a form"))

    domains = gates.domains()
    for node, p in _nodes(config, path):
        action = _service_name(node)
        acted = _acted_on(node, action)
        if mode == "scene" and node is config and isinstance(node.get("entities"), dict):
            # A scene's own `entities` keys are what activating it sets.
            acted += [str(k) for k in node["entities"]]
        for eid in acted:
            if (gate := _named(gates, eid)) is not None:
                problems.append(_refusal(gate, eid, p, "so only its own services may move it"))
        if not isinstance(action, str) or _domain(action) not in domains:
            continue
        keys, _ids, unknowable = _target_selection(node, action)
        for key in INDIRECT_TARGET_KEYS:
            if key in keys:
                problems.append(f"{p}: action {action} may not select by {key} in this home; name the entities")
        values, opaque = _selector_values(node, action)
        if unknowable or opaque or any(_TEMPLATE_RE.search(v) or v.lower() in _MATCH_ANY for v in values):
            problems.append(f"{p}: action {action} has a target that cannot be known before it runs, which this home does not allow; name the entities")
    return list(dict.fromkeys(problems))
