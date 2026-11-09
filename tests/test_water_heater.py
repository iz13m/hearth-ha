"""A water heater's exact ceiling on the box (#560, AgDR-0075).

The walkers refuse a temperature that is not a literal number or is above 140, over the ceiling in
either unit (pinned against the hub by the shared cases in `test_policy.py` and
`test_stored_scene_routines.py`). These pin the half only the box can do: 60 °C, or 140 °F in a
Fahrenheit home, or the entity's own `max_temp` if lower, on every surface: devices.call, an authored
automation (both of `_validate`'s passes), a scene Hearth writes, every scene run (a household's own
scene included), a `Hearth: scene` script when it runs, and a config-flow form value. Never clamped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.flows import check_flow_input
from custom_components.hearth_ai.handlers.scenes import find_config_violations, find_scene_run_violations, scene_run_violations
from custom_components.hearth_ai.handlers.water_heater import ceiling, find_water_heater_violations
from custom_components.hearth_ai.policy import (
    WATER_HEATER_CEILING,
    WATER_HEATER_CEILING_RULE,
    WATER_HEATER_MODE_RULE,
    WATER_HEATER_MODES,
    find_service_call_violations,
)
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

TANK = "water_heater.tank"
SMALL = "water_heater.small"  # its own max_temp is below the ceiling


def _home(core: HomeAssistant, *, fahrenheit: bool = False) -> None:
    core.config.units = US_CUSTOMARY_SYSTEM if fahrenheit else METRIC_SYSTEM
    big, small = (160, 130) if fahrenheit else (75, 55)
    core.states.async_set(TANK, "eco", {"temperature": 50, "min_temp": 40, "max_temp": big, "operation_list": ["eco", "performance"]})
    core.states.async_set(SMALL, "electric", {"temperature": 45, "min_temp": 35, "max_temp": small})
    for eid in (TANK, SMALL):
        async_expose_entity(core, "conversation", eid, True)


def _set(target: Any, temperature: Any) -> dict[str, Any]:
    return {"action": "water_heater.set_temperature", "target": target, "data": {"temperature": temperature}}


def _automation(action: dict[str, Any]) -> dict[str, Any]:
    return {"alias": "Tank", "triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": [action]}


def test_the_ceiling_is_one_temperature_in_two_units_and_the_rules_say_it_in_both_languages() -> None:
    assert dict(WATER_HEATER_CEILING) == {"°C": 60, "°F": 140}
    assert (WATER_HEATER_CEILING["°F"] - 32) * 5 / 9 == WATER_HEATER_CEILING["°C"]
    assert WATER_HEATER_MODES == ("off", "eco", "electric", "gas", "heat_pump")
    assert "capped at 60 °C (140 °F) for safety" in WATER_HEATER_CEILING_RULE and "60 درجة مئوية" in WATER_HEATER_CEILING_RULE
    assert "سخان الماء" in WATER_HEATER_MODE_RULE


async def test_the_ceiling_is_the_homes_unit_or_the_entitys_own_max(core: HomeAssistant) -> None:
    _home(core)
    assert ceiling(core, TANK) == (60, "°C")
    assert ceiling(core, SMALL) == (55, "°C")
    assert ceiling(core, None) == (60, "°C")
    assert ceiling(core, "water_heater.missing") == (60, "°C")
    _home(core, fahrenheit=True)
    assert ceiling(core, TANK) == (140, "°F")
    assert ceiling(core, SMALL) == (130, "°F")


async def test_devices_call_refuses_over_the_ceiling_in_a_celsius_home_and_never_lowers_it(core: HomeAssistant) -> None:
    _home(core)
    sent = async_mock_service(core, "water_heater", "set_temperature")
    modes = async_mock_service(core, "water_heater", "set_operation_mode")
    async_mock_service(core, "water_heater", "set_away_mode")
    d = build_dispatcher(core)

    async def call(ids: list[str], data: dict[str, Any], service: str = "set_temperature") -> None:
        await d.dispatch("devices.call", {"domain": "water_heater", "service": service, "entity_id": ids, "data": data})

    await call([TANK], {"temperature": 60})
    await call([SMALL], {"temperature": 55})
    for ids, value, why in (
        ([TANK], 61, f"{TANK}: 61 °C is above 60 °C"),
        # Below 140, so only the box can refuse it: the walkers cannot know the home is metric.
        ([TANK], 100, f"{TANK}: 100 °C is above 60 °C"),
        ([TANK], 60.5, f"{TANK}: 60.5 °C is above 60 °C"),
        ([SMALL], 56, f"{SMALL}: 56 °C is above 55 °C"),
        ([TANK, SMALL], 58, f"{SMALL}: 58 °C is above 55 °C"),
    ):
        assert find_service_call_violations("water_heater", "set_temperature", ids, {"temperature": value}) == []
        with pytest.raises(RpcError) as ei:
            await call(ids, {"temperature": value})
        assert ei.value.code == "method_not_allowed", value
        assert why in ei.value.message and "capped at 60 °C (140 °F) for safety" in ei.value.message, ei.value.message
    # Over 140, a string and a template are the walkers' to refuse, before the box reads anything.
    for value in (150, "55", "{{ 50 }}"):
        with pytest.raises(RpcError) as ei:
            await call([TANK], {"temperature": value})
        assert ei.value.code == "method_not_allowed" and WATER_HEATER_CEILING_RULE in ei.value.message
    await core.async_block_till_done()
    # Nothing was lowered and sent in place of a refused value.
    assert [c.data["temperature"] for c in sent] == [60, 55]

    # The modes: five, compared exactly. Away mode is a setback and stays allowed.
    await call([TANK], {"operation_mode": "eco"}, "set_operation_mode")
    for mode in ("performance", "high_demand", "boost", "Eco"):
        with pytest.raises(RpcError) as ei:
            await call([TANK], {"operation_mode": mode}, "set_operation_mode")
        assert WATER_HEATER_MODE_RULE in ei.value.message, mode
    await call([TANK], {"away_mode": True}, "set_away_mode")
    await core.async_block_till_done()
    assert [c.data["operation_mode"] for c in modes] == ["eco"]


async def test_devices_call_holds_a_fahrenheit_home_to_140_or_the_entitys_max(core: HomeAssistant) -> None:
    _home(core, fahrenheit=True)
    sent = async_mock_service(core, "water_heater", "set_temperature")
    d = build_dispatcher(core)
    await d.dispatch("devices.call", {"domain": "water_heater", "service": "set_temperature", "entity_id": [TANK], "data": {"temperature": 140}})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "water_heater", "service": "set_temperature", "entity_id": [SMALL], "data": {"temperature": 135}})
    assert f"{SMALL}: 135 °F is above 130 °F" in ei.value.message
    with pytest.raises(RpcError):
        await d.dispatch("devices.call", {"domain": "water_heater", "service": "set_temperature", "entity_id": [TANK], "data": {"temperature": 141}})
    await core.async_block_till_done()
    assert [c.data["temperature"] for c in sent] == [140]


async def test_an_authored_config_is_held_to_the_exact_ceiling(core: HomeAssistant) -> None:
    _home(core)
    assert await find_config_violations(core, _automation(_set({"entity_id": TANK}, 60))) == []
    for action, why in (
        (_set({"entity_id": TANK}, 61), "61 °C is above 60 °C"),
        (_set({"entity_id": SMALL}, 58), "58 °C is above 55 °C"),
        (_set({"entity_id": f"{TANK}, {SMALL}"}, 58), "58 °C is above 55 °C"),
        # A target the box cannot list is held to the home's ceiling.
        (_set({"area_id": "utility"}, 70), "this water heater: 70 °C is above 60 °C"),
        ({"action": "water_heater.set_temperature", "entity_id": TANK, "data_template": {"temperature": 90}}, "90 °C is above 60 °C"),
        ({"action": "scene.apply", "data": {"entities": {TANK: {"state": "on", "temperature": 65}}}}, f"entities.{TANK}: 65 °C is above 60 °C"),
        ({"choose": [{"conditions": [], "sequence": [{"service": "Water_Heater.Set_Temperature", "target": {"entity_id": TANK}, "data": {"temperature": 70}}]}]}, "70 °C"),
    ):
        problems = find_water_heater_violations(core, _automation(action))
        assert any(why in p for p in problems), (action, problems)

    d = build_dispatcher(core)
    res = await d.dispatch("automations.validate", {"config": _automation(_set({"entity_id": TANK}, 61))})
    assert res["ok"] is False and res["status"] == "policy" and "61 °C is above 60 °C" in res["error"]
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.create", {"config": {"alias": "Hot", "sequence": [_set({"entity_id": TANK}, 61)]}})
    assert "61 °C is above 60 °C" in ei.value.message
    assert (await d.dispatch("automations.validate", {"config": _automation(_set({"entity_id": TANK}, 55))}))["ok"] is True


async def test_a_written_scene_is_held_to_the_ceiling_and_the_modes(core: HomeAssistant, tmp_path: Path) -> None:
    _home(core)
    d = build_dispatcher(core)
    for entities, why in (
        ({TANK: {"state": "on", "temperature": 65}}, "65 °C is above 60 °C"),
        ({SMALL: {"state": "on", "temperature": 58}}, "58 °C is above 55 °C"),
        ({TANK: {"state": "performance", "temperature": 50}}, WATER_HEATER_MODE_RULE),
    ):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scenes.create", {"config": {"name": "Hot", "entities": entities}})
        assert ei.value.code == "validation_failed" and why in ei.value.message, ei.value.message
    assert (tmp_path / "scenes.yaml").read_text().strip() == "[]"
    await d.dispatch("scenes.create", {"config": {"name": "Warm", "entities": {TANK: {"state": "on", "temperature": 55, "operation_mode": "eco"}}}})


async def test_a_household_scene_over_the_ceiling_no_longer_runs_and_its_mode_still_does(core: HomeAssistant, tmp_path: Path) -> None:
    """The council reversed #557's grandfathering for the run path; a household's mode is its own (#713's line)."""
    _home(core)
    (tmp_path / "scenes.yaml").write_text(
        f"- id: 'hot'\n  name: Hot\n  entities:\n    {TANK}:\n      state: eco\n      temperature: 70\n"
        f"- id: 'boost'\n  name: Boost\n  entities:\n    {TANK}:\n      state: performance\n      temperature: 55\n"
    )
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()
    for eid in ("scene.hot", "scene.boost"):
        async_expose_entity(core, "conversation", eid, True)
    sent = async_mock_service(core, "water_heater", "set_temperature")
    async_mock_service(core, "water_heater", "set_operation_mode")
    d = build_dispatcher(core)

    assert any("70 °C is above 60 °C" in p for p in await scene_run_violations(core, "scene.hot"))
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.hot"})
    assert ei.value.code == "method_not_allowed" and "capped at 60 °C (140 °F) for safety" in ei.value.message
    assert await scene_run_violations(core, "scene.boost") == []
    await d.dispatch("scenes.activate", {"entity_id": "scene.boost"})
    await core.async_block_till_done()
    assert [c.data["temperature"] for c in sent] == [55]

    # An authored link to the hot scene is refused for the same reason.
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "scene.turn_on", "target": {"entity_id": "scene.hot"}})})
    assert res["ok"] is False and "70 °C is above 60 °C" in res["error"]


