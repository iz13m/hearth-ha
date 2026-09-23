"""Policy lists match the shared package and denied actions are refused before any write."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.hearth_ai.policy import (
    TARGET_BEARING_KEYS,
    DENIED_ACTION_DOMAINS,
    DENIED_ACTIONS,
    DENIED_ENTITY_DOMAINS,
    HOST_SERVICE_DOMAINS,
    REFERENCEABLE_DENIED_DOMAINS,
    _fold,
    _service_name,
    find_policy_violations,
    find_reference_violations,
    find_scene_policy_violations,
    find_service_call_violations,
)

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def test_lists_match_shared() -> None:
    data = json.loads(SCHEMA.read_text())
    assert set(data["denied_action_domains"]) == DENIED_ACTION_DOMAINS
    assert set(data["denied_actions"]) == DENIED_ACTIONS
    assert set(data["denied_entity_domains"]) == DENIED_ENTITY_DOMAINS
    assert set(data["referenceable_denied_domains"]) == REFERENCEABLE_DENIED_DOMAINS
    assert set(data["host_service_domains"]) == HOST_SERVICE_DOMAINS


def test_nested_denied_actions_found() -> None:
    cfg = {
        "alias": "evil",
        "triggers": [{"trigger": "time_pattern", "seconds": "/5"}],
        "actions": [
            {"action": "lock.unlock", "target": {"entity_id": "lock.front"}},
            {"choose": [{"conditions": [], "sequence": [{"service": "shell_command.rm"}]}], "default": [{"action": "homeassistant.restart"}]},
            {"repeat": {"count": 3, "sequence": [{"action": "switch.turn_on", "target": {"entity_id": ["switch.a", "camera.b"]}}]}},
        ],
    }
    v = find_policy_violations(cfg)
    assert any("lock.unlock" in x for x in v)
    assert any("shell_command.rm" in x for x in v)
    assert any("homeassistant.restart" in x for x in v)
    assert any("camera.b" in x for x in v)
    assert find_policy_violations({"alias": "ok", "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.a"}}]}) == []
    assert find_scene_policy_violations({"entities": {"lock.a": "locked"}}) == ["entities.lock.a: denied domain lock"]


@pytest.mark.usefixtures("core")
async def test_handlers_refuse_denied_configs(core, tmp_path) -> None:  # noqa: ANN001
    from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

    d = build_dispatcher(core)
    bad = {"alias": "unlock", "triggers": [{"trigger": "sun", "event": "sunset"}], "actions": [{"action": "lock.unlock", "target": {"entity_id": "lock.front"}}]}
    res = await d.dispatch("automations.validate", {"config": bad})
    assert res["ok"] is False and res["status"] == "policy"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": bad})
    assert ei.value.code == "validation_failed"
    assert not (tmp_path / "automations.yaml").read_text().strip("[]\n")
    with pytest.raises(RpcError):
        await d.dispatch("scripts.create", {"config": {"alias": "x", "sequence": [{"action": "shell_command.x"}]}})
    with pytest.raises(RpcError):
        await d.dispatch("scenes.create", {"config": {"name": "x", "entities": {"lock.a": "unlocked"}}})


def test_the_reference_walkers_agree_on_every_shared_case() -> None:
    """
    The same form values, the same verdicts, in both languages (AgDR-0038).

    These cases were mirrored by hand here and in `policy.test.ts` until #220, which is the same
    arrangement that let `scene.apply` be caught on one side only (#126) — and it mattered at once:
    half of #220's fix is a case fold in *this* walker, because a config-flow form value carries no
    action and `find_policy_violations` returns `[]` for it by design. A fold applied to one walker
    and not the other now fails here instead of shipping as an open form path.
    """
    cases = json.loads(SCHEMA.read_text())["reference_walk_cases"]
    assert len(cases) > 10, "run pnpm --filter @hearth/shared export:jsonschema first"
    for case in cases:
        assert find_reference_violations(case["config"]) == case["violations"], case["name"]


def test_a_templated_action_name_is_refused() -> None:
    v = find_policy_violations({"actions": [{"action": "{{ 'lock.unlock' }}"}]})
    assert any("is a template" in x for x in v)
    assert find_policy_violations({"actions": [{"action": "light.turn_on"}]}) == []


def test_the_walkers_agree_on_every_shared_case() -> None:
    """
    The same configs, the same verdicts, in both languages.

    `test_lists_match_shared` pins the denied lists, and the cases below were mirrored by hand —
    which is how `scene.apply` came to be caught on the hub and not here (#126). The corpus in
    `schema/methods.json` carries the verdict the TypeScript walker gives each case, so a rule added
    to one side and forgotten on the other fails this test instead of shipping as a hole.
    """
    cases = json.loads(SCHEMA.read_text())["policy_walk_cases"]
    assert len(cases) > 10, "run pnpm --filter @hearth/shared export:jsonschema first"
    for case in cases:
        assert find_policy_violations(case["config"]) == case["violations"], case["name"]


def test_a_lock_hiding_in_scene_data_is_found() -> None:
    """Mirror of the policy.test.ts cases for #126."""
    apply_lock = {"actions": [{"action": "scene.apply", "data": {"entities": {"lock.front_door": {"state": "unlocked"}}}}]}
    assert any("lock.front_door" in x for x in find_policy_violations(apply_lock))
    assert len(find_policy_violations({"actions": [{"action": "scene.create", "snapshot_entities": ["lock.front_door"]}]})) == 1
    assert find_policy_violations({"actions": [{"action": "scene.apply", "data": {"entities": {"light.lamp": {"state": "on"}}}}]}) == []


def test_a_device_action_in_a_denied_domain_is_refused() -> None:
    v = find_policy_violations({"actions": [{"device_id": "abc", "domain": "lock", "type": "unlock"}]})
    assert v == ["config.actions[0]: device in denied domain lock"]
    assert len(find_policy_violations({"triggers": [{"trigger": "device", "device_id": "a", "domain": "lock", "type": "locked"}]})) == 1
    assert find_policy_violations({"actions": [{"device_id": "abc", "domain": "light", "type": "turn_on"}]}) == []


def test_an_automation_is_never_triggered_or_flipped_from_a_config() -> None:
    for action in ("automation.trigger", "automation.turn_on", "automation.turn_off", "automation.toggle"):
        assert len(find_policy_violations({"actions": [{"action": action}]})) == 1
    assert find_policy_violations({"actions": [{"action": "automation.reload"}]}) == []


@pytest.mark.usefixtures("core")
async def test_a_blueprint_cannot_smuggle_a_denied_action_past_the_walk(core, tmp_path) -> None:  # noqa: ANN001
    """
    A config that is nothing but `use_blueprint` carries no actions for the walk to read (#220).

    Both walkers answer `[]` for it — correctly, there is nothing there — while Home Assistant
    expands it into whatever the blueprint does. This is why `_validate` walks the config a second
    time as `async_validate_config_item` returns it: that is the only form in which the blueprint's
    body exists. Written against the pinned 2026.9.3, where the expansion really does happen here.
    """
    import pathlib

    from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

    d = pathlib.Path(core.config.path("blueprints/automation/hearth_probe"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "p.yaml").write_text(
        "blueprint:\n"
        "  name: probe\n"
        "  domain: automation\n"
        "  input:\n"
        "    delay:\n"
        "      name: delay\n"
        "triggers:\n"
        "  - trigger: time_pattern\n"
        "    seconds: \"/5\"\n"
        "actions:\n"
        "  - action: shell_command.rm\n"
        "  - action: lock.unlock\n"
        "    target:\n"
        "      entity_id: lock.front_door\n"
        "  - delay: !input delay\n"
    )
    cfg = {"alias": "looks harmless", "use_blueprint": {"path": "hearth_probe/p.yaml", "input": {"delay": "00:00:05"}}}

    # The walk on the config as written finds nothing — that is the whole point of the case.
    assert find_policy_violations(cfg) == []
    assert find_reference_violations(cfg, "config") == []

    d2 = build_dispatcher(core)
    res = await d2.dispatch("automations.validate", {"config": cfg})
    assert res["ok"] is False and res["status"] == "policy"
    assert "shell_command.rm" in res["error"] and "lock.unlock" in res["error"]
    with pytest.raises(RpcError):
        await d2.dispatch("automations.create", {"config": cfg})


def test_a_kelvin_sign_is_folded_into_the_denied_service_it_names() -> None:
    """Mirror of `the two walkers fold the same way` in policy.test.ts (#220).

    U+212A is the only codepoint at or above 0x80 whose lowercase is pure ASCII, swept over
    0x80..0x10FFFF on both runtimes rather than sampled, and Home Assistant folds it:
    `cv.service("LOC\u212a.UNLOCK")` returns `"lock.unlock"` on the pinned 2026.9.3. An ASCII-only
    fold would leave `loc\u212a.unlock`, which is in no denied list.
    """
    assert _fold("LOC\u212a.UNLOCK") == "lock.unlock"
    assert find_policy_violations({"actions": [{"action": "LOC\u212a.UNLOCK", "target": {"entity_id": "LOC\u212a.FRONT_DOOR"}}]}) != []


def test_a_long_s_matches_a_reference_the_way_the_hub_does() -> None:
    """U+017F is not a `.lower()` divergence — both runtimes leave it alone.

    It diverged because the reference regex folds via the engine: `re.IGNORECASE` is full Unicode
    here, and JavaScript only matches that under its `u` flag, which `policy.ts` now sets.
    """
    assert find_reference_violations({"entity_id": "\u017fhell_command.rm"}) != []
    assert find_reference_violations({"entity_id": "ba\u017fkup.now"}) == []  # control: folds to "baskup"


def test_every_codepoint_that_folds_into_ascii_is_folded_the_same_way() -> None:
    """The invariant that actually decides verdicts, rather than a witness codepoint.

    The two folds differ on 28 codepoints under the pinned pair, and that set moves when either
    runtime updates its Unicode tables — but a divergence changes an outcome only if it changes
    whether a token matches a denied-list entry, and all 30 of those are pure ASCII. So this is the
    property worth holding, and it survives a table change. It also catches a no-op fold and an
    ASCII-only fold, which the weaker phrasing ("alters nothing outside [A-Z]") does not.
    """
    crossing = [f"U+{cp:04X}" for cp in range(0x80, 0x110000) if not (0xD800 <= cp <= 0xDFFF) and chr(cp).lower() != chr(cp) and chr(cp).lower().isascii()]
    # If this list ever grows, policy.ts must fold the new codepoint identically.
    assert crossing == ["U+212A"]
    assert _fold("\u212a") == "k"


def test_a_denied_token_spelled_with_a_folding_codepoint_still_matches() -> None:
    """The assertion that guards the *matcher*, not the fold helper (#220).

    `policy.ts` fixes this with a regex flag, and a property over fold helpers cannot see a flag
    change at all — this walker's case handling is the engine's. The crossing set is computed rather
    than hardcoded so a future Unicode release that adds a second such codepoint is picked up with
    no edit, which is the lesson from three witness codepoints chosen wrong.
    """
    crossers = {
        chr(cp): chr(cp).lower()
        for cp in range(0x80, 0x110000)
        if not (0xD800 <= cp <= 0xDFFF) and len(chr(cp).lower()) == 1 and re.fullmatch(r"[a-z0-9_]", chr(cp).lower())
    }
    assert sorted(set(crossers.values())) == ["k"]  # U+212A today; grows only if Unicode adds one

    # Three denied tokens carry a `k`, not one: `lock`, `device_tracker` and `backup`.
    tokens = REFERENCEABLE_DENIED_DOMAINS | HOST_SERVICE_DOMAINS
    missed = [
        token.replace(lower, ch) + ".x"
        for ch, lower in crossers.items()
        for token in tokens
        if lower in token and not find_reference_violations({"entity_id": token.replace(lower, ch) + ".x"})
    ]
    assert missed == []


def test_the_service_call_walkers_agree_on_every_shared_case() -> None:
    """
    `devices.call`, in both languages — the third walker, pinned by nothing until #220.

    It regressed inside that PR: this module built its message from the caller's spelling and then
    compared that raw string against `DENIED_ACTIONS`, so adding the fold made the malformed-name
    guard stop rejecting `Homeassistant.Restart` and nothing else caught it. `rpc.py` validates no
    parameter formats, so on the box this function is the last gate.
    """
    cases = json.loads(SCHEMA.read_text())["service_call_cases"]
    assert len(cases) > 10, "run pnpm --filter @hearth/shared export:jsonschema first"
    for case in cases:
        i = case["input"]
        assert find_service_call_violations(i["domain"], i["service"], i["entityIds"]) == case["violations"], case["name"]


def test_our_key_set_matches_home_assistants_service_schema() -> None:
    """
    Every key `cv.SERVICE_SCHEMA` declares is one we have classified (#228).

    This is the fix for the *class*, not the instance. `service_template` was not a key we
    overlooked — the schema states there are exactly two ways to name a service, as `vol.Exclusive`
    members of one group with `has_at_least_one_key` requiring one of them, and we read one. Any
    hand-maintained key list repeats that in a year.

    Asserts a **subset**, and names the offender: a count (`len(...) == 8`) passes when Home
    Assistant swaps one key for another, which is the same coverage-not-logic failure as every other
    gate in #220. If this fails, decide which bucket the new key belongs in — do not widen the
    ignore list to make it pass.
    """
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol

    names = set()
    for marker in cv.SERVICE_SCHEMA.validators[1].schema:
        names.add(str(getattr(marker, "schema", marker)))

    names_a_service = {"action", "service", "service_template"}
    carries_entities = set(TARGET_BEARING_KEYS) | {"entity_id"}
    # Presentation and control flow, from SCRIPT_ACTION_BASE_SCHEMA, plus two that name no entity.
    # `enabled: false` means an action does not run; it is safe to *read* such a node and refuse it,
    # and would only need handling if this walk ever started resolving rather than reading.
    not_executable = {"alias", "note", "continue_on_error", "enabled", "response_variable", "metadata"}

    unclassified = names - names_a_service - carries_entities - not_executable
    assert not unclassified, (
        f"cv.SERVICE_SCHEMA declares {sorted(unclassified)}, which the policy walk does not classify. "
        "Home Assistant may execute it; decide which bucket it belongs in."
    )
    # And the two halves we rely on are really still there.
    assert names_a_service & names == {"action", "service_template"}, sorted(names)
    assert {"target", "data", "data_template", "entity_id"} <= names, sorted(names)


def test_every_action_key_home_assistant_knows_is_classified() -> None:
    """
    Every key in `cv.ACTIONS_MAP` is one the walk reads, or one we have declared inert (#228).

    **Key level, deliberately, and the count is the argument.** `ACTIONS_MAP` has 21 keys mapping to
    16 types; three of those keys mean `call_service` and the walk read two. So a *type*-level
    completeness assertion — "do we handle `call_service`?" — passes while `service_template` sits
    unread inside it. That assertion is the one I wrote first, and it would have been the fifth
    coverage-not-logic failure of the night, inside the fix for the fourth.

    `scene` is the same defect one row down: `{scene: "scene.gate"}` activates a scene and names no
    service, so every check keyed on a service name skipped it.

    Fails naming the offending key. Do not widen `INERT` to make it pass — decide what the key does.

    **And answering this test is not sufficient on its own.** It tells you whether Home Assistant
    added a key the walk does not read. It cannot tell you whether *reading* that key is enough:

        A shape is caught by the existing walk if and only if it names the denied thing.
        A shape that names an allowed **container** whose contents reach the denied thing needs a
        resolver, and no amount of regex or key enumeration substitutes for one.

    That is why `{scene: "scene.gate"}` needed `find_nested_scene_violations` and not merely a place
    in the read set: it names a *scene*, which is a perfectly allowed reference. Every other nested
    shape swept at 2026.9.3 — device triggers in `wait_for_trigger`, entity ids in `event` data —
    names its denied domain directly, so recursion plus the reference and device-shape checks reach
    them.

    Home Assistant gives a config four containers, and each needs its own mechanism:

    | container        | reached by                                        | mechanism                       |
    |------------------|---------------------------------------------------|---------------------------------|
    | scene            | `scene.turn_on`, `scene.apply` keys, `scene:`      | resolve contents here (#220/228)|
    | script           | `script.turn_on`, the implicit `script.<id>`       | resolver, fail closed (#225)    |
    | blueprint        | `use_blueprint`, carrying no actions at all        | walk the validated config (#226)|
    | indirect target  | `area_id` / `label_id` / `floor_id` / `device_id`  | refuse where the action fans out|
    | automation       | `automation.turn_on|trigger|toggle`, and the alias | **blanket denial** (AgDR-0042)  |

    **The automation row's mechanism is different from the rest, and that is a trap.** An automation
    is a bag of actions, so it is structurally a container like a scene or a script — but nothing
    resolves its contents. It is safe only because `automation.trigger|turn_on|turn_off|toggle` are
    in `DENIED_ACTIONS` outright and `automations.set_enabled` is the one sanctioned path. So:
    **narrowing the blanket denial for `automation.*` — to permit some benign automation service, or
    to let a model pause a schedule — requires building a resolver first.** The container rule above
    will not stop that change, because the key is already read; only this note will.

    Five containers, three mechanisms (resolver, validated config, blanket denial) plus the fan-out
    rule for indirect targets. Complete against `ACTIONS_MAP` at 2026.9.3, not for all time — this assertion is what tells us
    when that stops being true. A reader who has the key rule and not the container rule will add a
    key to the read set and believe they are done.
    """
    import homeassistant.helpers.config_validation as cv

    # Keys the walk reads, and where.
    READ = {
        "action", "service", "service_template",  # -> _service_name
        "scene",                                  # -> _service_name, normalised to scene.turn_on
        "device_id",                              # -> the device-shape rule (AgDR-0042)
        "choose", "if", "repeat", "parallel", "sequence",  # -> _LIST_KEYS recursion
        "condition", "and", "or", "not",                   # -> recursion; device conditions caught by shape
        "wait_for_trigger",                                # -> recursion reaches a device trigger inside it
    }
    # Keys that run nothing and name no entity. A reason each, so widening this set is a decision.
    INERT = {
        "delay": "a duration",
        "wait_template": "a template evaluated for truth; laundering through it is the documented-open gap",
        "event": "fires an event on HA's bus; reaches no service and names no entity",
        "variables": "binds names for later templates; template laundering is documented-open",
        "stop": "halts the sequence",
        # `enabled` also accepts a **template** (`Any(boolean, template)`). Neither walker reads it,
        # so a disabled action is still walked — stricter than Home Assistant, and therefore not a
        # bypass; both files checked rather than assumed. It is a loaded gun: the moment anyone adds
        # "skip disabled actions" as an optimisation, `enabled: "{{ ... }}"` hides an action from the
        # walk while HA runs it. Whoever adds that will be reading this table, not the thread.
        "set_conversation_response": "sets a string returned to the caller",
    }

    unclassified = set(cv.ACTIONS_MAP) - READ - set(INERT)
    assert not unclassified, (
        f"cv.ACTIONS_MAP has keys {sorted(unclassified)} the policy walk neither reads nor declares "
        "inert. Home Assistant may execute them; classify each rather than widening INERT."
    )
    # **Teeth.** "Do not widen INERT" is a comment, not a check: moving `scene` from READ to INERT
    # and deleting the normalisation left this test green, and only `test_handlers.py` caught it.
    # So assert each READ key is *actually read* rather than merely listed.
    for key in ("action", "service", "service_template"):
        assert _fold(str(_service_name({key: "Lock.Unlock"}))) == "lock.unlock", key
    assert _fold(str(_service_name({"scene": "scene.gate"}))) == "scene.turn_on"
    assert find_policy_violations({"actions": [{"device_id": "a", "domain": "lock", "type": "unlock"}]}) != []
    for key in ("target", "data", "data_template"):
        assert find_policy_violations({"actions": [{"action": "homeassistant.turn_off", key: {"area_id": "k"}}]}) != [], key

    # And the spellings we depend on have not been renamed underneath us.
    assert {k for k, v in cv.ACTIONS_MAP.items() if v == "call_service"} == {"action", "service", "service_template"}
    assert {k for k, v in cv.ACTIONS_MAP.items() if v == "scene"} == {"scene"}
    assert {k for k, v in cv.ACTIONS_MAP.items() if v == "device"} == {"device_id"}


def test_every_declared_value_shape_is_classified() -> None:
    """
    Companion to the `ACTIONS_MAP` key assertion, one level down (#228 review, by Security).

    The key assertion answers *which keys exist*. This answers *which shapes each key declares* —
    and that is the half that was missing when both of the last two holes shipped. `cv.SERVICE_SCHEMA`
    states `data` as `Any(template, All(dict, template_complex))`: **two shapes behind one name**,
    and the walk read one. `entity_id` is the same — `Any(All(Lower, Any("all","none")), entity_ids)`
    — and the walk read the list and not the sentinel. Enumerating the keys was necessary and not
    sufficient.

    **What this does not do**, stated because a green assertion is otherwise read as "handled":
    it checks that the *claim* covers the schema, not that the *code* honours the claim. A COVERAGE
    entry saying `read` while the code ignores that shape passes here. The corpus is the half that
    checks the code — both, or neither is enough.

    Labels come from `_alt_labels`, not `repr`: a `vol.All(Lower, Any(...))` reprs with the `Lower`
    function's **memory address**, which changes every run, so a table keyed on it could never
    match. An unstable label cannot be checked against.
    """
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol

    def label(v: object) -> str:
        if isinstance(v, (vol.Schema, dict)):
            return "dict"
        if isinstance(v, vol.All):
            if any(s is dict for s in v.validators):
                return "dict"
            for s in v.validators:
                if isinstance(s, vol.Any) and all(isinstance(x, str) for x in s.validators):
                    return "sentinel(" + ",".join(sorted(s.validators)) + ")"
            return "All(" + ",".join(getattr(s, "__name__", type(s).__name__) for s in v.validators) + ")"
        return getattr(v, "__name__", type(v).__name__)

    def alternatives(v: object) -> list[str]:
        if isinstance(v, vol.Any):
            return sorted({a for sub in v.validators for a in alternatives(sub)})
        return [label(v)]

    # `read` = a structural check inspects this shape. `inert:` = deliberately not read, with why.
    coverage = {
        "action": {"service": "read", "dynamic_template": "read"},  # AgDR-0042 refuses the template
        "service_template": {"service": "read", "dynamic_template": "read"},
        "data": {"dict": "read", "template": "read"},  # template -> `unknowable` in the carrier view
        "data_template": {"dict": "read", "template": "read"},
        "target": {"dict": "read", "dynamic_template": "read"},
        # The sentinel is read by the **script resolver at the handler**, not by this walker — the
        # hub cannot resolve scripts at all. Same split as the HANDLER_ONLY corpus group.
        "entity_id": {"entity_ids": "read", "sentinel(all,none)": "read (handler: the script resolver)"},
        "enabled": {
            "boolean": "inert: never read, so a disabled action is still walked — stricter than HA",
            "template": "inert: same. A loaded gun: add skip-disabled and this hides an action from "
                        "the walk while HA runs it. Whoever adds it will read this table, not the thread.",
        },
        "alias": {"string": "inert: a label"},
        "continue_on_error": {"boolean": "inert: control flow"},
        "response_variable": {"str": "inert: captures a response, names nothing"},
        "metadata": {"dict": "inert: the frontend's, never read by core"},
        "note": {"str": "inert: a comment", "NoneType": "inert: a comment"},
    }

    declared = {str(getattr(k, "schema", k)): alternatives(v) for k, v in cv.SERVICE_SCHEMA.validators[1].schema.items()}
    gaps = []
    for key, alts in declared.items():
        known = coverage.get(key)
        if known is None:
            gaps.append(f"key {key!r} is not classified at all (declares {alts})")
            continue
        gaps.extend(f"{key}: alternative {a!r} is in neither the read nor the declared-inert list" for a in alts if a not in known)
    assert not gaps, "cv.SERVICE_SCHEMA declares a value shape nothing classifies:\n  " + "\n  ".join(gaps)
