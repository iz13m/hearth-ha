"""Action policy — mirror of packages/shared/src/policy.ts. Enforced before any config is written.

The allowlist stops the hub from calling services directly; this stops it from smuggling
denied services into automations/scripts/scenes that HA would then run on its behalf.
"""

from __future__ import annotations

from typing import Any

DENIED_ACTION_DOMAINS: frozenset[str] = frozenset(
    {
        "lock",
        "alarm_control_panel",
        "camera",
        "shell_command",
        "python_script",
        "hassio",
        "update",
        "device_tracker",
        "person",
        "ffmpeg",
        "stream",
        "backup",
        "recorder",
        "onboarding",
        "auth",
        "cloud",
    }
)
DENIED_ACTIONS: frozenset[str] = frozenset(
    {
        "homeassistant.restart",
        "homeassistant.stop",
        "homeassistant.reload_all",
        "homeassistant.reload_core_config",
        "homeassistant.reload_config_entry",
        "homeassistant.set_location",
        "system_log.write",
        "logger.set_level",
        "persistent_notification.dismiss_all",
    }
)
DENIED_ENTITY_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel", "camera", "device_tracker", "person"})

# Domains handled by their own capability, so device control never implicitly grants the
# ability to run user-authored sequences.
ROUTINE_DOMAINS: frozenset[str] = frozenset({"scene", "script", "automation"})


def find_service_call_violations(domain: str, service: str, entity_ids: list[str]) -> list[str]:
    """Mirror of findServiceCallViolations in packages/shared/src/policy.ts."""
    import re  # noqa: PLC0415

    problems: list[str] = []
    full = f"{domain}.{service}"
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
        return [f"{full}: malformed service name"]
    if domain in DENIED_ACTION_DOMAINS:
        problems.append(f"{full}: domain {domain} can never be controlled")
    if full in DENIED_ACTIONS:
        problems.append(f"{full}: not allowed")
    if domain in ROUTINE_DOMAINS:
        problems.append(f"{full}: use scenes.activate or scripts.run instead of calling the {domain} domain directly")
    if not entity_ids:
        problems.append(f"{full}: at least one entity_id is required (broad targets are not allowed)")
    for eid in entity_ids:
        d = _domain(eid)
        if not d:
            problems.append(f"{eid}: malformed entity_id")
        elif d in DENIED_ENTITY_DOMAINS:
            problems.append(f"{eid}: entities in {d} can never be controlled")
        elif d in ROUTINE_DOMAINS:
            problems.append(f"{eid}: use scenes.activate or scripts.run for {d} entities")
    return list(dict.fromkeys(problems))


_LIST_KEYS = {"actions", "action", "sequence", "then", "else", "default", "parallel", "choose", "options", "repeat", "if", "conditions"}


def _domain(value: Any) -> str | None:
    if not isinstance(value, str) or "." not in value:
        return None
    return value.split(".", 1)[0]


def _entity_ids(target: Any) -> list[str]:
    if not isinstance(target, dict):
        return []
    v = target.get("entity_id")
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def find_policy_violations(config: Any, path: str = "config") -> list[str]:
    problems: list[str] = []

    def visit(node: Any, p: str) -> None:
        if isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
            return
        if not isinstance(node, dict):
            return
        action = node.get("action", node.get("service"))
        if isinstance(action, str):
            dom = _domain(action)
            if dom in DENIED_ACTION_DOMAINS:
                problems.append(f"{p}: action {action} targets denied domain {dom}")
            if action in DENIED_ACTIONS:
                problems.append(f"{p}: action {action} is not allowed")
            for eid in _entity_ids(node.get("target")) + _entity_ids(node.get("data")) + _entity_ids(node):
                if _domain(eid) in DENIED_ENTITY_DOMAINS:
                    problems.append(f"{p}: entity {eid} is in denied domain {_domain(eid)}")
        for k, v in node.items():
            if k in _LIST_KEYS or isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config, path)
    return list(dict.fromkeys(problems))


def find_scene_policy_violations(config: Any) -> list[str]:
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    return [f"entities.{eid}: denied domain {_domain(eid)}" for eid in entities if _domain(eid) in DENIED_ENTITY_DOMAINS]
