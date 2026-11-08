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
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..policy import _TEMPLATE_RE, _fold, _service_name

REMOTE_NAMED_COMMAND_PLATFORMS: frozenset[str] = frozenset({"broadlink"})

_LIMITED = frozenset({"remote.send_command", "remote.learn_command"})
_RULE = f"Hearth sends or teaches remote commands only to a remote whose integration plays and learns stored names alone ({', '.join(sorted(REMOTE_NAMED_COMMAND_PLATFORMS))})"
_INDIRECT_KEYS = ("device_id", "area_id", "floor_id", "label_id")


def remote_platform_refusal(hass: HomeAssistant, entity_id: Any) -> str | None:
    """Why `entity_id` may not be sent or taught a remote command, or None when it may."""
    if not isinstance(entity_id, str) or _TEMPLATE_RE.search(entity_id):
        return f"a remote named by a template or a non-string cannot be checked; {_RULE}"
    eid = entity_id.strip().lower()
    if eid in ("all", "none"):
        return f"{eid} is not a remote Hearth can check; {_RULE}"
    if not eid.startswith("remote."):
        return f"{entity_id} is not a remote; {_RULE}"
    entry = er.async_get(hass).async_get(eid)
    if entry is None:
        return f"{entity_id} has no registry entry, so its integration cannot be checked; {_RULE}"
    if entry.platform not in REMOTE_NAMED_COMMAND_PLATFORMS:
        return f"{entity_id} belongs to {entry.platform}, which may play or write a command it has no stored name for; {_RULE}"
    return None


def remote_call_violations(hass: HomeAssistant, domain: str, service: str, entity_ids: list[str]) -> list[str]:
    """The control path: `devices.call` names its targets as a literal list, and strips target keys from `data`."""
    if f"{_fold(domain)}.{_fold(service)}" not in _LIMITED:
        return []
    full = f"{domain}.{service}"
    return [f"{full}: {why}" for eid in entity_ids if (why := remote_platform_refusal(hass, eid))]


def _ids(value: Any) -> list[Any]:
    """An `entity_id` value as `cv.comp_entity_ids` reads it: a comma-split string, or a list."""
    if isinstance(value, str):
        return [x for x in (p.strip() for p in value.split(",")) if x] if not _TEMPLATE_RE.search(value) else [value]
    if isinstance(value, list):
        return list(value)
    return [value]


def _node_violations(hass: HomeAssistant, node: dict[str, Any], action: str, p: str) -> list[str]:
    where = f"{p}: action {action}"
    problems: list[str] = []
    ids: list[Any] = []
    target = node.get("target")
    if target is not None and not isinstance(target, dict):
        problems.append(f"{where} has a templated target; {_RULE}")
    for name, carrier in (("data", node.get("data")), ("data_template", node.get("data_template"))):
        if isinstance(carrier, dict) and any(k in carrier for k in ("entity_id", *_INDIRECT_KEYS)):
            problems.append(f"{where} names its target in {name}; {_RULE}")
    if isinstance(target, dict) and any(not isinstance(k, str) or _TEMPLATE_RE.search(k) for k in target):
        problems.append(f"{where} has a templated target key; {_RULE}")
    for src in (node, target) if isinstance(target, dict) else (node,):
        for key in _INDIRECT_KEYS:
            if key in src:
                problems.append(f"{where} targets by {key}; {_RULE}")
        if "entity_id" in src:
            ids.extend(_ids(src["entity_id"]))
    if not ids and not problems:
        problems.append(f"{where} names no remote; {_RULE}")
    problems.extend(f"{where}: {why}" for eid in ids if (why := remote_platform_refusal(hass, eid)))
    return problems


def find_remote_send_violations(hass: HomeAssistant, config: Any, path: str = "config") -> list[str]:
    """Every `remote.send_command` and `remote.learn_command` in an authored config, checked as a call is."""
    problems: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = _service_name(node)
        if isinstance(action, str) and _fold(action) in _LIMITED:
            problems.extend(_node_violations(hass, node, action, p))
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config, path)
    return list(dict.fromkeys(problems))
