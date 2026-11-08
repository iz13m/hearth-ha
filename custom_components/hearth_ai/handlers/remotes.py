"""Which remotes Hearth may send or teach a command (#568, Security's F1). Box only: the hub cannot see platforms.

The owner allowed `remote.send_command` on the reasoning that the household chose what to teach the
remote, so sending one of its stored names is the household's own decision. That holds only for a
remote whose integration plays **stored names and nothing else**. Many do not, measured on HA 2026.9.3:

- `androidtv` runs a command it has no key for as an `adb shell` line on the TV (`async_send_command`);
- `xiaomi_miio` plays a payload it has no stored name for as a raw IR code;
- `mysensors` writes the value straight to the node; `madvr` and `apple_tv` pass unknown names through.

Learning is not harmless either: on MySensors `async_learn_command` writes each `command` string to the
node as `V_IR_RECORD`, and Broadlink's learning **overwrites** a name the household already taught (its
store is `update({command: code})`), so a model can re-point "power". Only Broadlink and MySensors
implement learning on 2026.9.3; re-pointing a name on Broadlink is within the owner's ruling.

So `send_command` and `learn_command` are allowed only when **every** target is a literal `remote.*` entity id whose
entity-registry platform is in `REMOTE_NAMED_COMMAND_PLATFORMS`. Broadlink is the one platform read so
far: a name it has no code for fails, and its raw-code syntax (`b64:`) is refused by the shared rule in
`policy.py`. **A platform joins only after someone reads its `send_command` and `learn_command`.**

Fails closed on anything it cannot read as such a list: no registry entry, a platform not in the set,
a target by area, device, floor or label (and `all`), a templated or non-string id, a templated target,
an `entity_id` placed in `data`, or no target at all. Applied to devices.call (`control.py`), to every
authored automation and script on both of `_validate`'s passes (`scenes.find_config_violations`), to a
`Hearth: scene` script when it runs (`scripts.script_run_violations`), and to a config-flow form value
(`flows.check_flow_input`). On, off and toggle are not limited, and `delete_command` is refused everywhere.

**A remote activity (#765).** On, off and toggle take an `activity`, and `androidtv_remote` launches
whatever it names, a URL included; `homeassistant.turn_on`/`turn_off`/`toggle` forward it. On the same
surfaces, an `activity` is allowed only when every target is a literal `remote.*` id whose live state
lists that exact string in `activity_list` (`remote_activity_refusal`). The walkers in `policy.py` and
`policy.ts` refuse what could not be compared with the list.

**One hop away (#765)** `find_nested_remote_violations` holds a household script Hearth starts to a
narrower rule: no platform allowlist, but no target or command a caller can fill in. See its docstring.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..policy import _REMOTE_ACTIVITY_SERVICES, _TEMPLATE_RE, _fold, _service_name

REMOTE_NAMED_COMMAND_PLATFORMS: frozenset[str] = frozenset({"broadlink"})

_LIMITED = frozenset({"remote.send_command", "remote.learn_command"})
_RULE = f"Hearth sends or teaches remote commands only to a remote whose integration plays and learns stored names alone ({', '.join(sorted(REMOTE_NAMED_COMMAND_PLATFORMS))})"
_ACTIVITY_RULE = "Hearth starts only a remote activity the remote itself lists in its activity_list, named literally"
_NESTED_RULE = "a household script Hearth starts may send its own command to a remote it names literally, never one a caller fills in when it runs"
_INDIRECT_KEYS = ("device_id", "area_id", "floor_id", "label_id")


def _literal_remote_refusal(entity_id: Any, rule: str) -> str | None:
    """Why `entity_id` is not a literal `remote.*` id, or None when it is."""
    if not isinstance(entity_id, str) or _TEMPLATE_RE.search(entity_id):
        return f"a remote named by a template or a non-string cannot be checked; {rule}"
    eid = entity_id.strip().lower()
    if eid in ("all", "none"):
        return f"{eid} is not a remote Hearth can check; {rule}"
    if not eid.startswith("remote."):
        return f"{entity_id} is not a remote; {rule}"
    return None


def remote_platform_refusal(hass: HomeAssistant, entity_id: Any) -> str | None:
    """Why `entity_id` may not be sent or taught a remote command, or None when it may."""
    if why := _literal_remote_refusal(entity_id, _RULE):
        return why
    entry = er.async_get(hass).async_get(entity_id.strip().lower())
    if entry is None:
        return f"{entity_id} has no registry entry, so its integration cannot be checked; {_RULE}"
    if entry.platform not in REMOTE_NAMED_COMMAND_PLATFORMS:
        return f"{entity_id} belongs to {entry.platform}, which may play or write a command it has no stored name for; {_RULE}"
    return None


def remote_activity_refusal(hass: HomeAssistant, entity_id: Any, activity: Any) -> str | None:
    """Why `entity_id` may not be turned on, off or toggled with `activity` (#765), or None when it may.

    `androidtv_remote` launches whatever `activity` names, an app id or a URL included, so an activity
    is allowed only when it is a literal string the target itself offers in `activity_list`, which is
    what the household configured (the app names on Android TV, the activities on Harmony). Exact
    match, as both integrations compare. Read from the live state, so a remote that is unavailable, or
    lists nothing, is refused.
    """
    if why := _literal_remote_refusal(entity_id, _ACTIVITY_RULE):
        return why
    if not isinstance(activity, str) or _TEMPLATE_RE.search(activity):
        return f"an activity named by a template or a non-string cannot be checked; {_ACTIVITY_RULE}"
    state = hass.states.get(entity_id.strip().lower())
    listed = state.attributes.get("activity_list") if state is not None else None
    if not isinstance(listed, (list, tuple)) or activity not in listed:
        return f"{entity_id} does not list the activity {activity!r}; {_ACTIVITY_RULE}"
    return None


def _activities(node: dict[str, Any]) -> list[Any]:
    """Every `activity` a service node carries, in `data` or `data_template`."""
    return [c["activity"] for c in (node.get("data"), node.get("data_template")) if isinstance(c, dict) and "activity" in c]


def remote_call_violations(hass: HomeAssistant, domain: str, service: str, entity_ids: list[str], data: Any = None) -> list[str]:
    """The control path: `devices.call` names its targets as a literal list, and strips target keys from `data`."""
    folded = f"{_fold(domain)}.{_fold(service)}"
    full = f"{domain}.{service}"
    problems: list[str] = []
    if folded in _LIMITED:
        problems += [f"{full}: {why}" for eid in entity_ids if (why := remote_platform_refusal(hass, eid))]
    # `homeassistant.turn_on` forwards `data` to `remote.turn_on`, so every target must be a remote that lists it.
    if folded in _REMOTE_ACTIVITY_SERVICES and isinstance(data, dict) and "activity" in data:
        problems += [f"{full}: {why}" for eid in entity_ids if (why := remote_activity_refusal(hass, eid, data["activity"]))]
    return problems


def _ids(value: Any) -> list[Any]:
    """An `entity_id` value as `cv.comp_entity_ids` reads it: a comma-split string, or a list."""
    if isinstance(value, str):
        return [x for x in (p.strip() for p in value.split(",")) if x] if not _TEMPLATE_RE.search(value) else [value]
    if isinstance(value, list):
        return list(value)
    return [value]


def _node_violations(node: dict[str, Any], where: str, rule: str, check: Callable[[Any], str | None]) -> list[str]:
    """A service node's targets, read as a literal list of ids and each put to `check`; anything else refused."""
    problems: list[str] = []
    ids: list[Any] = []
    target = node.get("target")
    if target is not None and not isinstance(target, dict):
        problems.append(f"{where} has a templated target; {rule}")
    for name, carrier in (("data", node.get("data")), ("data_template", node.get("data_template"))):
        if isinstance(carrier, dict) and any(k in carrier for k in ("entity_id", *_INDIRECT_KEYS)):
            problems.append(f"{where} names its target in {name}; {rule}")
    if isinstance(target, dict) and any(not isinstance(k, str) or _TEMPLATE_RE.search(k) for k in target):
        problems.append(f"{where} has a templated target key; {rule}")
    for src in (node, target) if isinstance(target, dict) else (node,):
        for key in _INDIRECT_KEYS:
            if key in src:
                problems.append(f"{where} targets by {key}; {rule}")
        if "entity_id" in src:
            ids.extend(_ids(src["entity_id"]))
    if not ids and not problems:
        problems.append(f"{where} names no remote; {rule}")
    problems.extend(f"{where}: {why}" for eid in ids if (why := check(eid)))
    return problems


def _first(reasons: list[str | None]) -> str | None:
    return next((r for r in reasons if r), None)


def _visit_services(config: Any, path: str, each: Callable[[dict[str, Any], str, str], None]) -> None:
    """Call `each(node, action, path)` for every node in `config` that names a service."""

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if isinstance(action, str):
            each(node, action, p)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config, path)


