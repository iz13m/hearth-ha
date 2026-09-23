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


def test_every_path_writing_service_is_classified() -> None:
    """
    Every Home Assistant service taking a file path is classified, on **both** axes (#227).

    The hole was `downloader`: a model with `integrations.manage` set its download directory to the
    config directory and then overwrote `automations.yaml` with an automation that unlocked a door
    — which passed no policy walker because it never went through the write path they sit on.

    **Two axes, because a denylist with one output hides half the class:**

        setup   DENIED_DOMAINS   the model installs it       (integrations.manage, opt-in)
        call    DENIED_ACTIONS   the household installed it  (automations.write, DEFAULT ON)

    `downloader` is in both because it is both. `homematicip_cloud.dump_hap_config` is only in the
    second, and would have been invisible to a setup-only list.

    **This instrument covers the CALL axis only, and the setup axis is hand-maintained.**

    The enumeration below globs `*/services.yaml` — services. A config flow that takes a filesystem
    path is invisible to it, which is how `local_file` was missed: its flow takes `CONF_FILE_PATH`
    validated by `os.access` alone, and the resulting `camera.*` entity serves that file's bytes. A
    reviewer found it by reading. So this file argues "two axes, because one output hides half the
    class" while enumerating one of them, and the setup axis is **hand-kept**. Note the two lists
    differ and neither is "the setup denials": `flows.DENIED_DOMAINS` is the enforced set, while
    `REFUSED_AT_SETUP` below is only the part of it this scan can also surface (`downloader`,
    `hassio`) — `local_file` and `upb` are enforced and deliberately absent from it, per assertion 7.

    A scan of the setup axis exists at `RESEARCH/HEARTH_SETUP_AXIS_SCAN/`, deliberately outside the
    suite, because **it is not fit to drive a denial**:

        TIGHT (explicit file-path fields):  13 flows,  2 gated, 11 ungated
        LOOSE (+ any _PATH, key_file):      40 flows,  3 gated, 37 ungated

    Neither is the surface. The loose pattern pulls in HTTP routes and storage keys; the tight one
    missed `folder_watcher`, which *is* gated — so it was wrong in both directions. Denying its
    output would have refused `zwave_js`, `mqtt` and `zha` on the strength of a regex, where
    `zwave_js`'s "path" is `/dev/ttyUSB0`.

    Read-verified during review, so the surface is known rather than merely enumerated. Three
    mechanisms take a flow out, and which one applies matters more than the verdict:
      * **upload, not path** — `google_cloud`, `mqtt`, `sftp_storage` and `velbus` (`CONF_VLP_FILE`)
        use `FileSelector` + `process_uploaded_file`: the caller supplies **bytes** it already holds.
      * **fixed internal write** — `bosch_shc` takes only host + password; it *writes* a cert/key
        pair to `hass.config.path(DOMAIN, unique_id, …)`, content from the pairing result. Neither
        the path nor the content is caller-chosen, and nothing is read back out.
      * **not a host path at all** — `transmission`, `octoprint` and `zwave_js` paths are URL routes;
        `zha` and `bryant_evolution` are serial devices.

    **`upb` was the one other raw host-path read, and it is now denied at setup too.** It was first
    left open on the bound "the content must parse as a UPB export, so a parse oracle rather than
    disclosure" — which nobody had checked, because the read is in external `upb_lib`. Reading it
    showed the bound was wrong in both directions: the format check is `line1.split(",")[0] == "0"`
    so `/etc/hosts` passes, and past it the file's bytes become **entity names** a model reads back
    through `list_entities`. See `flows.DENIED_DOMAINS` for the mechanism and
    `RESEARCH/HEARTH_UPB_SETUP_AXIS/FINDINGS.md` for the run.

    **It is not in the buckets below, and that is assertion 7 working rather than an omission.**
    The scan globs `*/services.yaml`; upb declares services but none takes a path, so classifying
    it here would be a claim the scan cannot support. It is a setup-axis denial the call-axis
    instrument structurally cannot see — the same blindness that hid `local_file`, which is why
    the hand-kept list is the answer on this axis and the scan is not.

    A coarse sieve flagged `bosch_shc`, `upb` and `velbus` together. Two are false positives and the
    third is the finding, which is why the mechanism is recorded per flow above: a list of verdicts
    with the reasons stripped is re-derivable only by doing the reading again.

    **Deny on properties. Enumerate with instruments. Never mistake the output of the second for the
    first.** `downloader` was denied on a property — an ungated caller-chosen path fixed at setup,
    demonstrated end-to-end through the dispatcher. A list whose membership triples when a regex
    widens is a pattern with a product cost attached, not a rule. #256 holds the reading that would
    turn the setup axis into properties, and its question is not "does it take a path" but **does
    the content ever surface, or is the effect a planted configuration?**

    **What this does not do.** It forces a human decision on anything new; it does not prove safety.
    "Is it gated by `is_allowed_path`?" is answered by reading the component, and a component can
    gate a different field than the one that matters — `transmission` calls it on the torrent
    *source* while `download_dir` is the destination. So bucket 3 records **which field** is gated,
    not that the call appears. A classifier keyed on the call alone would have called that safe.

    The enumeration is derived from HA's own source, so a new integration that writes files fails
    here **naming the domain** rather than being found by attack. Do not add a bucket for accepted
    risk: if it writes to a caller-chosen host path it is refused, and if the write lands on a
    remote service's filesystem that is bucket 3 or 4 with a reason.

    **Why the `[%key:...%]` resolve step stays even though it finds no new domain.** At 2026.9.3,
    resolving references changes which *fields* carry path language (nine more, in
    `telegram_bot`, `minio` and `hassio`) and not which *domains* do. That it changes no domain is
    a property of where Home Assistant happens to have put the literal text — a reference in a
    domain with no other path-shaped field would surface a domain the unresolved scan cannot see.
    The step removes a dependence on HA's translation bookkeeping; that the dependence is currently
    harmless is luck rather than design. Delete it and the net is blind to a quarter of the corpus.

    **The boundary, stated with its counts, because a count is a property of a pattern:** it is real
    and, at 2026.9.3, empty — probed by instruments whose own counts disagreed (23 domains by field
    name; 4 and 17 by description at two thresholds; 19 against 27 on the write-primitive sweep, whose
    count was inflated by an overbroad regex and is corrected below) and which agreed on the only thing that matters: **no
    service field drives a host write outside these buckets.** That the counts disagree while the
    conclusion holds is the point — a count is a property of a pattern, the conclusion is not.

    **What is asserted here, and the state of the evidence that is not.** Everything above runs in
    this test. The backstop that depends on neither names nor descriptions — a scan of service
    handlers for file-write primitives — is **not** in this file.

    One adversarial sweep during review found no service field driving a host write outside these
    buckets. **Its count is unreliable, and the cause is known.** The as-run regex matched a bare
    `.write(`, which catches socket, stream and HTTP-response writes rather than file writes,
    inflating the file-writer set from ~17 to 27: the extra ten (`ai_task api ebusd esphome hassio
    icloud immich onkyo reolink unifiprotect`) are stream writes — `await response.write(chunk)` in
    `reolink/views.py`, a proxy stream in `esphome/ffmpeg_proxy.py`, serial protocol in `onkyo` —
    each already read and classified as not a caller-chosen host path.

    The remaining 17-vs-19 spread is a **second** blind spot, not imprecision: the sweep globbed
    each component's top-level `*.py` only. Recursing finds two more real file writes —
    `knx/storage/keyring.py` (`shutil.move`) and
    `zwave_js/scripts/convert_device_diagnostics_to_fixture.py` (`write_text`) — neither
    caller-chosen. So the file-writer set is **19 when you look in subdirectories and 17 when you
    do not**.

    **The conclusion is unaffected because it never rested on the count** — it rested on reading the
    components, and both of the sweep's errors were errors of counting. That is the useful thing
    here: the count moved by ten and then by two, and the verdict did not move at all. Both
    derivations and the as-run script are in `RESEARCH/HEARTH_PATH_WRITER_PATTERN/`.

    Whether "contains a file-write primitive" can be pinned tightly enough for CI at all — given it
    took two people, three regexes and two file-set definitions to agree on a number — is what #252
    exists to decide.

    **Do not narrow the prose half of `path_language`, and here is the argument you will otherwise
    make for narrowing it.** At 2026.9.3 ten of its eleven additions are false positives —
    `recorder.repack` matched "save disk space". That evidence is permanent, it is visible in the
    bucket-4 entries below, and it argues for deletion every time anyone reads this.

    **It is kept anyway, because it is the only check covering a path described in words with no
    filename and no path token in the field name** — "the location to write to". The instrument that
    would replace that coverage is a write-primitive **data-flow** scan: a service field's value
    reaching a write primitive's path argument. That is static analysis, not a grep — an adversarial
    sweep during review found components containing file writes and triaged those outside these
    buckets **by reading each one** (its counts were wrong twice over; corrected below), so a pinned version would
    flag them all and demand per-component judgments that rot exactly like the claims
    quote-the-match exists to remove. It does not exist; it is #252. **Do not narrow this without
    shipping that first.**

    The filename half stays regardless of any of the above. It is what found `device_tracker`, whose
    `dev_id` is described as "find the ID in `known_devices.yaml`" — a real file, named, with no
    prose token in the sentence, missed by every word-based instrument including the ones written to
    attack this one.
    """
    import pathlib
    import re

    import homeassistant.components as ha_components
    import yaml

    from custom_components.hearth_ai.handlers.flows import DENIED_DOMAINS

    # **A domain can be dangerous on both axes**, so these are separate maps rather than one
    # bucket per domain. `downloader` is in both — denying only its setup left every household
    # whose download directory is already the config directory exposed, which is #227's own
    # demonstrated configuration.
    REFUSED_AT_SETUP = {
        "downloader": "writes caller-chosen bytes to a caller-chosen path; directory fixed at setup, so HA never gates it",
        "hassio": "supervisor; already refused entirely. Surfaced by the widened scan via backup field descriptions",
    }
    # domain -> the **exact service**, because a domain prefix passes `automation` on
    # `automation.trigger` while leaving `automation.reload` undenied.
    REFUSED_AT_CALL = {
        "downloader": "downloader.download_file",
        "telegram_bot": "telegram_bot.download_file",
        "local_file": "local_file.update_file_path",
        "homematicip_cloud": "homematicip_cloud.dump_hap_config",
    }
    GATED_BY_HA = {  # bucket 3 — name the field the gate is actually on
        "camera": "filename, via is_allowed_path; and the domain is refused to models entirely",
        "image": "filename, via is_allowed_path; domain refused entirely",
        "file": "file_name, via is_allowed_path (allowlist_external_dirs is empty by default)",
        "androidtv": "local_path, via is_allowed_path",
        "blink": "filename, via is_allowed_path",
        "color_extractor": "color_extract_path, via is_allowed_path",
        "google_photos": "filename, via is_allowed_path",
        "onedrive": "filename, via is_allowed_path",
        "openai_conversation": "filenames, via is_allowed_path",
        # NOT telegram_bot — its `is_allowed_path` guards `load_data`, the outbound send, while
        # `directory_path` is the ungated destination. It is refused at the call axis instead.
        "minio": "file_path, via is_allowed_path",
        # Surfaced only once the scan recursed into nested `fields` — `attachments.filename` sits
        # inside a collapsible section, so the first-level scan never saw it. The attachment path
        # itself is gated at `smtp/helpers.py:118` (`is_allowed_path`); the nested `filename` that
        # matched is the attachment's *display* name, not a path.
        "smtp": "the attachment path, via is_allowed_path at helpers.py:118",
    }
    # Bucket 4 — **not a host write**, either because it lands on a remote service or because the
    # pattern matched something that is not a path at all. Every reason **quotes what matched**
    # rather than asserting where the write goes: a claim about the world rots when the component
    # changes, a claim about the match cannot. `local_file` was in here with the reason "on a remote
    # filesystem" — emphatically false, and sayable only because the reason was a claim about the
    # world. That is the rule's origin and the reason it is not optional.
    ON_A_REMOTE_FILESYSTEM = {
        "transmission": "download_path — matched on the field name; goes to the Transmission daemon, and note its is_allowed_path gates the torrent SOURCE not this",
        # Was "an upload to the household's own Immich server" — true, and the wrong property.
        # What makes it safe is the **selector**: `{media: {accept: [image/*, video/*]}}`, so the
        # value is a media-source reference and not a host path at all. Swap it for a
        # `TextSelector` upstream and the old reason still reads true while the danger appears.
        "immich": "file — a media selector ({media: accept image/*,video/*}), so the value is a media-source reference, not a host path",
        # These two are the weak kind and are labelled as such deliberately. Both are **plain
        # `{text: null}` selectors** — checked, not assumed — so nothing about the field's shape
        # makes them safe; the reason is entirely *where the value goes*, which is a claim about
        # the world and rots when the component changes. Stated so the next auditor knows these
        # are the rows to re-read, not the rows to trust.
        "system_bridge": "path — a plain text selector; safe only because open_path sends it to the remote System Bridge host. World-claim: re-read if that changes",
        "guardian": "filename — a plain text selector; safe only because upgrade_firmware sends it to the remote device. World-claim: re-read if that changes",
        "html5": "dir — matched on the field name; the value is a text direction, ltr or rtl",
        "ntfy": "attach_file — matched on the field name; the value is a URL",
        # NOT local_file — "read-only" was true and irrelevant: it redirects the camera at any
        # readable host path. Refused at the call axis.
        "cast": "dashboard_path/view_path — matched on the field name; the values are Lovelace view ids",
        "google_travel_time": "destination — matched on the field name; the value is a street address",
        "waze_travel_time": "destination — matched on the field name; the value is a street address",
        # Surfaced by the widened description scan rather than by field name. These quote **what
        # matched**, not a claim that the component is safe: a claim about the world rots when the
        # component changes, a quote about the match cannot. If you are auditing one of these, read
        # the quoted text — the pattern fired on prose, not on a path.
        "alexa_devices": "sound — matched 'file' in 'The sound file to play.'",
        "deconz": "field — matched 'path' in 'a full path to deCONZ endpoint' (a REST resource, not a filesystem)",
        "imap": "target_folder — matched 'folder' in 'The target folder the email should be moved to.'",
        "recorder": "repack — matched 'save' in 'Attempt to save disk space…'",
        "remote": "alternative — matched 'stored' in 'If code must be stored as an alternative.'",
        "shelly": "key — matched 'stored' in 'the key under which the KVS value will be stored'",
        "vallox": "duration — matched 'stored' in 'device uses stored duration.'",
        "velbus": "address — matched 'directory' in the module-address description",
        "xiaomi_miio": "slot — matched 'save' in 'the slot used to save the IR command.'",
        # The previous reason here said "matched 'file' inside a word" and that is wrong — the
        # description reads "your device's configuration **file**", a standalone word. A reason
        # that misdescribes its own match is the defect quoting-the-match exists to prevent, so it
        # is worth more than the correction: it means nobody re-read the string. The real property
        # is the field's **type** — `value_size` takes 1, 2 or 4.
        "zwave_js": "value_size — a number selector (1|2|4); matched 'file' in 'your device's configuration file', which is a file on the device, not a path this call supplies",
        # The one the filename half earned. It *does* write this file — at a fixed internal path,
        # never one a caller chooses, which is the property that decides the bucket.
        "device_tracker": "dev_id — matched 'known_devices.yaml'; see writes that file at a fixed internal path",
    }

    import json

    pathy = re.compile(r"(^|_)(file|filename|path|dir|directory|destination)$|^(file|filename|path)")
    # Deliberately loose: it is cheaper to bucket a false positive with a reason than to be blind to
    # a path field named `output` or `store_to`. Every addition it makes gets classified below.
    path_language = re.compile(
        # Prose tokens, **and a literal filename with an extension**. The second half is not
        # decoration: `device_tracker.see.dev_id` is described as "find the ID in
        # `known_devices.yaml`" — a real file, named, with not one prose token in the sentence. A
        # pattern loose on prose and blind to filenames is loose in the wrong dimension.
        r"\b(path|file|folder|directory|filename|saved?|written?|stored?)\b"
        r"|[\w./-]+\.(ya?ml|json|txt|csv|db|log|conf|ini|png|jpe?g|mp3|wav)\b",
        re.I,
    )
    reference = re.compile(r"^\[%key:(.+)%\]$")
    root = pathlib.Path(ha_components.__file__).parent

    _strings: dict[str, dict] = {}

    def strings_for(domain: str) -> dict:
        if domain not in _strings:
            path = root / domain / "strings.json"
            try:
                _strings[domain] = json.loads(path.read_text()) if path.exists() else {}
            except Exception:  # noqa: BLE001
                _strings[domain] = {}
        return _strings[domain]

    def resolve(text: object, depth: int = 0) -> str:
        """A description, following `[%key:...%]` to the entry it points at.

        602 of the 2400 descriptions are references rather than words. A reader that does not
        follow them sees a placeholder, finds no path language, and reports a clean result — the
        same blindness as reading `services.yaml`, one layer further in.
        """
        match = reference.match(str(text or ""))
        if not match or depth > 5:
            return str(text or "")
        parts = match.group(1).split("::")
        if len(parts) < 3 or parts[0] != "component":
            return ""
        node: object = strings_for(parts[1])
        for key in parts[2:]:
            node = node.get(key) if isinstance(node, dict) else None
        return resolve(node, depth + 1) if node else ""

    found: dict[str, list[str]] = {}
    described = unresolved = 0
    for services_yaml in sorted(root.glob("*/services.yaml")):
        domain = services_yaml.parent.name
        try:
            data = yaml.safe_load(services_yaml.read_text()) or {}
        except Exception:  # noqa: BLE001 - a component we cannot parse is not our concern
            continue
        service_strings = strings_for(domain).get("services") or {}
        fields: list[str] = []

        def walk(node: object, service: str | None = None) -> None:
            nonlocal described, unresolved
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key == "fields" and isinstance(value, dict):
                    for field, spec in value.items():
                        if pathy.search(str(field)):
                            fields.append(field)
                        else:
                            raw = (((service_strings.get(service) or {}).get("fields") or {}).get(field) or {}).get("description", "")
                            text = resolve(raw)
                            # Branch on the **input** and check the **output**, so a resolver that
                            # returns its argument unchanged is detected. Branching on `text` first
                            # made the `unresolved` counter unreachable the moment `resolve` stopped
                            # resolving — a guard that could not fail for the deletion it existed to
                            # catch, which is the class this whole file is about (#227 review).
                            if reference.match(str(raw)):
                                if text and not reference.match(str(text)):
                                    described += 1
                                else:
                                    unresolved += 1
                            elif str(raw).strip():
                                described += 1
                            if text and path_language.search(text):
                                fields.append(field)
                        # **Recurse.** A field may itself hold `fields` — HA's collapsible sections.
                        # Twenty domains nest fields that way, `smtp`'s `attachments.filename`
                        # among them, and stopping at the first level made every one invisible.
                        if isinstance(spec, dict):
                            walk(spec, service)
                elif isinstance(value, dict):
                    walk(value, key if service is None else service)

        walk(data)
        if fields:
            found[domain] = sorted(set(fields))

    # **The guard.** Reading `services.yaml` for descriptions returns 9 non-empty out of 2275 —
    # a scan over it is blind, not strict, and loosening its regex cannot help. Assert the corpus
    # this reads is actually populated and fully resolved, so neither blindness recurs silently.
    assert described > 1000, f"only {described} resolved field descriptions — this scan is reading the wrong place"
    assert unresolved == 0, f"{unresolved} `[%key:...%]` references did not resolve; a quarter of the corpus is placeholders"

    classified = set(REFUSED_AT_SETUP) | set(REFUSED_AT_CALL) | set(GATED_BY_HA) | set(ON_A_REMOTE_FILESYSTEM)
    unclassified = sorted(set(found) - classified)
    assert not unclassified, (
        "Home Assistant has service(s) taking a file path that nothing here classifies: "
        f"{ {d: found[d] for d in unclassified} }. Put each in exactly one bucket with a reason — "
        "refused at setup, refused at call, gated by HA on a named field, or writing to a remote "
        "filesystem. There is deliberately no accepted-risk bucket."
    )

    # **Teeth.** A bucket is a claim. Without these the whole fix reverts green: a reviewer deleted
    # `downloader` from `DENIED_DOMAINS`, emptied `REFUSED_AT_SETUP` and refiled it under
    # `GATED_BY_HA` with an invented reason, and 278 tests passed — because `set(X) <= Y` is
    # vacuously true for an empty X and buckets 3 and 4 are unverified free text.

    # 1. **Hardcoded, not read from the tables above.** This is the assertion that actually stops a
    #    revert, and it took two attempts: a first version asserted the tables were non-empty and
    #    checked their claims against the code, which still let the reviewer delete `downloader`
    #    from `DENIED_DOMAINS`, drop it from both refused maps and refile it under `GATED_BY_HA`
    #    with an invented reason — every table-driven assertion passed, because removing a claim
    #    removes its check. A check that reads the thing it is checking cannot detect its deletion.
    assert "downloader" in DENIED_DOMAINS, "#227's setup axis has been reverted"
    assert "local_file" in DENIED_DOMAINS, "#227's setup axis has been reverted for local_file"
    # `upb` is the third, and the only one the scan below could never have surfaced: its
    # services.yaml declares no path field, so the danger is entirely in the config flow.
    assert "upb" in DENIED_DOMAINS, "#227's setup axis has been reverted for upb"
    # **Deletion, not only renaming.** The existence assertions below punish a rename and permit a
    # deletion — and ten of sixteen domain entries deleted with the suite green, including
    # `python_script` and `hassio`, two of the four host-level domains invariant 1 rests on. A
    # failure message saying "find the new name rather than removing the entry" is guidance; this
    # is the check. Named literally, because a check that reads its own table cannot see the table
    # shrink.
    for domain in ("shell_command", "python_script", "hassio", "backup"):
        assert domain in HOST_SERVICE_DOMAINS, f"{domain} dropped from HOST_SERVICE_DOMAINS"
        assert domain in DENIED_ACTION_DOMAINS, f"{domain} dropped from DENIED_ACTION_DOMAINS"

    # **Every** member, not the four this PR happened to name. Pinning only the four it touched
    # left ten of sixteen deletable with the suite green — `device_tracker` the sharpest, because
    # it is pinned in `DENIED_ENTITY_DOMAINS` and was not here, so deleting it made
    # `device_tracker.see` callable: the very service this file classifies in bucket 4 for writing
    # `known_devices.yaml`. "Pinned somewhere" is not pinned; each list needs its own teeth.
    ALL_DENIED_ACTION_DOMAINS = (
        "alarm_control_panel", "auth", "backup", "camera", "cloud", "device_tracker", "ffmpeg",
        "hassio", "lock", "onboarding", "person", "python_script", "recorder", "shell_command",
        "stream", "update",
    )
    for domain in ALL_DENIED_ACTION_DOMAINS:
        assert domain in DENIED_ACTION_DOMAINS, (
            f"{domain} is no longer a denied action domain. If Home Assistant renamed it, find the "
            "new name and update the entry — deleting it is what makes this failure go away and "
            "the hole come back."
        )
    # Ratchet: a denial added later must be pinned above too, or it inherits the same blind spot.
    assert set(ALL_DENIED_ACTION_DOMAINS) == set(DENIED_ACTION_DOMAINS), (
        "DENIED_ACTION_DOMAINS changed; add the new domain to ALL_DENIED_ACTION_DOMAINS so it is "
        f"pinned literally. Unpinned: {sorted(set(DENIED_ACTION_DOMAINS) - set(ALL_DENIED_ACTION_DOMAINS))}"
    )
    # The remaining two lists, with the same ratchet the two above carry (#257). Pinning without a
    # ratchet protects today's entries and not tomorrow's — and "unpinned because it was added
    # later" is exactly the state that left ten domains and eight services deletable-green here.
    ALL_DENIED_ENTITY_DOMAINS = ("alarm_control_panel", "camera", "device_tracker", "lock", "person")
    for domain in ALL_DENIED_ENTITY_DOMAINS:
        assert domain in DENIED_ENTITY_DOMAINS, f"{domain} dropped from DENIED_ENTITY_DOMAINS"
    assert set(ALL_DENIED_ENTITY_DOMAINS) == set(DENIED_ENTITY_DOMAINS), (
        "DENIED_ENTITY_DOMAINS changed; add the new domain to ALL_DENIED_ENTITY_DOMAINS so it is "
        f"pinned literally. Unpinned: {sorted(set(DENIED_ENTITY_DOMAINS) - set(ALL_DENIED_ENTITY_DOMAINS))}"
    )
    ALL_SETUP_DENIED_DOMAINS = (
        "backup", "command_line", "downloader", "ffmpeg", "hassio", "hearth_ai", "homeassistant",
        "local_file", "python_script", "shell_command", "upb",
    )
    for domain in ALL_SETUP_DENIED_DOMAINS:
        assert domain in DENIED_DOMAINS, f"{domain} dropped from flows.DENIED_DOMAINS"
    assert set(ALL_SETUP_DENIED_DOMAINS) == set(DENIED_DOMAINS), (
        "flows.DENIED_DOMAINS changed; add the new domain to ALL_SETUP_DENIED_DOMAINS so it is "
        "pinned literally — and add an over-refusal case to "
        "`test_the_setup_denylist_does_not_swallow_what_it_spared` if the new denial is a "
        f"judgement call. Unpinned: {sorted(set(DENIED_DOMAINS) - set(ALL_SETUP_DENIED_DOMAINS))}"
    )
    # Same correction on the service list: the four below are #227's, and eight others were
    # deletable green — including the whole `homeassistant.*` family that AGENTS.md calls "refused
    # everywhere, to everyone".
    ALL_DENIED_ACTIONS = (
        "automation.toggle", "automation.trigger", "automation.turn_off", "automation.turn_on",
        "downloader.download_file", "homeassistant.reload_all", "homeassistant.reload_config_entry",
        "homeassistant.reload_core_config", "homeassistant.restart", "homeassistant.set_location",
        "homeassistant.stop", "homematicip_cloud.dump_hap_config", "local_file.update_file_path",
        "logger.set_level", "persistent_notification.dismiss_all", "system_log.write",
        "telegram_bot.download_file",
    )
    for service in ALL_DENIED_ACTIONS:
        assert service in DENIED_ACTIONS, (
            f"{service} is no longer denied. If it was renamed upstream, find the new name — "
            "deleting the entry is what makes this failure go away and the hole come back."
        )
    assert set(ALL_DENIED_ACTIONS) == set(DENIED_ACTIONS), (
        "DENIED_ACTIONS changed; add the new service to ALL_DENIED_ACTIONS so it is pinned "
        f"literally. Unpinned: {sorted(set(DENIED_ACTIONS) - set(ALL_DENIED_ACTIONS))}"
    )

    # 2. Non-empty, so the subset assertions below cannot be satisfied by emptying a table.
    assert REFUSED_AT_SETUP and REFUSED_AT_CALL

    # 3. Every claim checked against the code that would have to enforce it.
    assert set(REFUSED_AT_SETUP) <= DENIED_DOMAINS, sorted(set(REFUSED_AT_SETUP) - DENIED_DOMAINS)
    for domain, service in REFUSED_AT_CALL.items():
        assert service.split(".", 1)[0] == domain, (domain, service)
        # The **exact service**, not the domain prefix: a prefix test passes `automation` on
        # `automation.trigger` while `automation.reload` is correctly not denied.
        assert service in DENIED_ACTIONS, f"{service} is claimed refused-at-call but is not in DENIED_ACTIONS"

    # 4. The shapes themselves, so the classification cannot drift from the behaviour. These are
    #    the #227 exploit and its siblings; if any starts passing, the fix has been reverted.
    for shape in (
        {"action": "downloader.download_file", "data": {"url": "http://x", "filename": "automations.yaml", "overwrite": True}},
        {"action": "telegram_bot.download_file", "data": {"url": "http://x", "directory_path": "/config"}},
        {"action": "local_file.update_file_path", "target": {"device_id": "abc"}, "data": {"file_path": "/etc/passwd"}},
        {"action": "homematicip_cloud.dump_hap_config", "data": {"config_output_path": "/config/secrets.yaml"}},
    ):
        assert find_policy_violations({"actions": [shape]}) != [], shape

    # 5. ...and the ordinary services of those same integrations still work, so this stays
    #    service-level rather than quietly becoming a domain ban.
    for allowed in ("homematicip_cloud.set_active_climate_profile", "telegram_bot.send_message"):
        assert find_policy_violations({"actions": [{"action": allowed}]}) == [], allowed

    # 6. Buckets 3 and 4 are mutually exclusive and neither may re-classify something refused.
    assert not (set(GATED_BY_HA) & set(ON_A_REMOTE_FILESYSTEM))
    refused = set(REFUSED_AT_SETUP) | set(REFUSED_AT_CALL)
    assert not (refused & (set(GATED_BY_HA) | set(ON_A_REMOTE_FILESYSTEM))), "a refused domain is also filed as safe"

    # 7. No dead entries: a classification for a domain the scan no longer finds is a stale claim,
    #    which is how a renamed HA field leaves a reason nobody rechecks.
    stale = sorted(classified - set(found))
    assert not stale, f"classified but no longer surfaced by the scan: {stale}"


