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

from collections.abc import Callable
import re
from typing import Any

from ..policy import (
    NOTIFY_COMMAND_RULE,
    TARGET_BEARING_KEYS,
    _TEMPLATE_RE,
    VALVE_DOMAIN,
    _data_is_variables,
    _domain,
    _entity_ids,
    _fold,
    _service_name,
    notify_message_problem,
)
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
# A notification's words, which an input may still fill in (#812). Every other key of a notify call is
# guarded, its nested `data` included, because that is where a phone reads `command`, `url`,
# `clickAction` and an action button's `uri`. The words are read by the delivery guard when the
# notification is sent (`delivery.py`), and every input value by `find_input_value_violations` when
# the config that passes it is written.
_NOTIFY_WORDS = frozenset({"message", "title"})
NOTIFY_STEP_RULE = (
    "Hearth could not install its notification delivery guard in this Home Assistant, so a script that sends a "
    "notification is not started with inputs"
)


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
        if _domain(action) == "notify":
            # Everything but the words (#812).
            fields.extend(f"{p}.{key}.{k}" for k, v in carrier.items() if k not in _NOTIFY_WORDS and _filled_in(v))
        fields.extend(_guarded_in(carrier, guarded, f"{p}.{key}"))
    return fields


def _device_action_fields(node: dict[str, Any], p: str) -> list[str]:
    """The guarded fields of a device action that hold a template: what it acts on, and for the companion
    app's notify action everything but its words (#812)."""
    fields = [f"{p}.{k}" for k in _DEVICE_ACTION_KEYS if k in node and _filled_in(node[k])]
    if _fold(node.get("domain")) == "mobile_app":
        skip = _NOTIFY_WORDS | _NOT_ACTION | set(_DEVICE_ACTION_KEYS)
        fields.extend(f"{p}.{k}" for k, v in node.items() if k not in skip and _filled_in(v))
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
            fields = _device_action_fields(node, p)
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


def _script_inputs(node: dict[str, Any], action: str) -> list[tuple[str, Any]]:
    """The inputs a call that starts a script hands it, as (field, value) pairs. See `passes_inputs`."""
    carriers = [(k, node.get(k)) for k in ("data", "data_template")]
    if _data_is_variables(action):
        return [(k, v) for k, v in carriers if v is not None] + [(k, node[k]) for k in _NODE_TARGET_KEYS if k != "scene" and k in node]
    return [(f"{k}.variables", v["variables"]) for k, v in carriers if isinstance(v, dict) and v.get("variables") is not None]


# A message or title that only places one input, as the household wrote `{{ message }}`: the matcher
# reads the value that fills it, and `| trim` and `| lower` are what the matcher already does to it.
_BARE_RE = re.compile(r"^\s*\{\{-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*(?:trim|lower)\s*)*-?\}\}\s*$")
WORDS_RULE = (
    "a household script started with inputs may put one into a notification's message or title only as a bare "
    "{{ input }} (| trim and | lower allowed), or after literal words, so that what reaches a phone can be read "
    f"when the inputs are written; {NOTIFY_COMMAND_RULE}"
)

# What `notifies` answers for a started script (`scenes._chain_notifies`): it, or a script it starts with
# inputs, sends a notification; or what it does cannot be read. None: neither.
NOTIFIES = "notifies"
UNREADABLE = "unreadable"


def _assigned_names(config: Any) -> set[str]:
    """Every name a script assigns itself: a `variables` mapping at any depth, and a `response_variable`.
    A bare `{{ name }}` over one of these is not an input placed as it came, so it is not bare."""
    names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                visit(n)
        elif isinstance(node, dict):
            for k, v in node.items():
                if k == "variables" and isinstance(v, dict):
                    names.update(x for x in v if isinstance(x, str))
                elif k == "response_variable" and isinstance(v, str):
                    names.add(v)
                visit(v)

    visit(config)
    return names


def _bare(value: Any, assigned: set[str]) -> bool:
    source = getattr(value, "template", value)
    match = _BARE_RE.match(source) if isinstance(source, str) else None
    return match is not None and match.group(1) not in assigned


def _words_problem(value: Any, assigned: set[str]) -> bool:
    """A message or title built from inputs in a way the matcher cannot read when they are written."""
    return _filled_in(value) and not _bare(value, assigned) and notify_message_problem(value) is not None


