"""A scene service whose entities are named by a template (invariant 1, AgDR-0042).

Home Assistant's `cv.template_complex` and `render_complex` render a mapping's **keys** as well as its
values, and `scene.apply` then validates each rendered key as an entity id
(`components/homeassistant/scene.py`, `_convert_states`). So `{"{{ 'lo' ~ 'ck.front_door' }}":
"unlocked"}` reaches `lock.unlock`, and neither walker could see it: the box's `_scene_entity_ids`
took the raw key, the reference walker cannot read a concatenated name, and the whole-template rule
only covered a `data` that is itself a template string. Only default-on `automations.write` is needed.

Each case is run twice: once through Home Assistant's own script engine, with `lock.unlock` (or the
service the case reaches) mocked, to prove the vector is real on the pinned release; and once
through Hearth's authoring check, which must refuse it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config

from custom_components.hearth_ai.policy import find_policy_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

LOCK = "lock.front_door"

# The security reviewer's three configs, verbatim in shape, plus the forms the same rendering allows.
UNLOCKS: dict[str, dict[str, Any]] = {
    "a templated key in scene.apply entities": {"action": "scene.apply", "data": {"entities": {"{{ 'lo' ~ 'ck.front_door' }}": "unlocked"}}},
    "scene.apply entities as a template string": {"action": "scene.apply", "data": {"entities": "{{ {'lo' ~ 'ck.front_door': 'unlocked'} }}"}},
    "a templated key under data_template": {"action": "scene.apply", "data_template": {"entities": {"{{ 'lo' ~ 'ck.front_door' }}": "unlocked"}}},
    "a templated entities key": {"action": "scene.apply", "data": {"{{ 'entities' }}": {"{{ 'lo' ~ 'ck.front_door' }}": "unlocked"}}},
    "a Jinja comment splitting the key": {"action": "scene.apply", "data": {"entities": {"lo{# #}ck.front_door": "unlocked"}}},
    "a Jinja comment splitting the action name": {"action": "lo{# #}ck.unlock", "target": {"entity_id": "lo{# #}ck.front_door"}},
}

SNAPSHOTS: dict[str, dict[str, Any]] = {
    "snapshot_entities as a template string": {"action": "scene.create", "data": {"scene_id": "snap", "snapshot_entities": "{{ ['lo' ~ 'ck.front_door'] }}"}},
    "a template element in snapshot_entities": {"action": "scene.create", "data": {"scene_id": "snap", "snapshot_entities": ["input_boolean.test", "{{ 'lo' ~ 'ck.front_door' }}"]}},
}


async def _run(hass: HomeAssistant, step: dict[str, Any]) -> None:
    """Run one step through Home Assistant's own script engine, as an automation's action would."""
    sequence = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA([step]))
    await Script(hass, sequence, "probe", "test").async_run(context=Context())
    await hass.async_block_till_done()


def _automation(step: dict[str, Any]) -> dict[str, Any]:
    return {"alias": "Let me in", "triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": [step]}


@pytest.mark.parametrize("name", list(UNLOCKS))
async def test_home_assistant_unlocks_through_the_template(core: HomeAssistant, name: str) -> None:
    """Pins that the vector is real on the pinned release, so the refusal below is not theory."""
    core.states.async_set(LOCK, "locked")
    unlocked = async_mock_service(core, "lock", "unlock")
    await _run(core, UNLOCKS[name])
    assert [c.data.get("entity_id") for c in unlocked] in ([LOCK], [[LOCK]]), name


@pytest.mark.parametrize("name", list(SNAPSHOTS))
async def test_home_assistant_snapshots_through_the_template(core: HomeAssistant, name: str) -> None:
    core.states.async_set(LOCK, "unlocked")
    await _run(core, SNAPSHOTS[name])
    assert LOCK in core.states.get("scene.snap").attributes["entity_id"], name


@pytest.mark.parametrize("name", [*UNLOCKS, *SNAPSHOTS])
async def test_an_automation_naming_a_scene_entity_by_template_is_refused(core: HomeAssistant, name: str) -> None:
    step = {**UNLOCKS, **SNAPSHOTS}[name]
    d = build_dispatcher(core)
    res = await d.dispatch("automations.validate", {"config": _automation(step)})
    assert res["ok"] is False and res["status"] == "policy", (name, res)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": _automation(step)})
    assert ei.value.code == "validation_failed" and ei.value.data == {"status": "policy"}, name


@pytest.mark.parametrize("name", [*UNLOCKS, *SNAPSHOTS])
async def test_a_script_naming_a_scene_entity_by_template_is_refused(core: HomeAssistant, name: str) -> None:
    step = {**UNLOCKS, **SNAPSHOTS}[name]
    d = build_dispatcher(core)
    res = await d.dispatch("scripts.validate", {"config": {"alias": "Let me in", "sequence": [step]}})
    assert res["ok"] is False and res["status"] == "policy", (name, res)


async def test_a_hearth_scene_script_holding_a_templated_key_is_refused_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """Written to `scripts.yaml` directly, so only the run-time check (`script_run_violations`) stands in the way."""
    from custom_components.hearth_ai import labels as hl

    await hl.async_ensure_labels(core)
    core.states.async_set(LOCK, "locked")
    unlocked = async_mock_service(core, "lock", "unlock")
    (tmp_path / "scripts.yaml").write_text(
        "let_me_in:\n  alias: Let me in\n  sequence:\n  - action: scene.apply\n    data:\n      entities:\n"
        "        \"{{ 'lo' ~ 'ck.front_door' }}\": unlocked\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.let_me_in", True)
    await hl.async_mark_hearth_scene(core, "script.let_me_in", True)
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.let_me_in"})
    assert ei.value.code == "method_not_allowed"
    await core.async_block_till_done()
    assert unlocked == []


async def test_a_templated_data_key_cannot_override_a_closing_valve_position(core: HomeAssistant) -> None:
    """The same rendering on `data` itself: the later key wins, so a literal 0 first is not a close."""
    step = {"action": "valve.set_valve_position", "target": {"entity_id": "valve.main"}, "data": {"position": 0, "{{ 'position' }}": 100}}
    moved = async_mock_service(core, "valve", "set_valve_position")
    core.states.async_set("valve.main", "closed")
    await _run(core, step)
    assert moved[0].data["position"] == 100
    assert find_policy_violations({"actions": [step]}) != []


def test_the_allowed_forms_stay_allowed() -> None:
    """Literal keys, a literal snapshot list and a comma string are what Hearth's own scenes write."""
    for step in (
        {"action": "scene.apply", "data": {"entities": {"light.lamp": "on", "input_boolean.test": {"state": "on"}}}},
        {"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": ["light.lamp"]}},
        {"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": "light.a, light.b"}},
        {"action": "scene.create", "data": {"scene_id": "x", "entities": {"light.lamp": "on"}}},
        {"action": "light.turn_on", "target": {"entity_id": "light.lamp"}, "data": {"brightness": "{{ 40 }}"}},
    ):
        assert find_policy_violations({"actions": [step]}) == [], step