def test_every_denied_action_still_exists_in_home_assistant() -> None:
    """
    A denial naming a service or domain Home Assistant no longer has is green and meaningless.

    `DENIED_ACTIONS` is a list of literal strings. `assert "downloader.download_file" in
    DENIED_ACTIONS` passes whether or not Home Assistant still calls it that — so if upstream
    renames a service, the denial keeps passing while the real service runs undenied. That is the
    same shape as the anti-revert teeth needing a hardcoded assertion: a check that only consults
    our own side of the contract cannot notice the other side moving.

    Checked against `services.yaml`, which is where a component declares what it offers. If this
    fails, the service was renamed or removed upstream: find what it is called now and update the
    denial — do **not** delete the entry, which is the reading that makes the failure go away and
    the hole come back.
    """
    import pathlib

    import homeassistant.components as ha_components
    import yaml

    root = pathlib.Path(ha_components.__file__).parent

    # **Domains too.** A domain-level denial goes inert exactly the same way, and these carry the
    # host-level four that invariant 1 rests on — `shell_command`, `python_script`, `hassio`,
    # `backup` — plus the locks, cameras and location entities. A rename upstream would leave the
    # list pointing at nothing while the real domain went undenied.
    absent = sorted(
        domain
        for group in (DENIED_ACTION_DOMAINS, HOST_SERVICE_DOMAINS, DENIED_ENTITY_DOMAINS)
        for domain in group
        if not (root / domain).is_dir()
    )
    assert not absent, (
        f"denied domains that no longer exist in Home Assistant: {absent}. Same rule as below — "
        "find the new name, do not drop the entry."
    )

    missing = []
    for action in sorted(DENIED_ACTIONS):
        domain, service = action.split(".", 1)
        services_yaml = root / domain / "services.yaml"
        declared = False
        if services_yaml.exists():
            try:
                declared = service in (yaml.safe_load(services_yaml.read_text()) or {})
            except Exception:  # noqa: BLE001
                declared = False
        if not declared:
            missing.append(action)
    assert not missing, (
        f"denied but no longer declared by Home Assistant: {missing}. The denial is now inert — "
        "find the new name rather than removing the entry."
    )