def find_notify_word_violations(config: Any, path: str, entity_id: str) -> list[str]:
    """For a household script started with inputs (#812): each notification whose message or title uses
    a template the matcher cannot read through. Such a script runs under Home Assistant's own context
    when an authored automation starts it, where the delivery guard does not look, so this has to hold
    without it. Allowed: a bare `{{ input }}` with `| trim` or `| lower`, and a template after literal
    words that cannot become a command. Refused, whatever it references (#777's line): a join, a
    `replace`, anything else that could assemble a command from harmless parts."""
    assigned = _assigned_names(config)
    found: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if isinstance(action, str) and _domain(action) == "notify":
            for key in ("data", "data_template"):
                carrier = node.get(key)
                if isinstance(carrier, dict):
                    found.extend(f"{p}.{key}.{k}" for k in _NOTIFY_WORDS if k in carrier and _words_problem(carrier[k], assigned))
        elif action is None and "device_id" in node and _fold(node.get("domain")) == "mobile_app":
            found.extend(f"{p}.{k}" for k in _NOTIFY_WORDS if k in node and _words_problem(node[k], assigned))
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config["sequence"] if isinstance(config, dict) and "sequence" in config else config, f"{path}.sequence" if isinstance(config, dict) and "sequence" in config else path)
    return [f"{field} is decided when {entity_id} runs, and it is started with inputs; {WORDS_RULE}" for field in dict.fromkeys(found)]


def _started_ids(node: dict[str, Any], action: str) -> list[str]:
    """The scripts a call starts, as `scenes._script_calls` reads them; empty when they cannot be named."""
    if _data_is_variables(action):
        return [action]
    return [_fold(i) for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + [_fold(i) for i in _entity_ids(node)]


def _leaves(value: Any, p: str, bad: Callable[[Any], bool]) -> list[str]:
    if isinstance(value, dict):
        return [f for k, v in value.items() for f in _leaves(v, f"{p}.{k}", bad)]
    if isinstance(value, list):
        return [f for i, v in enumerate(value) for f in _leaves(v, f"{p}[{i}]", bad)]
    return [p] if bad(value) else []


def find_input_value_violations(config: Any, path: str, notifies: Callable[[str], str | None], *, household: bool = False) -> list[str]:
    """The inputs a config hands a script that could become a companion-app command (#812, amending #777).

    Only where the started script, or one it starts with inputs, sends a notification, or cannot be read
    (`notifies`): a script that dims to a value, or is passed a door's name and never notifies, is not
    asked. There every value is read as a message would be, by R4's matcher (`notify_message_problem`),
    trimmed and folded, a template refused when its literal lead could still become a command.

    `household` is for a household script started with inputs, passing some on: there a literal is the
    household's own, and a templated value is held to `find_notify_word_violations`'s line, since it may
    carry what the caller passed. The refusal names the field, never the value.
    """
    problems: list[str] = []
    assigned = _assigned_names(config) if household else set()

    def bad(value: Any) -> bool:
        return _words_problem(value, assigned) if household else notify_message_problem(value) is not None

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if isinstance(action, str) and _domain(action) == "script" and _fold(action).split(".", 1)[1] not in ("reload", "turn_off"):
            ids = _started_ids(node, _fold(action)) or ["all"]
            verdicts = {eid: notifies(eid) for eid in ids}
            if why := next((v for v in verdicts.values() if v), None):
                eid = next(e for e, v in verdicts.items() if v == why)
                reason = f"{eid} sends a notification" if why == NOTIFIES else f"what {eid} does cannot be read"
                for field, value in _script_inputs(node, _fold(action)):
                    problems.extend(
                        f"{leaf}: an input to {action} could become a companion-app command, and {reason}; {NOTIFY_COMMAND_RULE}"
                        for leaf in _leaves(value, f"{p}.{field}", bad)
                    )
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    if household and isinstance(config, dict) and "sequence" in config:
        visit(config["sequence"], f"{path}.sequence")
    else:
        visit(config, path)
    return list(dict.fromkeys(problems))


def notify_steps(config: Any, path: str) -> list[str]:
    """Each step of a script that could send a notification: a `notify.*` action, one whose action name
    is a template, or the companion app's device action. For failing closed without the guard (#812)."""
    found: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if action is not None:
            if not isinstance(action, str) or _TEMPLATE_RE.search(action) or _domain(action) == "notify":
                found.append(p)
        elif "device_id" in node and _fold(node.get("domain")) == "mobile_app":
            found.append(p)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    if isinstance(config, dict) and "sequence" in config:
        visit(config["sequence"], f"{path}.sequence")
    else:
        visit(config, path)
    return found