async def test_a_scene_whose_water_heater_cannot_be_read_does_not_run(core: HomeAssistant) -> None:
    """As for a valve: the membership attribute names the water heater but not its temperature."""
    _home(core)
    core.states.async_set("scene.snapshot", "unknown", {"entity_id": [TANK, "input_boolean.test"]})
    assert find_scene_run_violations(core, "scene.snapshot", None) == [
        f"{TANK}: this scene sets a water heater and Hearth cannot read its temperature, so it cannot be activated"
    ]
    core.states.async_set("scene.lamp", "unknown", {"entity_id": ["input_boolean.test"]})
    assert find_scene_run_violations(core, "scene.lamp", None) == []


async def test_a_hearth_scene_script_over_the_ceiling_is_refused_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    _home(core)
    await hl.async_ensure_labels(core)
    sent = async_mock_service(core, "water_heater", "set_temperature")
    for value, ok in ((65, False), (55, True)):
        (tmp_path / "scripts.yaml").write_text(
            f"tank:\n  alias: Tank\n  sequence:\n  - action: water_heater.set_temperature\n    target:\n      entity_id: {TANK}\n"
            f"    data:\n      temperature: {value}\n"
        )
        await core.services.async_call("script", "reload", blocking=True)
        await core.async_block_till_done()
        async_expose_entity(core, "conversation", "script.tank", True)
        await hl.async_mark_hearth_scene(core, "script.tank", True)
        d = build_dispatcher(core)
        if ok:
            await d.dispatch("scripts.run", {"entity_id": "script.tank"})
        else:
            with pytest.raises(RpcError) as ei:
                await d.dispatch("scripts.run", {"entity_id": "script.tank"})
            assert "65 °C is above 60 °C" in ei.value.message
        await core.async_block_till_done()
    assert [c.data["temperature"] for c in sent] == [55]


