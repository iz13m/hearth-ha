"""Action policy — mirror of packages/shared/src/policy.ts. Enforced before any config is written.

The allowlist stops the hub from calling services directly; this stops it from smuggling
denied services into automations/scripts/scenes that HA would then run on its behalf.
"""

from __future__ import annotations

import re
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
        # Hearth never triggers an automation, and never flips one on or off from inside a config it
        # authored. `automations.set_enabled` is the one sanctioned path and it is an RPC method
        # under `automations.write`, checked and audited as itself — not a line a model can bury in
        # a script, where turning an automation off is how you quietly disarm someone else's rule.
        "automation.trigger",
        "automation.turn_on",
        "automation.turn_off",
        "automation.toggle",
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
    # `full` keeps the caller's spelling, because it is what the messages quote back. Every
    # *comparison* uses the folded pair — mixing the two is how mixed case walked past
    # `DENIED_ACTIONS` here while the hub refused it: before the fold existed, the malformed-name
    # guard below rejected `Homeassistant.Restart` outright, so folding made that guard pass and
    # left a raw string being tested against a lower-case list.
    full = f"{domain}.{service}"
    raw_domain = domain
    domain, service = _fold(domain), _fold(service)
    folded = f"{domain}.{service}"
    if not re.fullmatch(r"[a-z0-9_]+", domain) or not re.fullmatch(r"[a-z0-9_]+", service):
        return [f"{full}: malformed service name"]
    if domain in DENIED_ACTION_DOMAINS:
        problems.append(f"{full}: domain {raw_domain} can never be controlled")
    if folded in DENIED_ACTIONS:
        problems.append(f"{full}: not allowed")
    if domain in ROUTINE_DOMAINS:
        problems.append(f"{full}: use activate_scene or run_script instead of calling the {raw_domain} domain directly")
    if not entity_ids:
        problems.append(f"{full}: at least one entity_id is required (broad targets are not allowed)")
    for eid in entity_ids:
        d = _domain(eid)
        if not d:
            problems.append(f"{eid}: malformed entity_id")
        elif d in DENIED_ENTITY_DOMAINS:
            problems.append(f"{eid}: entities in {d} can never be controlled")
        elif d in ROUTINE_DOMAINS:
            problems.append(f"{eid}: use activate_scene or run_script for {d} entities")
    return list(dict.fromkeys(problems))


_LIST_KEYS = {"actions", "action", "sequence", "then", "else", "default", "parallel", "choose", "options", "repeat", "if", "conditions"}


def _domain(value: Any) -> str | None:
    """The domain part of `domain.object_id`, **case-folded** (#220).

    Home Assistant decides case *after* we do: `cv.service` and `cv.entity_id` both `.lower()` their
    input (`helpers/config_validation.py`), so `Lock.Unlock` is stored raw, passes a walk that
    compares against lower-case lists, and runs as `lock.unlock`. Folded here rather than at each
    call site, so a future check cannot forget to do it.
    """
    if not isinstance(value, str) or "." not in value:
        return None
    return value.split(".", 1)[0].lower()




def _fold(value: Any) -> str:
    """A whole service name or entity id as Home Assistant will read it. See `_domain`."""
    return value.lower() if isinstance(value, str) else ""