def find_remote_violations(hass: HomeAssistant, config: Any, path: str = "config") -> list[str]:
    """Every `remote.send_command`, `remote.learn_command` and remote activity in an authored config, checked as a call is."""
    problems: list[str] = []

    def each(node: dict[str, Any], action: str, p: str) -> None:
        where = f"{p}: action {action}"
        if _fold(action) in _LIMITED:
            problems.extend(_node_violations(node, where, _RULE, lambda eid: remote_platform_refusal(hass, eid)))
        if _fold(action) in _REMOTE_ACTIVITY_SERVICES and (acts := _activities(node)):
            problems.extend(_node_violations(node, where, _ACTIVITY_RULE, lambda eid: _first([remote_activity_refusal(hass, eid, a) for a in acts])))

    _visit_services(config, path, each)
    return list(dict.fromkeys(problems))


def _filled_in(value: Any) -> bool:
    """Whether anything in `value` is decided when it runs: a template anywhere, a key included, or a non-JSON object."""
    if isinstance(value, str):
        return bool(_TEMPLATE_RE.search(value))
    if isinstance(value, dict):
        return any(_filled_in(k) or _filled_in(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_filled_in(v) for v in value)
    return not (value is None or isinstance(value, (int, float, bool)))


def _literal_id_refusal(entity_id: Any) -> str | None:
    if not isinstance(entity_id, str) or _TEMPLATE_RE.search(entity_id):
        return f"a remote named by a template or a non-string is filled in when it runs; {_NESTED_RULE}"
    if entity_id.strip().lower() in ("all", "none"):
        return f"{entity_id.strip().lower()} names every remote in the house rather than one; {_NESTED_RULE}"
    return None


def find_nested_remote_violations(config: Any, path: str) -> list[str]:
    """What a household script Hearth starts may not do with a remote (#765). No `hass`: nothing here reads the home.

    Run on a script one hop or more removed (`scenes.find_nested_script_violations`), where the script
    is the household's and the platform allowlist above deliberately does **not** apply: a household
    script that sends its own fixed command to its own fixed remote is the household's decision, which
    is AgDR-0005's accepted limit, and refusing it would take with it every Hearth automation that starts
    that script. What the caller can steer is refused: a target it can fill in (a template, a templated
    target or target key, a templated id, `all`) or one Hearth cannot list (an area, device or floor,
    which Hearth cannot resolve to a fixed list at authoring time, the `gated.py` rule, or a label,
    whose members a model with `labels.manage` can change), and for `send_command` and `learn_command`
    any template in `data`, which a variable passed to the script decides. An activity is held to the
    same target rule; a templated activity is already refused by the policy walk. The other way a caller
    fills in a household script's variables, `data` on a call to a group that holds it, is refused by
    `control.member_violations` and by the `variables` rule in `policy.py`.
    """
    problems: list[str] = []

    def each(node: dict[str, Any], action: str, p: str) -> None:
        folded = _fold(action)
        limited = folded in _LIMITED
        if not limited and not (folded in _REMOTE_ACTIVITY_SERVICES and _activities(node)):
            return
        where = f"{p}: action {action}"
        problems.extend(_node_violations(node, where, _NESTED_RULE, _literal_id_refusal))
        if limited and any(_filled_in(node.get(k)) for k in ("data", "data_template")):
            problems.append(f"{where} fills in its data when it runs; {_NESTED_RULE}")

    _visit_services(config, path, each)
    return list(dict.fromkeys(problems))
