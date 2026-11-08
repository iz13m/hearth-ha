"""A household's stored scene that sets an old-style group runs when every member may be set (#714).

`group/reproduce_state.py` hands a group's stored state to each member, and a member that is itself a
group hands it on again, so `group.doors: unlocked` unlocks every lock in the group. Since #715 such a
scene was refused outright, which stopped long-lived homes' `group.living_room_lights: on` scenes
running from Azka while they still ran in Home Assistant. The run check now walks the group
(`expand_members`) and judges each member as if the scene named it.

Writing a group key stays refused (`find_scene_write_violations`, `SCENE_UNSETTABLE_DOMAINS` inside
`scene.apply`/`scene.create`), and so does an authored link to a scene that sets one: a link runs
later without Hearth, by which time the group may hold something else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.state import async_reproduce_state

from custom_components.hearth_ai.handlers import registry
from custom_components.hearth_ai.handlers.scenes import UNREACHABLE_SCENE, find_scene_run_violations, scene_run_violations
from custom_components.hearth_ai.policy import find_scene_policy_violations, find_scene_write_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

LIGHTS = "- id: 'lr1'\n  name: Living room\n  entities:\n    group.living_room_lights: 'on'\n"


async def _stored(core: HomeAssistant, tmp_path: Path, text: str) -> None:
    (tmp_path / "scenes.yaml").write_text(text)
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()


def _scene(group: str, state: object = "on") -> dict[str, object]:
    return {"id": "1", "name": "n", "entities": {group: state}}


async def test_a_scene_that_sets_a_group_of_lights_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """The issue's case, end to end: listed runnable, activated, and every lamp turned on."""
    core.states.async_set("light.sofa", "off")
    core.states.async_set("light.ceiling", "off")
    core.states.async_set("group.living_room_lights", "off", {"entity_id": ["light.sofa", "light.ceiling"]})
    await _stored(core, tmp_path, LIGHTS)
    assert core.states.get("scene.living_room") is not None
    async_expose_entity(core, "conversation", "scene.living_room", True)
    turned_on = async_mock_service(core, "light", "turn_on")

    assert await scene_run_violations(core, "scene.living_room") == []
    d = build_dispatcher(core)
    (row,) = [s for s in await d.dispatch("scenes.list", {}) if s["entity_id"] == "scene.living_room"]
    assert row["can_run"] is True
    await d.dispatch("scenes.activate", {"entity_id": "scene.living_room"})
    await core.async_block_till_done()
    reached = {e for c in turned_on for e in ([c.data["entity_id"]] if isinstance(c.data["entity_id"], str) else c.data["entity_id"])}
    assert reached == {"light.sofa", "light.ceiling"}


async def test_a_group_with_a_lock_in_it_is_refused_and_the_lock_not_reached(core: HomeAssistant, tmp_path: Path) -> None:
    core.states.async_set("light.sofa", "off")
    core.states.async_set("lock.front", "locked")
    core.states.async_set("group.living_room_lights", "off", {"entity_id": ["light.sofa", "lock.front"]})
    await _stored(core, tmp_path, LIGHTS)
    async_expose_entity(core, "conversation", "scene.living_room", True)
    unlocked = async_mock_service(core, "lock", "unlock")
    locked = async_mock_service(core, "lock", "lock")

    problems = await scene_run_violations(core, "scene.living_room")
    assert problems == [UNREACHABLE_SCENE]
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.living_room"})
    assert ei.value.code == "method_not_allowed" and "lock.front" not in ei.value.message
    assert not unlocked and not locked
    (row,) = [s for s in await d.dispatch("scenes.list", {}) if s["entity_id"] == "scene.living_room"]
    assert row["can_run"] is False


