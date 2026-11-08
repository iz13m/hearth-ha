"""A service call on a group reaches every member, so every member is checked (#657).

`homeassistant.turn_on group.evening` calls `scene.turn_on` on a scene in it, `script.turn_on` on a
script and `automation.turn_on` on an automation. `devices.call` used to check only the ids it was
given, so a group was a way round the scene run check (AgDR-0012) and every other routine rule.

The "measured" rows call Home Assistant's own `homeassistant.turn_on` directly, without Hearth, to
show what the fan-out reaches; the rows after them show `devices.call` refusing it before anything
is sent. Each routine service is mocked, so a refusal is also proved to have called nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers import registry
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher


def _expose(hass: HomeAssistant, *entity_ids: str) -> None:
    for entity_id in entity_ids:
        async_expose_entity(hass, "conversation", entity_id, True)


# What a home that has switched on operating devices, but not running routines, grants; and both.
CONTROL_ONLY = frozenset({"devices.control"})
CONTROL_AND_ROUTINES = frozenset({"devices.control", "routines.run"})


async def _call(
    hass: HomeAssistant, entity_id: str, service: str = "turn_on", domain: str = "homeassistant", caps: frozenset[str] | None = None
) -> Any:
    d = build_dispatcher(hass, caps)
    return await d.dispatch("devices.call", {"domain": domain, "service": service, "entity_id": [entity_id]})


async def _refused(
    hass: HomeAssistant, entity_id: str, service: str = "turn_on", domain: str = "homeassistant", caps: frozenset[str] | None = None
) -> str:
    with pytest.raises(RpcError) as ei:
        await _call(hass, entity_id, service, domain, caps)
    assert ei.value.code == "method_not_allowed", ei.value.message
    return ei.value.message


def _ids(calls: list[Any]) -> list[str]:
    out: list[str] = []
    for c in calls:
        ids = c.data["entity_id"]
        out += [ids] if isinstance(ids, str) else list(ids)
    return out


async def test_the_measured_case_a_group_holding_a_scene_that_sets_a_lock_is_refused(core: HomeAssistant) -> None:
    """The issue's row: `scenes.activate` refuses the scene, and the group no longer runs it either."""
    core.states.async_set("scene.open_gate", "unknown", {"entity_id": ["lock.gate"]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.open_gate"]})
    _expose(core, "scene.open_gate", "group.evening")
    scenes = async_mock_service(core, "scene", "turn_on")

    # Measured: Home Assistant's own fan-out activates the scene.
    await core.services.async_call("homeassistant", "turn_on", {"entity_id": ["group.evening"]}, blocking=True)
    assert _ids(scenes) == ["scene.open_gate"]
    scenes.clear()

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.open_gate"})
    assert ei.value.code == "method_not_allowed"

    message = await _refused(core, "group.evening")
    assert "scene.open_gate" in message and "lock.gate" in message
    # Every service that could carry it there, not only `turn_on`.
    for service in ("turn_off", "toggle"):
        await _refused(core, "group.evening", service)
    assert not scenes


async def test_a_group_of_groups_is_followed_to_the_scene(core: HomeAssistant) -> None:
    core.states.async_set("scene.open_gate", "unknown", {"entity_id": ["lock.gate"]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.open_gate"]})
    core.states.async_set("group.house", "off", {"entity_id": ["input_boolean.test", "group.evening"]})
    _expose(core, "scene.open_gate", "group.evening", "group.house")
    scenes = async_mock_service(core, "scene", "turn_on")

    assert "scene.open_gate" in await _refused(core, "group.house")
    assert not scenes and core.states.get("input_boolean.test").state == "off"


async def test_control_a_group_holding_a_scene_that_sets_only_a_light_still_works(core: HomeAssistant) -> None:
    core.states.async_set("scene.evening_lights", "unknown", {"entity_id": ["light.lamp"]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.evening_lights", "input_boolean.test"]})
    _expose(core, "scene.evening_lights", "group.evening")
    scenes = async_mock_service(core, "scene", "turn_on")

    # With operating devices on and running routines off, the group would run a routine that
    # `scenes.activate` is not allowed to: refused, and nothing is sent, the input_boolean included.
    assert "disabled" in await _refused(core, "group.evening", caps=CONTROL_ONLY)
    assert not scenes and core.states.get("input_boolean.test").state == "off"

    result = await _call(core, "group.evening", caps=CONTROL_AND_ROUTINES)
    assert result["called"] == "homeassistant.turn_on"
    await core.async_block_till_done()
    assert _ids(scenes) == ["scene.evening_lights"]
    assert core.states.get("input_boolean.test").state == "on"


async def test_a_scene_in_a_group_must_be_shared_like_one_activated_directly(core: HomeAssistant) -> None:
    """`scenes.activate` refuses a scene the owner did not share; a group around it must not undo that."""
    core.states.async_set("scene.evening_lights", "unknown", {"entity_id": ["light.lamp"]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.evening_lights"]})
    _expose(core, "group.evening")
    async_expose_entity(core, "conversation", "scene.evening_lights", False)
    scenes = async_mock_service(core, "scene", "turn_on")

    assert "not shared" in await _refused(core, "group.evening")
    assert not scenes


async def test_script_member_measured_runs_and_is_held_to_the_scripts_run_rules(core: HomeAssistant, tmp_path: Path) -> None:
    """Measured: the fan-out calls `script.turn_on`, which runs the script, and so reaches whatever the
    script does: here it activates a scene that sets `lock.gate`. `scripts.run` lets an exposed
    script do that (AgDR-0005) unless Hearth presents it as a scene (AgDR-0044), and requires it to be
    shared either way; a group around it now gets exactly those answers."""
    await hl.async_ensure_labels(core)
    core.states.async_set("scene.open_gate", "unknown", {"entity_id": ["lock.gate"]})
    (tmp_path / "scripts.yaml").write_text("let_me_in:\n  alias: Let me in\n  sequence:\n  - action: scene.turn_on\n    target:\n      entity_id: scene.open_gate\n")
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    core.states.async_set("group.door", "off", {"entity_id": ["script.let_me_in"]})
    _expose(core, "group.door")
    scripts = async_mock_service(core, "script", "turn_on")

    await core.services.async_call("homeassistant", "turn_on", {"entity_id": ["group.door"]}, blocking=True)
    assert _ids(scripts) == ["script.let_me_in"]
    scripts.clear()

    # Not shared with Assist: `scripts.run` would refuse it, so the group is refused.
    assert "not shared" in await _refused(core, "group.door")
    assert not scripts

    # Shared and unlabelled: the owner's own sequence, which `scripts.run` runs (AgDR-0005), and
    # only `scripts.run`, which needs `routines.run`. With operating devices alone, refused.
    _expose(core, "script.let_me_in")
    assert "disabled" in await _refused(core, "group.door", caps=CONTROL_ONLY)
    assert not scripts
    await _call(core, "group.door", caps=CONTROL_AND_ROUTINES)
    assert _ids(scripts) == ["script.let_me_in"]
    scripts.clear()

    # Labelled as a Hearth scene: its contents are checked, and the lock stops it.
    await hl.async_mark_hearth_scene(core, "script.let_me_in", True)
    assert "lock.gate" in await _refused(core, "group.door")
    assert not scripts


async def test_automation_member_measured_switches_on_and_is_refused(core: HomeAssistant) -> None:
    """Measured: the fan-out calls `automation.turn_on`, which switches the automation on, and
    `turn_off` switches it off. Choosing which household rules are in force is
    `automations.set_enabled`'s alone (AgDR-0042), so a group holding one is refused."""
    core.states.async_set("automation.unlock_at_dawn", "off", {"id": "1"})
    core.states.async_set("group.morning", "off", {"entity_id": ["automation.unlock_at_dawn"]})
    _expose(core, "group.morning", "automation.unlock_at_dawn")
    on = async_mock_service(core, "automation", "turn_on")
    off = async_mock_service(core, "automation", "turn_off")

    await core.services.async_call("homeassistant", "turn_on", {"entity_id": ["group.morning"]}, blocking=True)
    assert _ids(on) == ["automation.unlock_at_dawn"]
    on.clear()

    for service in ("turn_on", "turn_off", "toggle"):
        assert "automation.unlock_at_dawn" in await _refused(core, "group.morning", service)
    assert not on and not off


async def test_an_off_limits_member_is_refused_without_naming_it(core: HomeAssistant) -> None:
    """The literal seed: `homeassistant.turn_on` reaches a siren in a group, so a group holding one is
    refused, in words that do not name it (#648)."""
    core.states.async_set("siren.hall", "off")
    core.states.async_set("group.noise", "off", {"entity_id": ["input_boolean.test", "siren.hall"]})
    _expose(core, "group.noise")
    sirens = async_mock_service(core, "siren", "turn_on")

    await core.services.async_call("homeassistant", "turn_on", {"entity_id": ["group.noise"]}, blocking=True)
    assert _ids(sirens) == ["siren.hall"]
    sirens.clear()
    core.states.async_set("input_boolean.test", "off")

    message = await _refused(core, "group.noise")
    assert "siren.hall" not in message
    assert not sirens and core.states.get("input_boolean.test").state == "off"

    core.states.async_set("lock.gate", "locked")
    core.states.async_set("group.noise", "off", {"entity_id": ["lock.gate"]})
    await _refused(core, "group.noise")


async def test_a_valve_member_moves_only_through_a_valve_service(core: HomeAssistant) -> None:
    """`homeassistant.toggle` forwards to `valve.toggle`, which can open it (#527)."""
    core.states.async_set("valve.main", "closed")
    core.states.async_set("group.water", "off", {"entity_id": ["valve.main"]})
    _expose(core, "group.water")
    toggles = async_mock_service(core, "valve", "toggle")
    closes = async_mock_service(core, "valve", "close_valve")

    assert "valve.main" in await _refused(core, "group.water", "toggle")
    assert not toggles
    await _call(core, "group.water", "close_valve", "valve")
    assert _ids(closes) == ["group.water"]


async def test_membership_is_walked_cycle_safe_and_bounded(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    # A group that holds itself is skipped, as Home Assistant skips it.
    core.states.async_set("group.self", "off", {"entity_id": ["group.self", "input_boolean.test"]})
    assert registry.expand_members(core, "group.self") == ["input_boolean.test"]
    _expose(core, "group.self")
    await _call(core, "group.self")

    # Two that hold each other send Home Assistant's own expansion round until Python's recursion
    # limit, so the call is refused in words instead.
    core.states.async_set("group.a", "off", {"entity_id": ["group.b", "input_boolean.test"]})
    core.states.async_set("group.b", "off", {"entity_id": ["group.a"]})
    _expose(core, "group.a")
    assert "hold each other" in await _refused(core, "group.a")
    with pytest.raises(registry.MembershipUnwalkable) as ei:
        registry.expand_members(core, "group.a")
    assert ei.value.cycle

    # Two groups holding one lamp group is not a cycle, and the lamp group is reported once.
    core.states.async_set("group.lamps", "off", {"entity_id": ["input_boolean.test"]})
    core.states.async_set("group.c", "off", {"entity_id": ["group.lamps"]})
    core.states.async_set("group.d", "off", {"entity_id": ["group.lamps", "group.c"]})
    assert registry.expand_members(core, "group.d") == ["group.lamps", "input_boolean.test", "group.c"]

    # A chain of exactly the bound's groups works, its lamp at the bottom included: a leaf is not a level.
    for i in range(registry.EXPAND_MAX_DEPTH):
        last = i == registry.EXPAND_MAX_DEPTH - 1
        core.states.async_set(f"group.ok{i}", "off", {"entity_id": ["input_boolean.test" if last else f"group.ok{i + 1}"]})
    _expose(core, "group.ok0")
    assert registry.expand_members(core, "group.ok0")[-1] == "input_boolean.test"
    await _call(core, "group.ok0")

    # One group more is refused, not walked part way and allowed.
    for i in range(registry.EXPAND_MAX_DEPTH + 1):
        last = i == registry.EXPAND_MAX_DEPTH
        core.states.async_set(f"group.g{i}", "off", {"entity_id": ["input_boolean.test" if last else f"group.g{i + 1}"]})
    _expose(core, "group.g0")
    message = await _refused(core, "group.g0")
    assert "nested too deep" in message and "hold each other" not in message

    # So is a group wider than the member bound.
    monkeypatch.setattr(registry, "EXPAND_MAX_MEMBERS", 3)
    core.states.async_set("group.wide", "off", {"entity_id": [f"input_boolean.x{i}" for i in range(4)]})
    with pytest.raises(registry.MembershipUnwalkable):
        registry.expand_members(core, "group.wide")


async def test_a_scene_member_is_not_walked_into_as_a_group(core: HomeAssistant) -> None:
    """A scene's `entity_id` attribute is what it sets, which its run check judges. A light it sets is
    not a member the call fans out to, so the walk reports the scene and stops there."""
    core.states.async_set("scene.evening_lights", "unknown", {"entity_id": ["light.lamp"]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.evening_lights"]})
    assert registry.expand_members(core, "group.evening", stop=frozenset({"scene"})) == ["scene.evening_lights"]


async def test_a_literal_off_limits_member_is_refused_whatever_gate_it_carries(core: HomeAssistant) -> None:
    """Ruling 9 hands a valve-wrapped group's gate down to its members, so a `lock.*` member of one has
    the effective domain `valve`. The member check reads the literal domain too, as `visible()` does,
    so no gate can make a lock a valve. Called directly with a gate map that says so, because in
    `devices.call` the group's own gate is refused before the member check runs."""
    from custom_components.hearth_ai.handlers.control import member_violations

    class ValveGates:
        def get(self, entity_id: str) -> str | None:
            return "valve" if entity_id == "lock.gate" else None

    core.states.async_set("lock.gate", "locked")
    core.states.async_set("group.door", "off", {"entity_id": ["lock.gate"]})
    assert registry.effective_domain(core, "lock.gate", ValveGates()) == "valve"  # type: ignore[arg-type]
    problems = await member_violations(core, "valve", "group.door", ValveGates())  # type: ignore[arg-type]
    assert problems and "lock.gate" not in " ".join(problems)