async def test_home_assistant_unlocks_a_lock_through_a_group_a_scene_sets(core: HomeAssistant) -> None:
    """A group's `reproduce_state` sets every member, and `group.set` can build the group from a template."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(core, "group", {})
    core.states.async_set(LOCK, "locked")
    unlocked = async_mock_service(core, "lock", "unlock")
    build = {"action": "group.set", "data": {"object_id": "doors", "entities": "{{ ['lo' ~ 'ck.front_door'] }}"}}
    apply = {"action": "scene.apply", "data": {"entities": {"group.doors": "unlocked"}}}
    await _run(core, build)
    await _run(core, apply)
    assert [c.data.get("entity_id") for c in unlocked] in ([LOCK], [[LOCK]])
    d = build_dispatcher(core)
    for steps in ([build, apply], [apply], [{"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": ["group.doors"]}}]):
        res = await d.dispatch("automations.validate", {"config": {**_automation(steps[0]), "actions": steps}})
        assert res["ok"] is False and res["status"] == "policy" and "group.doors" in res["error"], steps


async def test_home_assistant_turns_an_automation_on_through_a_scene_key(core: HomeAssistant) -> None:
    turned_on = async_mock_service(core, "automation", "turn_on")
    core.states.async_set("automation.night_alarm", "off")
    await _run(core, {"action": "scene.apply", "data": {"entities": {"automation.night_alarm": "on"}}})
    assert [c.data.get("entity_id") for c in turned_on] in (["automation.night_alarm"], [["automation.night_alarm"]])
    d = build_dispatcher(core)
    for key in ("automation.night_alarm", "script.open_gate", "scene.gate"):
        res = await d.dispatch("automations.validate", {"config": _automation({"action": "scene.apply", "data": {"entities": {key: "on"}}})})
        assert res["ok"] is False and res["status"] == "policy" and key in res["error"], key


async def test_a_stored_scene_that_sets_a_group_is_refused_when_written_and_when_run(core: HomeAssistant) -> None:
    """The AgDR-0012 contents check: from scenes.yaml, and from the membership attribute when there is none."""
    from custom_components.hearth_ai.handlers.scenes import find_scene_run_violations

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": {"name": "Doors", "entities": {"group.doors": "unlocked"}}})
    assert ei.value.code == "validation_failed" and "group.doors" in ei.value.message
    stored = {"id": "1", "name": "Doors", "entities": {"group.doors": "unlocked"}}
    assert find_scene_run_violations(core, "scene.doors", stored) == ["entities.group.doors: a scene may not set a group; what its members are cannot be checked here"]
    core.states.async_set("scene.doors", "unknown", {"friendly_name": "Doors", "entity_id": ["group.doors", "light.a"]})
    assert find_scene_run_violations(core, "scene.doors", None) == [
        "group.doors: a scene that sets a group cannot be activated; what its members are cannot be checked here"
    ]
    nested = {"alias": "Doors", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.doors"}}]}
    res = await d.dispatch("scripts.validate", {"config": nested})
    assert res["ok"] is False and res["status"] == "policy" and "group.doors" in res["error"]