@pytest.mark.parametrize("member", ["lock.front", "siren.hall", "alarm_control_panel.home", "camera.porch", "Lock.Front"])
async def test_an_off_limits_member_is_refused_without_naming_it(core: HomeAssistant, member: str) -> None:
    core.states.async_set(member.lower(), "off")
    core.states.async_set("group.mixed", "off", {"entity_id": ["light.a", member]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.mixed")) == [UNREACHABLE_SCENE]
    # From the membership attribute alone too: a scene outside scenes.yaml.
    core.states.async_set("scene.x", "unknown", {"entity_id": ["group.mixed"]})
    assert find_scene_run_violations(core, "scene.x", None) == [UNREACHABLE_SCENE]


async def test_a_nested_group_with_a_lock_is_refused(core: HomeAssistant) -> None:
    core.states.async_set("group.doors", "off", {"entity_id": ["lock.back"]})
    core.states.async_set("group.inner", "off", {"entity_id": ["light.b", "group.doors"]})
    core.states.async_set("group.house", "off", {"entity_id": ["light.a", "group.inner"]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.house")) == [UNREACHABLE_SCENE]
    core.states.async_set("group.doors", "off", {"entity_id": ["light.c"]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.house")) == []


async def test_a_valve_in_a_group_may_only_be_closed(core: HomeAssistant) -> None:
    core.states.async_set("valve.main", "closed")
    core.states.async_set("group.water", "off", {"entity_id": ["valve.main"]})
    opens = "group.water: this group holds valve.main, and a scene may only close a valve"
    assert find_scene_run_violations(core, "scene.x", _scene("group.water", "open")) == [opens]
    assert find_scene_run_violations(core, "scene.x", _scene("group.water", {"state": "open"})) == [opens]
    assert find_scene_run_violations(core, "scene.x", _scene("group.water", "closed")) == []
    assert find_scene_run_violations(core, "scene.x", _scene("group.water", {"state": "closed", "current_position": 0})) == []
    # Without scenes.yaml the state is unknown, so the direction is too.
    core.states.async_set("scene.x", "unknown", {"entity_id": ["group.water"]})
    assert find_scene_run_violations(core, "scene.x", None) == [opens]


async def _gate(core: HomeAssistant, object_id: str, target: str) -> str:
    """A real `switch_as_x` wrapper presenting `switch.<object_id>` as `target`."""
    source = f"switch.{object_id}"
    er.async_get(core).async_get_or_create("switch", "hearth_test", object_id, suggested_object_id=object_id)
    core.states.async_set(source, "off")
    entry = MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": source, "invert": False, "target_domain": target},
        title=f"{object_id} as {target}",
        version=1,
        minor_version=3,
    )
    entry.add_to_hass(core)
    assert await core.config_entries.async_setup(entry.entry_id)
    await core.async_block_till_done()
    assert registry.wrapped_domain(core, source) == target
    return source


async def test_a_group_over_a_gated_relay_is_refused(core: HomeAssistant) -> None:
    """A switch a `switch_as_x` helper presents as a lock is the lock (#643), however deep in the group."""
    await _gate(core, "door_relay", "lock")

    core.states.async_set("group.inner", "off", {"entity_id": ["switch.door_relay"]})
    core.states.async_set("group.porch", "off", {"entity_id": ["light.a", "group.inner"]})
    problems = find_scene_run_violations(core, "scene.x", _scene("group.porch"))
    assert problems and all("door_relay" not in p for p in problems)
    assert UNREACHABLE_SCENE in problems


async def test_a_group_that_cannot_be_walked_is_refused(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    core.states.async_set("group.a", "off", {"entity_id": ["group.b", "light.a"]})
    core.states.async_set("group.b", "off", {"entity_id": ["group.a"]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.a")) == [
        "group.a: this scene sets a group that reaches groups that hold each other, so it cannot be activated"
    ]
    too_far = "group.g0: this scene sets a group holding too many entities, or groups nested too deep, for Hearth to check, so it cannot be activated"
    for i in range(registry.EXPAND_MAX_DEPTH + 1):
        last = i == registry.EXPAND_MAX_DEPTH
        core.states.async_set(f"group.g{i}", "off", {"entity_id": ["light.a" if last else f"group.g{i + 1}"]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.g0")) == [too_far]
    # One level fewer is walked to the end and runs.
    assert find_scene_run_violations(core, "scene.x", _scene("group.g1")) == []

    monkeypatch.setattr(registry, "EXPAND_MAX_MEMBERS", 3)
    core.states.async_set("group.wide", "off", {"entity_id": [f"light.x{i}" for i in range(4)]})
    assert find_scene_run_violations(core, "scene.x", _scene("group.wide")) == [too_far.replace("group.g0", "group.wide")]


async def test_a_scene_member_of_the_group_is_not_walked(core: HomeAssistant) -> None:
    """A scene's own attribute is what it sets, not a fan-out. Measured: a group handing a state to a
    scene member does nothing (`scene` has no `reproduce_state`), so even a scene that sets a lock is
    not reached. If a release adds one, the measured half fails here first."""
    core.states.async_set("lock.gate", "locked")
    core.states.async_set("scene.inner", "unknown", {"entity_id": ["lock.gate"]})
    core.states.async_set("light.b", "off")
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.inner", "light.b"]})
    unlocked = async_mock_service(core, "lock", "unlock")
    scenes_on = async_mock_service(core, "scene", "turn_on")
    lights_on = async_mock_service(core, "light", "turn_on")

    await async_reproduce_state(core, [State("group.evening", "on")])
    await core.async_block_till_done()
    assert not unlocked and not scenes_on and lights_on
    assert find_scene_run_violations(core, "scene.evening", _scene("group.evening")) == []


async def test_one_group_is_refused_once_from_both_sources(core: HomeAssistant) -> None:
    """Item 3 of #714: the config and the attribute both name the group; one refusal, not two wordings."""
    core.states.async_set("group.doors", "off", {"entity_id": ["light.a", "valve.main"]})
    core.states.async_set("scene.x", "unknown", {"entity_id": ["group.doors"]})
    config = {"id": "1", "name": "n", "entities": {"group.doors": "open"}}
    assert find_scene_run_violations(core, "scene.x", config) == ["group.doors: this group holds valve.main, and a scene may only close a valve"]
    # The stored state is the one used, though the attribute names the group too: closing runs.
    assert find_scene_run_violations(core, "scene.x", {**config, "entities": {"group.doors": "closed"}}) == []
    # A group only the attribute names is still walked, with its state unknown: the live scene sets it.
    core.states.async_set("scene.x", "unknown", {"entity_id": ["group.doors", "group.more"]})
    core.states.async_set("group.more", "off", {"entity_id": ["valve.main"]})
    assert find_scene_run_violations(core, "scene.x", {**config, "entities": {"group.doors": "closed"}}) == [
        "group.more: this group holds valve.main, and a scene may only close a valve"
    ]


async def test_writing_a_group_key_is_still_refused(core: HomeAssistant, tmp_path: Path) -> None:
    config = {"name": "n", "entities": {"group.living_room_lights": "on"}}
    assert find_scene_policy_violations(config) == []
    assert find_scene_write_violations(config) == ["entities.group.living_room_lights: a scene may not set a group; what its members are cannot be checked here"]
    core.states.async_set("group.living_room_lights", "off", {"entity_id": ["light.a"]})
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": config})
    assert ei.value.code == "validation_failed" and "group.living_room_lights" in ei.value.message
    created = await d.dispatch("scenes.create", {"config": {"name": "Plain", "entities": {"input_boolean.test": "on"}}})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.update", {"id": created["id"], "config": config})
    assert ei.value.code == "validation_failed"

    automation = {"alias": "a", "triggers": [{"trigger": "time", "at": "03:00:00"}]}
    for action in (
        {"action": "scene.apply", "data": {"entities": {"group.living_room_lights": "on"}}},
        {"action": "scene.create", "data": {"scene_id": "x", "entities": {"group.living_room_lights": "on"}}},
    ):
        res = await d.dispatch("automations.validate", {"config": {**automation, "actions": [action]}})
        assert res["ok"] is False and res["status"] == "policy" and "group.living_room_lights" in res["error"], action


async def test_an_authored_link_to_a_group_scene_is_still_refused(core: HomeAssistant, tmp_path: Path) -> None:
    """The household's scene runs; a model may not write a link to it, since a link runs past every check."""
    core.states.async_set("group.living_room_lights", "off", {"entity_id": ["light.a"]})
    await _stored(core, tmp_path, LIGHTS)
    assert await scene_run_violations(core, "scene.living_room") == []
    d = build_dispatcher(core)
    link = {"alias": "Evening", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.living_room"}}]}
    res = await d.dispatch("scripts.validate", {"key": "evening", "config": link})
    assert res["ok"] is False and res["status"] == "policy" and "group.living_room_lights" in res["error"]
    # From the membership attribute alone as well.
    core.states.async_set("scene.elsewhere", "unknown", {"entity_id": ["group.living_room_lights"]})
    link = {"alias": "Evening", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.elsewhere"}}]}
    res = await d.dispatch("scripts.validate", {"key": "evening", "config": link})
    assert res["ok"] is False and res["status"] == "policy" and "group.living_room_lights" in res["error"]


async def test_a_group_over_a_valve_relay_is_refused_naming_it(core: HomeAssistant) -> None:
    """A relay a helper presents as a valve: its direction is unknowable through `switch`, so refused (#643 ruling 1)."""
    relay = await _gate(core, "water_relay", "valve")
    core.states.async_set("group.inner", "off", {"entity_id": [relay]})
    core.states.async_set("group.garden", "off", {"entity_id": ["light.a", "group.inner"]})
    problems = find_scene_run_violations(core, "scene.x", _scene("group.garden", "off"))
    assert f"{relay}: this drives a valve, so a scene that sets it cannot be activated" in problems


async def test_a_group_hidden_by_a_lock_gate_is_never_named(core: HomeAssistant) -> None:
    """A group a lock gate rests on through its members is off-limits itself (#643), so its cycle,
    bounds and valve refusals say `UNREACHABLE_SCENE` rather than name it (#722's rule)."""
    relay = await _gate(core, "door_relay", "lock")
    core.states.async_set("valve.main", "closed")
    core.states.async_set("group.porch", "off", {"entity_id": [relay, "valve.main"]})
    assert registry._off_limits_id(core, "group.porch", registry._gates(core))
    assert find_scene_run_violations(core, "scene.x", _scene("group.porch", "open")) == [UNREACHABLE_SCENE]

    core.states.async_set("group.loop", "off", {"entity_id": ["group.porch"]})
    core.states.async_set("group.porch", "off", {"entity_id": [relay, "group.loop"]})
    assert registry._off_limits_id(core, "group.porch", registry._gates(core))
    assert find_scene_run_violations(core, "scene.x", _scene("group.porch")) == [UNREACHABLE_SCENE]

    for i in range(registry.EXPAND_MAX_DEPTH + 1):
        last = i == registry.EXPAND_MAX_DEPTH
        core.states.async_set(f"group.g{i}", "off", {"entity_id": [relay if last else f"group.g{i + 1}"]})
    assert registry._off_limits_id(core, "group.g0", registry._gates(core))
    assert find_scene_run_violations(core, "scene.x", _scene("group.g0")) == [UNREACHABLE_SCENE]