def _entity_ids(target: Any) -> list[str]:
    if not isinstance(target, dict):
        return []
    v = target.get("entity_id")
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def _scene_entity_ids(node: dict[str, Any]) -> list[str]:
    """The entities a `scene` service call sets or snapshots — mirror of sceneEntityIds in policy.ts.

    `scene.apply` takes `{entities: {"lock.front_door": {state: "unlocked"}}}` and `scene.create`
    takes `{snapshot_entities: ["lock.front_door"]}`. Neither puts an id where `_entity_ids` looks —
    one hides it in an object *key*, the other in a bare list — so a lock could ride into an
    automation as scene data and HA would apply it on the model's behalf.
    """
    out: list[str] = []

    def push(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(x for x in value if isinstance(x, str))

    for src in (node, node.get("data")):
        if not isinstance(src, dict):
            continue
        entities = src.get("entities")
        if isinstance(entities, dict):
            out.extend(str(k) for k in entities)
        else:
            push(entities)
        push(src.get("snapshot_entities"))
    return out


REFERENCEABLE_DENIED_DOMAINS: frozenset[str] = DENIED_ENTITY_DOMAINS | {"image"}
"""Domains a model may never name anywhere — mirror of REFERENCEABLE_DENIED_DOMAINS in policy.ts.

`DENIED_ENTITY_DOMAINS` plus `image`: the read path hides `image` while the action policy does not,
and something that merely *reads* an entity is filtered by the read path's union. Keeping the same
union here makes "never listed, never readable" and "never referenceable" one set; `OFF_LIMITS` in
`handlers/registry.py` is the other mirror.
"""

HOST_SERVICE_DOMAINS: frozenset[str] = frozenset({"shell_command", "python_script", "hassio", "backup"})
"""Service domains that are host-level wherever they appear, including inside a template string."""

# `IGNORECASE`, because this one reads raw text rather than a parsed id: `Lock.Front_Door` names the
# same entity HA will open, and a case-sensitive pattern is blind to it (#220).
_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_/\-])(" + "|".join(sorted(REFERENCEABLE_DENIED_DOMAINS | HOST_SERVICE_DOMAINS)) + r")\.[a-z0-9_]+",
    re.IGNORECASE,
)
_TEMPLATE_RE = re.compile(r"\{\{|\{%")