async def test_a_form_value_over_the_ceiling_is_refused(core: HomeAssistant) -> None:
    _home(core)
    await check_flow_input(core, {"turn_on": [_set({"entity_id": TANK}, 55)]})
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [_set({"entity_id": TANK}, 65)]})
    assert "65 °C is above 60 °C" in ei.value.message


async def test_a_household_scene_setting_a_group_of_water_heaters_is_held_to_each_members_ceiling(core: HomeAssistant, tmp_path: Path) -> None:
    """HA's group `reproduce_state` hands the group's attributes to every member, its temperature included."""
    _home(core)
    core.states.async_set("group.tanks", "on", {"entity_id": [TANK, SMALL]})
    (tmp_path / "scenes.yaml").write_text(
        "- id: 'hot'\n  name: Hot tanks\n  entities:\n    group.tanks:\n      state: eco\n      temperature: 75\n"
        "- id: 'warm'\n  name: Warm tanks\n  entities:\n    group.tanks:\n      state: eco\n      temperature: 58\n"
        "- id: 'fine'\n  name: Fine tanks\n  entities:\n    group.tanks:\n      state: eco\n      temperature: 50\n"
        "- id: 'odd'\n  name: Odd tanks\n  entities:\n    group.tanks:\n      state: eco\n      temperature: '70'\n"
        "- id: 'bare'\n  name: Bare tanks\n  entities:\n    group.tanks: 'on'\n"
    )
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "scene.hot_tanks", True)
    sent = async_mock_service(core, "water_heater", "set_temperature")

    hot = await scene_run_violations(core, "scene.hot_tanks")
    assert f"group.tanks: this group holds {TANK}; {TANK}: 75 °C is above 60 °C; {WATER_HEATER_CEILING_RULE}" in hot
    assert any(f"{SMALL}: 75 °C is above 55 °C" in p for p in hot)
    # Under the home's ceiling, over the small tank's own max_temp.
    warm = await scene_run_violations(core, "scene.warm_tanks")
    assert warm == [f"group.tanks: this group holds {SMALL}; {SMALL}: 58 °C is above 55 °C; {WATER_HEATER_CEILING_RULE}"]
    assert any("not a literal number" in p for p in await scene_run_violations(core, "scene.odd_tanks"))
    assert await scene_run_violations(core, "scene.fine_tanks") == []
    assert await scene_run_violations(core, "scene.bare_tanks") == []

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.hot_tanks"})
    assert ei.value.code == "method_not_allowed" and "capped at 60 °C (140 °F) for safety" in ei.value.message
    await core.async_block_till_done()
    assert sent == []


async def test_a_group_of_water_heaters_in_a_scene_with_no_config_does_not_run(core: HomeAssistant) -> None:
    """The membership attribute names the group but not what it is set to, as for a valve."""
    _home(core)
    core.states.async_set("group.tanks", "on", {"entity_id": [TANK]})
    core.states.async_set("scene.loose", "unknown", {"entity_id": ["group.tanks"]})
    assert find_scene_run_violations(core, "scene.loose", None) == [
        f"group.tanks: this group holds {TANK}; Hearth cannot read the temperature this scene gives it, so it cannot be activated"
    ]
