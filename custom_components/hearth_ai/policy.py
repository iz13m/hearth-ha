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
    """The entity ids a service-call node names, read as Home Assistant reads them (#228).

    **Comma-split**, because `cv.comp_entity_ids` is: on the pinned 2026.9.3,
    `comp_entity_ids("input_boolean.test, automation.security")` returns both ids. Taking the string
    whole meant the walk saw one harmless entity while HA acted on two.
    """
    if not isinstance(target, dict):
        return []
    v = target.get("entity_id")
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [x.strip() for x in v if isinstance(x, str)]
    return []


def _service_name(node: dict[str, Any]) -> Any:
    """How a service-call node names the service it runs, per `cv.SERVICE_SCHEMA` (#228).

    `action` and `service_template` are `vol.Exclusive` members of one `"service name"` group, and
    `has_at_least_one_key(CONF_ACTION, CONF_SERVICE_TEMPLATE)` requires one of them — so
    `service_template` is not a legacy alias to overlook, it is one of exactly two ways the schema
    allows a service to be named. `service` is renamed to `action` upstream by
    `_backward_compat_service_schema`, which is why it is read here and `service_template` survives
    beside it. We read one of the two, which is why the same instruction was refused spelled
    `action:` and ran spelled `service_template:`.
    """
    for key in ("action", "service", "service_template"):
        # `is not None`, not `in`: TypeScript's `??` skips a null and falls through to the next
        # spelling, so `{"action": null, "service_template": "lock.unlock"}` was refused on the hub
        # and allowed here. JSON can express that, so it is a real mirror break even though Home
        # Assistant rejects `None` at `cv.service` (#228 review).
        if node.get(key) is not None:
            return node[key]
    # `cv.ACTIONS_MAP` maps those three to `call_service` and `scene` to its own action *type*: a
    # `{scene: "scene.gate"}` node activates a scene while naming no service, so a check keyed on
    # the service name never sees it. Normalised so every rule treats the shorthand as what it is.
    if "scene" in node:
        return "scene.turn_on"
    return None


# Every key of a service-call node that can carry the entities it acts on, per `cv.SERVICE_SCHEMA`.
# `data_template` sits beside `data` in the schema and `template_complex` renders it the same way.
# `SCRIPT_ACTION_BASE_SCHEMA` contributes only `alias`, `note`, `continue_on_error` and `enabled` —
# enumerated, not assumed — and `response_variable`/`metadata` name no entities.
TARGET_BEARING_KEYS = ("target", "data", "data_template")


def _data_is_variables(action: str) -> bool:
    """Whether `data` on this action is service data or a script's **variables** (#228 review).

    Home Assistant has two paths: `script.turn_on` reads variables from
    `service.data.get(ATTR_VARIABLES)`, so a legacy `entity_id`/`device_id` in its `data` really is
    a target selector. The **per-script service** `script.<id>` passes `variables=service.data` —
    the whole mapping becomes the script's variables and selects nothing. So refusing
    `{action: "script.notify_phone", data: {device_id: "abc"}}` was over-refusal, and that is how
    every "notify this device" script is invoked.
    """
    dom = _domain(action)
    return dom == "script" and _fold(action).split(".", 1)[1] not in ("turn_on", "toggle", "reload", "turn_off")


def _reach_decided_by_data(action: str) -> bool:
    """Actions whose reach is decided by what is inside `data` rather than by the service name.

    `cv.SERVICE_SCHEMA` gives `data`/`data_template` **two** alternatives —
    `Any(template, All(dict, template_complex))` — and only the dict was read, so a whole-template
    `data` contributed nothing to any structural check. That let the target *selection* be
    laundered: `scene.apply` with `data="{{ {'entities': {'lo' ~ 'ck.front_door': ...}} }}"` set a
    lock, and no regex can catch the `area_id` form because an area name is not an entity id.
    """
    return fans_out(action) or _fold(action) in ("scene.apply", "scene.create")


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
        action = _service_name(node)
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
            ids = [i for k in TARGET_BEARING_KEYS for i in _entity_ids(node.get(k))] + _entity_ids(node) + _entity_ids({"entity_id": node.get("scene")})
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


def _target_selection(node: dict[str, Any], action: str) -> tuple[set[str], list[str], bool]:
    """**One view of how an action selects what it touches**, from every carrier key (#228).

    `target`, `data` and `data_template` are three spellings of target selection, and
    `cv.SERVICE_SCHEMA` declares each as `Any(template, dict)` — two shapes behind one name. The
    predicate had three call sites which then diverged: the templated form was refused on `target`
    and invisible on the other two, so the same input laundered through a different spelling walked
    past the rule. Normalise first, decide once; a fourth carrier key is then one line here.

    Returns (keys, ids, unknowable). The dict alternative contributes keys and ids; the template
    alternative contributes `unknowable`, which is what it is at authoring time (AgDR-0042).
    """
    keys: set[str] = set()
    ids: list[str] = []
    unknowable = False
    # For the per-script service `script.<id>`, `data` is the script's variables rather than a
    # target — see `_data_is_variables`. Its `target` still selects.
    carriers = ("target",) if _data_is_variables(action) else TARGET_BEARING_KEYS
    for key in carriers:
        value = node.get(key)
        if isinstance(value, str):
            unknowable = True
        elif isinstance(value, dict):
            keys.update(str(k) for k in value)
            ids.extend(_entity_ids(value))
    keys.update(k for k in INDIRECT_TARGET_KEYS if k in node)
    ids.extend(_entity_ids(node))
    ids.extend(_entity_ids({"entity_id": node.get("scene")}))
    return keys, ids, unknowable


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
    keys, _sel_ids, unknowable = _target_selection(node, action)
    # An unknowable selection is refused wherever the action's reach is decided by that selection
    # rather than bounded by its own service name: `fans_out` **plus** `scene.apply`/`scene.create`,
    # which are domain-bounded services whose data inlines entity states of any domain. Checked
    # against `fans_out` rather than assumed — the predicate is phrased "not bounded by its own
    # domain", which a scene service passes.
    if unknowable and _reach_decided_by_data(action):
        problems.append(
            f"{p}: action {action} decides what it touches from its target, and that target is a "
            "template; what it would reach cannot be known until it runs"
        )
    if not fans_out(action):
        return problems
    for key in INDIRECT_TARGET_KEYS:
        if key in keys:
            problems.append(f"{p}: action {action} decides what it touches from its target, so it may not use {key} — name the entities")
    for eid in ids:
        if _TEMPLATE_RE.search(eid):
            problems.append(
                f"{p}: action {action} decides what it touches from its target, and {eid} is a "
                "template; what it would reach cannot be known until it runs"
            )
            continue
        ed = _domain(eid)
        if ed == _GROUP_DOMAIN:
            problems.append(f"{p}: action {action} aimed at {eid} reaches whatever that group holds, which is not knowable here")
        if ed in ROUTINE_DOMAINS and _domain(action) == "homeassistant":
            article = "an" if ed == "automation" else "a"
            problems.append(f"{p}: action {action} aimed at {eid} runs {article} {ed}; use activate_scene or run_script")
    if not ids and not unknowable and _domain(action) == "homeassistant":
        problems.append(f"{p}: action {action} needs the entities it acts on named here")
    return problems


def find_scene_policy_violations(config: Any) -> list[str]:
    entities = config.get("entities") if isinstance(config, dict) else None
    if not isinstance(entities, dict):
        return []
    return [f"entities.{eid}: denied domain {_domain(eid)}" for eid in entities if _domain(eid) in DENIED_ENTITY_DOMAINS]
