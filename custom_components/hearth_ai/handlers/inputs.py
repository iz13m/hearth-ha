"""What an input passed to a household script may reach (#777, owner ruling 2026-11-08: check, not refuse).

An authored automation, script or config-flow form value may start a household script with inputs:
`script.turn_on` with `data.variables`, or the per-script service `script.<id>` with `data`, which
becomes the script's variables whole. The household wrote the script, so what it does with a fixed
value is AgDR-0005's accepted limit. What the caller fills in is not: a household "notify the TV" whose
`data.icon` is `{{ icon }}` lets a model have webOS read any file on the host (#771), and one whose
target is `light.{{ room }}` lets it choose what the script acts on.

So when inputs are passed, the started script may use a template only where a value cannot choose
what the script acts on, what it reads off the host, or which service it calls. The rule is
deliberately coarser than "this field names that input": a template in a guarded field is refused
whatever it references, because a template can reach an input through a `variables` step, a
`repeat.item`, or `this`, and no reading of Jinja here would be complete. A message, a title or a
temperature filled in from an input stays allowed, which is what keeps a parameterised household
announcement working.

Only the script's `sequence` is walked: its `fields` (a `selector: {action: {}}` is a dict with an
`action` key) and its script-level `variables` describe inputs and act on nothing.

**Guarded**, on a node that names a service (`action`, `service`, `service_template`, or the `scene`
shorthand):

- the service name itself;
- the node's `target`, and its own `entity_id`, `device_id`, `area_id`, `floor_id`, `label_id` and `scene`;
- in `data` and `data_template`: the carrier itself when it is a template or has a templated key (it
  could render to any of these), and at any depth, a value under `GUARDED_DATA_KEYS`;
- for a valve, `position` too (#527; the policy walk already refuses any but a literal 0);
- for `remote.send_command`, `learn_command` and `delete_command`, the whole node (#568, #765).

And on a device action (a node with `device_id` and `type` or `domain`), its `device_id`, `domain`,
`type`, `subtype` and `entity_id`.

Not guarded: a script call's own variables (`data` on `script.<id>`, `data.variables` on
`script.turn_on`), because those are inputs to the script one hop further, which the caller walks
with the same rule.
"""

from __future__ import annotations

from typing import Any

from ..policy import _TEMPLATE_RE, VALVE_DOMAIN, _data_is_variables, _domain, _fold, _service_name
from .remotes import _filled_in

INPUT_RULE = "a household script started with inputs may use them only where they cannot choose what it acts on, what it reads, or which service it calls"

# Keys inside a service call's data that choose what the call acts on, which file or URL it reads, or
# which remote activity it starts. `target` is legacy notify's recipient list: an input that chooses it
# chooses who is told.
GUARDED_DATA_KEYS: frozenset[str] = frozenset(
    {
        "entity_id",
        "device_id",
        "area_id",
        "floor_id",
        "label_id",
        "target",
        "entities",
        "snapshot_entities",
        "icon",
        "activity",
        "media_content_id",
    }
)

_NODE_TARGET_KEYS = ("target", "entity_id", "device_id", "area_id", "floor_id", "label_id", "scene")
_DEVICE_ACTION_KEYS = ("device_id", "domain", "type", "subtype", "entity_id")
_REMOTE_COMMANDS = frozenset({"remote.send_command", "remote.learn_command", "remote.delete_command"})
_NOT_ACTION = frozenset({"alias", "note", "enabled", "continue_on_error", "response_variable"})


def _guarded_in(value: Any, guarded: frozenset[str], p: str) -> list[str]:
    """Each place under `value` where a guarded key holds anything filled in when it runs."""
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k in guarded and _filled_in(v):
                found.append(f"{p}.{k}")
            elif isinstance(v, (dict, list)):
                found.extend(_guarded_in(v, guarded, f"{p}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found.extend(_guarded_in(v, guarded, f"{p}[{i}]"))
    return found


def _action_fields(node: dict[str, Any], action: Any, p: str) -> list[str]:
    """The guarded fields of one service-call node that hold a template."""
    if not isinstance(action, str) or _TEMPLATE_RE.search(action):
        return [f"{p}: its action name"]
    folded = _fold(action)
    if folded in _REMOTE_COMMANDS:
        return [f"{p}: action {action}"] if any(_filled_in(v) for k, v in node.items() if k not in _NOT_ACTION) else []
    fields = [f"{p}.{k}" for k in _NODE_TARGET_KEYS if k in node and _filled_in(node[k])]
    if _data_is_variables(action):
        return fields  # `data` is the started script's variables, walked one hop further
    guarded = GUARDED_DATA_KEYS | {"position"} if _domain(action) == VALVE_DOMAIN else GUARDED_DATA_KEYS
    for key in ("data", "data_template"):
        carrier = node.get(key)
        if carrier is None:
            continue
        if not isinstance(carrier, dict) or any(not isinstance(k, str) or _TEMPLATE_RE.search(k) for k in carrier):
            fields.append(f"{p}.{key}")
            continue
        if _domain(action) == "script":
            # `script.turn_on`/`toggle`: `variables` are the started script's inputs, walked one hop further.
            carrier = {k: v for k, v in carrier.items() if k != "variables"}
        fields.extend(_guarded_in(carrier, guarded, f"{p}.{key}"))
    return fields


def find_input_reachable_violations(config: Any, path: str, entity_id: str) -> list[str]:
    """Every guarded field of a household script `entity_id` that an input could fill in (#777)."""
    problems: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if action is not None:
            fields = _action_fields(node, action, p)
        elif "device_id" in node and ("type" in node or "domain" in node):
            fields = [f"{p}.{k}" for k in _DEVICE_ACTION_KEYS if k in node and _filled_in(node[k])]
        else:
            fields = []
        problems.extend(f"{field} is decided when {entity_id} runs, and it is started with inputs; {INPUT_RULE}" for field in fields)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    # Only the script's actions: `fields` (whose `selector: {action: {}}` is a dict with an `action` key)
    # and the script-level `variables` describe inputs, they do not act on anything.
    if isinstance(config, dict) and "sequence" in config:
        visit(config["sequence"], f"{path}.sequence")
    else:
        visit(config, path)
    return list(dict.fromkeys(problems))


def passes_inputs(node: dict[str, Any], action: str, *, templated_only: bool = False) -> bool:
    """Whether a call that starts a script hands it inputs (#777).

    - `script.<id>`: Home Assistant folds `target` into the service data (`core.py`, `ServiceCall`) and
      the per-script service passes all of it as the script's variables, so `data`, `data_template`,
      `target`, and a node-level `entity_id`, `device_id`, `area_id`, `floor_id` or `label_id` all count.
    - `script.turn_on`/`toggle`: `variables` in `data` or `data_template`.
    - Either: a carrier that is a template or has a templated key, which could render to the above.

    `templated_only` is for a call one hop or more removed, inside a household script: there a value
    the household wrote literally is the household's own, so only inputs that hold a template (and so
    could carry what the caller passed on) count.
    """
    def counts(value: Any) -> bool:
        if value in (None, {}, [], ""):
            return False
        return _filled_in(value) if templated_only else True

    carriers = [node.get(k) for k in ("data", "data_template")]
    for carrier in carriers:
        if carrier is not None and (not isinstance(carrier, dict) or any(not isinstance(k, str) or _TEMPLATE_RE.search(k) for k in carrier)):
            return True
    if _data_is_variables(action):
        return any(counts(v) for v in carriers) or any(counts(node.get(k)) for k in _NODE_TARGET_KEYS if k != "scene")
    return any(counts(c.get("variables")) for c in carriers if isinstance(c, dict))