def find_reference_violations(value: Any, path: str = "input") -> list[str]:
    """Every place a value names an entity in a domain the model may never reach (AgDR-0038).

    Mirror of `findReferenceViolations` in policy.ts. `find_policy_violations` only inspects the
    target of a node that already carries an `action`, which is right for an automation and not
    enough for a config-flow form: a `trend` helper's whole configuration is
    `{"entity_id": "lock.front_door"}` and a template sensor's is `{{ states('lock.front_door') }}`.

    Complete for the helpers whose fields are entity pickers and numbers; a **backstop, not a
    boundary**, for anything Home Assistant renders as a template, since
    `{{ states('lo' ~ 'ck.front_door') }}` renders the same and no regex will see it.
    """
    problems: list[str] = []

    def scan(text: str, p: str) -> None:
        for m in _REFERENCE_RE.finditer(text):
            problems.append(f"{p}: {m.group(0)} is in a domain Hearth never lets a model reach")

    def visit(node: Any, p: str) -> None:
        if isinstance(node, str):
            scan(node, p)
        elif isinstance(node, list):
            for i, n in enumerate(node):
                visit(n, f"{p}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                scan(key, f"{p}.{key}")
                visit(v, f"{p}.{key}")

    visit(value, path)
    return list(dict.fromkeys(problems))


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
            # A templated action name is refused outright: the service it would call cannot be known
            # until it runs, and `_domain("{{ 'lock.unlock' }}")` is `{{ 'lock`, which matches nothing.
            if _TEMPLATE_RE.search(action):
                problems.append(f"{p}: action {action} is a template; the service it would call cannot be known until it runs")
            dom = _domain(action)
            if dom in DENIED_ACTION_DOMAINS:
                problems.append(f"{p}: action {action} targets denied domain {dom}")
            if _fold(action) in DENIED_ACTIONS:
                problems.append(f"{p}: action {action} is not allowed")
            ids = _entity_ids(node.get("target")) + _entity_ids(node.get("data")) + _entity_ids(node)
            if dom == "scene":
                ids += _scene_entity_ids(node)
            for eid in ids:
                if _domain(eid) in DENIED_ENTITY_DOMAINS:
                    problems.append(f"{p}: entity {eid} is in denied domain {_domain(eid)}")
            # An action that decides its own blast radius from its target (#220). After the
            # denied-domain sweep above, which still applies to whatever ids it does name.
            problems.extend(find_fan_out_violations(action, node, ids, p))
        # A device action names no entity and carries no service: `{device_id, domain: "lock",
        # type: "unlock"}` has no `action` key at all, so everything above skips it while HA still
        # opens the door. The check is therefore on the shape and sits outside the action branch.
        # It catches a device *trigger* and *condition* too, deliberately — the same three keys are
        # all three shapes, and a lock is something the model may not even read the state of.
        device_domain = node.get("domain")
        if isinstance(device_domain, str) and _fold(device_domain) in DENIED_ENTITY_DOMAINS and ("type" in node or "device_id" in node):
            problems.append(f"{p}: device in denied domain {device_domain}")
        for k, v in node.items():
            if k in _LIST_KEYS or isinstance(v, (dict, list)):
                visit(v, f"{p}.{k}")

    visit(config, path)
    return list(dict.fromkeys(problems))



# Target keys Home Assistant resolves against its own registries, which we cannot read here.
# `scene.turn_on` has refused these since AgDR-0042; `fans_out` generalises that rule.
# A tuple, in policy.ts's declaration order: `test_policy.py` compares the two walkers' messages
# as ordered lists, so a target carrying two of these must produce them in the same order here.
INDIRECT_TARGET_KEYS = ("device_id", "area_id", "floor_id", "label_id")

# A group's membership lives in Home Assistant, so a `group.*` id is as unresolvable as an area.
_GROUP_DOMAIN = "group"


def fans_out(action: str) -> bool:
    """Whether an action's effect is decided by something other than its own domain (#220).

    Mirror of `fansOut` in packages/shared/src/policy.ts; see that doc comment for how the list was
    derived from the pinned Home Assistant rather than written from memory.
    """
    dom = _domain(action)
    folded = _fold(action)
    if dom == "homeassistant":
        return folded[len(dom) + 1 :] in {"turn_on", "turn_off", "toggle"}
    if folded == "scene.turn_on":
        return True
    if dom == "script":
        return folded[len(dom) + 1 :] not in {"reload", "turn_off"}
    return False


def find_fan_out_violations(action: str, node: dict[str, Any], ids: list[str], p: str) -> list[str]:
    """What a fan-out action may not carry: a target we cannot resolve, and so cannot check.

    A refusal rather than a resolution, deliberately. Neither side can resolve an `area_id` at
    authoring time to a set that stays true afterwards — someone re-labelling a camera would change
    it. Refusing what cannot be checked is the only answer that survives the config being written.

    Home Assistant registers the generic services with `extra=vol.ALLOW_EXTRA`, so `entity_id` is
    validated by `cv.entity_ids` (which is why `entity_id: all` dies there) while these four pass
    through untouched and are resolved later by `async_extract_referenced_entity_ids`. That
    asymmetry is exactly why this vector is open and that one is not.
    """
    problems: list[str] = []
    if not fans_out(action):
        return problems
    target = node.get("target") if isinstance(node.get("target"), dict) else {}
    for key in INDIRECT_TARGET_KEYS:
        if key in target or key in node:
            problems.append(f"{p}: action {action} decides what it touches from its target, so it may not use {key} — name the entities")
    for eid in ids:
        # The same reasoning that refuses a templated *action* name, applied to the target: HA
        # renders it at run time (`template.render_complex(conf, variables)` in
        # `helpers/service.py`, checked against the pinned 2026.9.3), so `{{ 'scene.gate' }}` reads
        # here as an ordinary string in no denied domain and is a scene by the time it runs. Only
        # for an action that fans out — `light.turn_on` at a templated id still reaches only a light.
        if _TEMPLATE_RE.search(eid):
            problems.append(
                f"{p}: action {action} decides what it touches from its target, and {eid} is a template; "
                "what it would reach cannot be known until it runs"
            )
            continue
        ed = _domain(eid)
        if ed == _GROUP_DOMAIN:
            problems.append(f"{p}: action {action} aimed at {eid} reaches whatever that group holds, which is not knowable here")
        # The alias is how `automation.turn_off` and `script.turn_on` come back under another name:
        # both are refused as direct actions, and neither becomes safe for being reached indirectly.
        if ed in ROUTINE_DOMAINS and _domain(action) == "homeassistant":
            article = "an" if ed == "automation" else "a"
            problems.append(f"{p}: action {action} aimed at {eid} runs {article} {ed}; use activate_scene or run_script")
    # Only the dispatcher: HA refuses it without a target anyway, but a target made only of keys we
    # just refused would otherwise read as "no problem found". A per-script service such as
    # `script.my_script` legitimately names no entities, which is why this is not general.
    if not ids and _domain(action) == "homeassistant":
        problems.append(f"{p}: action {action} needs the entities it acts on named here")
    return problems


def find_scene_policy_violations(config: Any) -> list[str]:
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    return [f"entities.{eid}: denied domain {_domain(eid)}" for eid in entities if _domain(eid) in DENIED_ENTITY_DOMAINS]
