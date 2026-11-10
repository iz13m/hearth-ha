"""An integration's own service is vetted before a model calls it (R4 of the council's ruling, #581, AgDR-0076).

The walk itself is pinned against the hub's by `POLICY_WALK_CASES` and `SERVICE_CALL_CASES`
(`test_policy.py`). This file pins the lists, the refusal's tag, and the box-only half: which device a
vetted service reaches, refused when it holds a lock, alarm panel, siren, camera, valve or entrance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.hearth_ai.handlers.flows import check_flow_input
from custom_components.hearth_ai.handlers.scenes import find_config_violations, find_nested_authoring_violations
from custom_components.hearth_ai.handlers.vetted import (
    GUARDED_ADDRESS_RULE,
    GUARDED_DEVICE_RULE,
    find_vetted_target_violations,
    vetted_call_violations,
)
from custom_components.hearth_ai.policy import (
    ENTITY_SERVICE_DOMAINS,
    ENTRANCE_REFUSAL,
    ENTRANCE_RULE,
    UNVETTABLE_DOMAINS,
    UNVETTABLE_SERVICES,
    UNVETTED_REFUSAL,
    UNVETTED_RULE,
    UNVETTED_RULE_AR,
    NOTIFY_COMMAND_PREFIXES,
    NOTIFY_EXACT_COMMANDS,
    NOTIFY_COMMAND_RULE,
    VETTED_DEVICE_ACTIONS,
    VETTED_INTEGRATION_SERVICES,
    find_policy_violations,
    raise_refusal,
)
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def test_lists_match_shared() -> None:
    data = json.loads(SCHEMA.read_text())["integration_services"]
    assert set(data["entity_service_domains"]) == ENTITY_SERVICE_DOMAINS
    assert set(data["vetted_services"]) == VETTED_INTEGRATION_SERVICES
    assert set(data["vetted_device_actions"]) == VETTED_DEVICE_ACTIONS
    assert set(data["unvettable_services"]) == UNVETTABLE_SERVICES
    assert set(data["unvettable_domains"]) == UNVETTABLE_DOMAINS
    assert data["rule"] == UNVETTED_RULE
    assert data["rule_ar"] == UNVETTED_RULE_AR
    assert tuple(data["notify_command_prefixes"]) == NOTIFY_COMMAND_PREFIXES
    assert tuple(data["notify_exact_commands"]) == NOTIFY_EXACT_COMMANDS
    assert data["notify_command_rule"] == NOTIFY_COMMAND_RULE


def test_every_entity_platform_home_assistant_has_is_classified() -> None:
    """A platform Home Assistant adds is neither allowed nor forgotten: this fails until someone decides."""
    platforms = {p.value for p in Platform}
    assert platforms <= ENTITY_SERVICE_DOMAINS | UNVETTABLE_DOMAINS, sorted(platforms - ENTITY_SERVICE_DOMAINS - UNVETTABLE_DOMAINS)
    assert not ENTITY_SERVICE_DOMAINS & UNVETTABLE_DOMAINS


def test_the_refusal_is_tagged_only_when_it_is_this_rule_alone() -> None:
    unvetted = [f"config.actions[0]: action sonos.join is not yet vetted; {UNVETTED_RULE}"]
    with pytest.raises(RpcError) as ei:
        raise_refusal("method_not_allowed", unvetted)
    assert ei.value.data == dict(UNVETTED_REFUSAL)
    assert UNVETTED_REFUSAL["refusal"] == "unvetted"
    with pytest.raises(RpcError) as ei:
        raise_refusal("method_not_allowed", [*unvetted, "lock.front: entities in lock can never be controlled"])
    assert ei.value.data is None
    assert ei.value.message == "lock.front: entities in lock can never be controlled"
    with pytest.raises(RpcError) as ei:
        raise_refusal("method_not_allowed", [f"cover.garage: {ENTRANCE_RULE}"])
    assert ei.value.data == dict(ENTRANCE_REFUSAL)
    # Both rules and nothing else: neither tag claims the whole refusal.
    with pytest.raises(RpcError) as ei:
        raise_refusal("method_not_allowed", [*unvetted, f"cover.garage: {ENTRANCE_RULE}"])
    assert ei.value.data is None


def test_a_household_script_one_hop_away_is_not_held_to_it() -> None:
    own = {"sequence": [{"action": "sonos.join", "data": {"master": "media_player.kitchen"}}]}
    assert find_policy_violations(own, vet_services=False) == []
    assert len(find_policy_violations(own)) == 1
    # Denials by name still apply there.
    assert find_policy_violations({"sequence": [{"action": "mqtt.publish"}]}, vet_services=False) == ["config.sequence[0]: action mqtt.publish is not allowed"]


ZWAVE_LOCK_NODE = "5"
ZHA_SIREN_IEEE = "00:0d:6f:00:0a:90:69:e7"


def _home(core: HomeAssistant) -> dict[str, str]:
    """A TV with a lock's keypad wired to it (contrived, but one device), a plain TV, a phone, an Echo, a Z-Wave lock, a ZHA siren,
    an alarm panel and a water valve. The lock, the panel and the valve each carry a switch or a select beside them, as real ones do."""
    dev_reg = dr.async_get(core)
    ent_reg = er.async_get(core)
    out: dict[str, str] = {}

    def device(name: str, domain: str, identifier: str) -> str:
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(core)
        out[name] = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(domain, identifier)}).id
        return out[name]

    def entity(domain: str, platform: str, object_id: str, device_id: str) -> None:
        ent_reg.async_get_or_create(domain, platform, f"{platform}-{object_id}", suggested_object_id=object_id, device_id=device_id)
        core.states.async_set(f"{domain}.{object_id}", "on")
        async_expose_entity(core, "conversation", f"{domain}.{object_id}", True)

    entity("media_player", "webostv", "lg_tv", device("tv", "webostv", "tv-1"))
    guarded_tv = device("guarded_tv", "webostv", "tv-2")
    entity("media_player", "webostv", "hall_tv", guarded_tv)
    entity("camera", "webostv", "hall_tv_camera", guarded_tv)
    device("phone", "mobile_app", "phone-1")
    device("echo", "alexa_devices", "echo-1")
    lock = device("lock", "zwave_js", f"3967201280-{ZWAVE_LOCK_NODE}")
    entity("lock", "zwave_js", "front_door", lock)
    entity("binary_sensor", "zwave_js", "front_door_lock_door", lock)
    entity("switch", "zwave_js", "front_door_auto_relock", lock)
    entity("select", "zwave_js", "front_door_operating_mode", lock)
    siren = device("siren", "zha", ZHA_SIREN_IEEE)
    entity("siren", "zha", "hallway", siren)
    alarm = device("alarm", "elkm1", "panel-1")
    entity("alarm_control_panel", "elkm1", "house", alarm)
    entity("switch", "elkm1", "house_chime", alarm)
    valve = device("valve", "zwave_js", "3967201280-9")
    entity("valve", "zwave_js", "water_main", valve)
    entity("switch", "zwave_js", "water_main_leak_alarm", valve)
    plug = device("plug", "zwave_js", "3967201280-12")
    entity("switch", "zwave_js", "kettle", plug)
    return out


def _automation(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"alias": "R4", "triggers": [{"trigger": "time", "at": "07:00"}], "actions": list(actions)}


async def test_devices_call_refuses_a_vetted_service_at_a_device_holding_a_camera(core: HomeAssistant) -> None:
    _home(core)
    pressed = async_mock_service(core, "webostv", "button")
    d = build_dispatcher(core)
    await d.dispatch("devices.call", {"domain": "webostv", "service": "button", "entity_id": ["media_player.lg_tv"], "data": {"button": "HOME"}})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "webostv", "service": "button", "entity_id": ["media_player.hall_tv"], "data": {"button": "HOME"}})
    assert ei.value.code == "method_not_allowed"
    assert "reaches a device that has a lock, alarm panel, siren, camera, valve or entrance" in ei.value.message
    await core.async_block_till_done()
    assert [c.data["entity_id"] for c in pressed] == [["media_player.lg_tv"]]


async def test_devices_call_refuses_an_unvetted_service_tagged(core: HomeAssistant) -> None:
    _home(core)
    refreshed = async_mock_service(core, "zwave_js", "refresh_value")
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "zwave_js", "service": "refresh_value", "entity_id": ["binary_sensor.front_door_lock_door"]})
    assert ei.value.code == "method_not_allowed"
    assert ei.value.data == dict(UNVETTED_REFUSAL)
    await core.async_block_till_done()
    assert refreshed == []


async def test_a_vetted_service_reaches_a_lock_by_its_address(core: HomeAssistant) -> None:
    """Every address a vetted service can name, against a device holding a lock or a siren. The seed list
    has no service that takes a node or an IEEE address, so the walk is asked as if it did."""
    ids = _home(core)

    def refused(action: dict[str, Any], why: str) -> None:
        problems = find_vetted_target_violations(core, _automation(action))
        assert any(why in p for p in problems), (action, problems)

    sound = {"action": "alexa_devices.send_sound", "data": {"sound": "bell_02"}}
    assert find_vetted_target_violations(core, _automation({**sound, "data": {**sound["data"], "device_id": ids["echo"]}})) == []
    refused({**sound, "data": {**sound["data"], "device_id": ids["lock"]}}, "reaches a device that has a lock")
    refused({**sound, "target": {"device_id": [ids["echo"], ids["siren"]]}}, "reaches a device that has a lock")
    refused({**sound, "data": {**sound["data"], "node_id": int(ZWAVE_LOCK_NODE)}}, "reaches a device that has a lock")
    refused({**sound, "data": {**sound["data"], "ieee": ZHA_SIREN_IEEE.upper()}}, "reaches a device that has a lock")
    refused({**sound, "target": {"entity_id": "binary_sensor.front_door_lock_door"}}, "reaches a device that has a lock")
    # What it cannot resolve, it refuses.
    refused({**sound, "data": {**sound["data"], "device_id": "0" * 32}}, "is not a device Home Assistant has")
    refused({**sound, "data": {**sound["data"], "node_id": 99}}, "is not a device Home Assistant has")
    refused({**sound, "data": {**sound["data"], "ieee": "ff:ff:ff:ff:ff:ff:ff:ff"}}, "is not a device Home Assistant has")
    refused({**sound, "data": {**sound["data"], "device_id": {"x": 1}}}, "is not an address Azka can check")
    # The companion app's notify device action, at a phone and at a lock.
    notify = {"domain": "mobile_app", "type": "notify", "message": "Dinner"}
    assert find_vetted_target_violations(core, _automation({**notify, "device_id": ids["phone"]})) == []
    refused({**notify, "device_id": ids["lock"]}, "reaches a device that has a lock")


async def test_every_authoring_surface_asks(core: HomeAssistant) -> None:
    ids = _home(core)
    at_lock = {"action": "alexa_devices.send_sound", "data": {"device_id": ids["lock"], "sound": "bell_02"}}
    assert any("reaches a device that has a lock" in p for p in await find_config_violations(core, _automation(at_lock)))
    assert any("not yet vetted" in p for p in await find_config_violations(core, _automation({"action": "esphome.front_door_unlock"})))
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [at_lock]})
    assert "reaches a device that has a lock" in ei.value.message
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [{"action": "sonos.join"}]})
    assert "not yet vetted" in ei.value.message
    fine = {"action": "alexa_devices.send_sound", "data": {"device_id": ids["echo"], "sound": "bell_02"}}
    assert await find_config_violations(core, _automation(fine)) == []


async def test_a_household_script_started_by_an_authored_automation_keeps_its_integration_services(core: HomeAssistant, tmp_path: Path) -> None:
    """One hop away is the household's own (AgDR-0005), as for the remote platform allowlist (#765)."""
    from custom_components.hearth_ai.handlers import scenes as scenes_mod  # noqa: PLC0415

    script = {"sequence": [{"action": "sonos.join", "data": {"master": "media_player.kitchen", "entity_id": "media_player.hall"}}]}
    core.states.async_set("script.group_speakers", "off")
    original = scenes_mod._script_raw_config
    scenes_mod._script_raw_config = lambda hass, entity_id: script if entity_id == "script.group_speakers" else original(hass, entity_id)
    try:
        assert await find_nested_authoring_violations(core, _automation({"action": "script.turn_on", "target": {"entity_id": "script.group_speakers"}})) == []
    finally:
        scenes_mod._script_raw_config = original


# R4's security review (AgDR-0076): the device check is asked of every service and device action that
# names a device by address, whatever its domain. `switch.turn_off` at a lock's device reaches its
# auto-relock switch, and a `select` reaches its operating mode, with no entity named for the rules to see.


async def test_any_service_that_names_a_guarded_device_is_refused(core: HomeAssistant) -> None:
    ids = _home(core)
    reached = "reaches a device that has a lock, alarm panel, siren, camera, valve or entrance"
    for name in ("lock", "alarm", "valve", "siren", "guarded_tv"):
        for action in (
            {"action": "switch.turn_off", "target": {"device_id": ids[name]}},
            {"action": "select.select_option", "target": {"device_id": ids[name]}, "data": {"option": "Manual"}},
            {"action": "homeassistant.turn_off", "device_id": ids[name]},
            {"action": "switch.turn_off", "data": {"device_id": [ids["plug"], ids[name]]}},
            {"device_id": ids[name], "domain": "select", "type": "select_option", "entity_id": "select.front_door_operating_mode", "option": "Manual"},
            {"device_id": ids[name], "domain": "switch", "type": "turn_off", "entity_id": "switch.front_door_auto_relock"},
        ):
            problems = find_vetted_target_violations(core, _automation(action))
            assert any(reached in p for p in problems), (name, action, problems)
            # The same, through every authoring surface.
            assert any(reached in p for p in await find_config_violations(core, _automation(action))), (name, action)
    # The controls: the same shapes at a plain plug.
    for action in (
        {"action": "switch.turn_off", "target": {"device_id": ids["plug"]}},
        {"action": "select.select_option", "target": {"device_id": ids["plug"]}, "data": {"option": "Manual"}},
        {"device_id": ids["plug"], "domain": "switch", "type": "turn_off", "entity_id": "switch.kettle"},
    ):
        assert find_vetted_target_violations(core, _automation(action)) == [], action


async def test_a_templated_address_is_refused_in_any_domain_and_an_unknown_one_reaches_nothing(core: HomeAssistant) -> None:
    _home(core)
    for action in (
        {"action": "switch.turn_off", "target": {"device_id": "{{ device_id('switch.kettle') }}"}},
        {"device_id": "{{ d }}", "domain": "switch", "type": "turn_off", "entity_id": "switch.kettle"},
    ):
        assert any("so what it reaches cannot be checked" in p for p in find_vetted_target_violations(core, _automation(action))), action
    # A registry id Home Assistant does not have reaches nothing, and cannot later be a lock's.
    for action in (
        {"action": "switch.turn_off", "target": {"device_id": "0" * 32}},
        {"device_id": "0" * 32, "domain": "switch", "type": "turn_off", "entity_id": "switch.kettle"},
    ):
        assert find_vetted_target_violations(core, _automation(action)) == [], action


async def test_a_trigger_a_condition_and_a_scripts_variables_are_not_asked(core: HomeAssistant) -> None:
    ids = _home(core)
    config = {
        "alias": "R4",
        "triggers": [{"trigger": "device", "device_id": ids["siren"], "domain": "zha", "type": "device_offline"}],
        "conditions": [{"condition": "device", "device_id": ids["lock"], "domain": "zwave_js", "type": "value", "property": "x"}],
        "actions": [
            {"wait_for_trigger": [{"trigger": "device", "device_id": ids["lock"], "domain": "zwave_js", "type": "event.value_notification"}]},
            {"if": [{"condition": "device", "device_id": ids["alarm"], "domain": "elkm1", "type": "is_on"}], "then": []},
            # A script's `data` is its variables; what the script does with them is asked of the script.
            {"action": "script.turn_on", "target": {"entity_id": "script.tell"}, "data": {"variables": {"device_id": ids["lock"]}}},
            {"action": "script.tell", "data": {"device_id": ids["lock"]}},
        ],
    }
    assert find_vetted_target_violations(core, config) == []


async def test_a_switch_as_x_lock_makes_its_device_guarded(core: HomeAssistant) -> None:
    """Effective domains count (#643). A `switch_as_x` lock over a switch group hands the lock's gate to
    each member (ruling 9), so a member relay's device holds a lock by effective domain alone: the
    wrapper and the group sit on no device, and nothing on the relay's device is a `lock.*` entity."""
    from .test_switch_as_x import _group, _wrap  # noqa: PLC0415

    ids = _home(core)
    dev_reg, ent_reg = dr.async_get(core), er.async_get(core)
    entry = MockConfigEntry(domain="hearth_test")
    entry.add_to_hass(core)
    relay_device = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hearth_test", "relay-1")}).id
    for object_id in ("porch_relay", "porch_light"):
        ent_reg.async_get_or_create("switch", "hearth_test", object_id, suggested_object_id=object_id, device_id=relay_device if object_id == "porch_relay" else None)
        core.states.async_set(f"switch.{object_id}", "off")
        async_expose_entity(core, "conversation", f"switch.{object_id}", True)
    sound = {"action": "alexa_devices.send_sound", "data": {"sound": "bell_02", "device_id": relay_device}}
    off = {"action": "switch.turn_off", "target": {"device_id": relay_device}}
    # Before the helper, a plain relay.
    assert find_vetted_target_violations(core, _automation(sound)) == []
    assert find_vetted_target_violations(core, _automation(off)) == []
    outside = await _group(core, "switch", ["switch.porch_relay", "switch.porch_light"], "All outside")
    await _wrap(core, outside, "lock")
    # Not vacuous: no entity on the relay's device is literally a lock.
    assert all(e.domain != "lock" for e in er.async_entries_for_device(ent_reg, relay_device, include_disabled_entities=True))
    for action in (sound, off):
        assert any("reaches a device that has a lock" in p for p in find_vetted_target_violations(core, _automation(action))), action
    assert find_vetted_target_violations(core, _automation({**sound, "data": {**sound["data"], "device_id": ids["echo"]}})) == []


async def test_an_entrance_labelled_device_is_guarded(core: HomeAssistant) -> None:
    from custom_components.hearth_ai import labels as hl  # noqa: PLC0415

    _home(core)
    await hl.async_ensure_labels(core)
    label = hl.label_id_for(core, "entrance")
    assert label is not None
    dev_reg, ent_reg = dr.async_get(core), er.async_get(core)
    entry = MockConfigEntry(domain="hearth_test")
    entry.add_to_hass(core)
    gate_device = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hearth_test", "gate-1")}).id
    ent = ent_reg.async_get_or_create("cover", "hearth_test", "yard", suggested_object_id="yard", device_id=gate_device)
    core.states.async_set("cover.yard", "closed", {"device_class": "shade"})
    sound = {"action": "alexa_devices.send_sound", "data": {"sound": "bell_02", "device_id": gate_device}}
    # A shade, unlabelled: not an entrance.
    assert find_vetted_target_violations(core, _automation(sound)) == []
    ent_reg.async_update_entity(ent.entity_id, labels={label})
    await core.async_block_till_done()
    assert any("reaches a device that has a lock" in p for p in find_vetted_target_violations(core, _automation(sound)))


async def test_a_labelled_script_calling_an_unvetted_service_still_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """Its run-time check is for what it may not touch, not for which integration it calls (AgDR-0076)."""
    from custom_components.hearth_ai import labels as hl  # noqa: PLC0415
    from custom_components.hearth_ai.handlers.scripts import script_run_violations  # noqa: PLC0415
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    (tmp_path / "scripts.yaml").write_text(
        "speakers:\n  alias: Speakers\n  sequence:\n  - action: sonos.join\n    data:\n      master: media_player.kitchen\n      entity_id: media_player.hall\n"
    )
    assert await async_setup_component(core, "script", {})
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    await hl.async_ensure_labels(core)
    await hl.async_mark_hearth_scene(core, "script.speakers", True)
    assert await script_run_violations(core, "script.speakers") == []
    # The control: the same script written by a model is refused as not yet vetted.
    assert any("not yet vetted" in p for p in find_policy_violations({"sequence": [{"action": "sonos.join", "data": {"master": "media_player.kitchen"}}]}))


async def test_notify_send_refuses_a_companion_app_command(core: HomeAssistant) -> None:
    """`notify.send` reaches a phone directly: the command rule applies to it as to a written notification."""
    sent = async_mock_service(core, "notify", "mobile_app_phone")
    d = build_dispatcher(core)
    for message in ("command_dnd", "{{ states('input_text.note') }}", " Request_Location_Update"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("notify.send", {"target": "notify.mobile_app_phone", "message": message})
        assert ei.value.code == "method_not_allowed" and NOTIFY_COMMAND_RULE in ei.value.message, message
    await core.async_block_till_done()
    assert sent == []
    await d.dispatch("notify.send", {"target": "notify.mobile_app_phone", "message": "Dinner is ready"})
    await core.async_block_till_done()
    assert [c.data["message"] for c in sent] == ["Dinner is ready"]


# Security re-review of R4 (AgDR-0076): what the any-domain check cannot read. A templated carrier or a
# templated key can render to a lock's device, and an area, floor or label reaches every device in it.
# Refused where a guarded device holds an entity in the service's own domain, and only there.

SELECTS = "selects by"


def _selects(core: HomeAssistant, action: dict[str, Any]) -> list[str]:
    return [p for p in find_vetted_target_violations(core, _automation(action)) if SELECTS in p]


async def test_a_target_that_cannot_be_read_is_refused_where_a_lock_could_be_reached(core: HomeAssistant) -> None:
    ids = _home(core)
    lock = ids["lock"]
    for action in (
        {"action": "switch.turn_off", "target": "{{ {'device_id': '" + lock + "'} }}"},
        {"action": "switch.turn_off", "data": "{{ {'device_id': '" + lock + "'} }}"},
        {"action": "switch.turn_off", "data_template": "{{ {'device_id': '" + lock + "'} }}"},
        {"action": "switch.turn_off", "target": {"{{ 'device_id' }}": lock}},
        {"action": "select.select_option", "data": {"{{ 'device' ~ '_id' }}": lock, "option": "Manual"}},
        {"action": "homeassistant.turn_off", "target": "{{ x }}"},
    ):
        problems = _selects(core, action)
        assert any("a target Azka cannot read until it runs" in p and GUARDED_ADDRESS_RULE in p for p in problems), (action, problems)


async def test_an_area_a_floor_or_a_label_is_refused_where_a_lock_could_be_reached(core: HomeAssistant) -> None:
    _home(core)
    for key in ("area_id", "floor_id", "label_id"):
        for action in (
            {"action": "switch.turn_off", "target": {key: "hall"}},
            {"action": "select.select_option", "target": {key: "hall"}, "data": {"option": "Manual"}},
            {"action": "homeassistant.turn_off", "target": {key: "hall"}},
            {"action": "switch.turn_off", "data": {key: ["hall", "porch"]}},
        ):
            problems = _selects(core, action)
            assert any(f"selects by {key}" in p for p in problems), (action, problems)
    # Every entity of the domain, or any one of them (third security review).
    for action in (
        {"action": "switch.turn_off", "target": {"entity_id": "all"}},
        {"action": "switch.turn_off", "entity_id": "ALL"},
        {"action": "switch.turn_off", "data": {"entity_id": "all"}},
        {"action": "select.select_option", "target": {"entity_id": "all"}, "data": {"option": "Manual"}},
        {"action": "switch.turn_off", "target": {"entity_id": ["switch.kettle", "all"]}},
        {"action": "switch.turn_off", "target": {"entity_id": "switch.kettle, all"}},
        {"action": "switch.turn_off", "target": {"entity_id": "{{ 'switch.' ~ x }}"}},
        {"action": "switch.turn_off", "target": {"entity_id": ["switch.kettle", "{{ x }}"]}},
        {"action": "homeassistant.turn_off", "target": {"entity_id": "all"}},
        {"action": "switch.turn_off", "target": {"entity_id": "none"}},
    ):
        problems = _selects(core, action)
        assert any("selects by entity_id" in p for p in problems), (action, problems)


async def test_a_validated_templated_entity_id_is_refused_where_a_lock_could_be_reached(core: HomeAssistant) -> None:
    """`_validate`'s second walk reads Home Assistant's validated config, where a templated id is a `Template`."""
    from homeassistant.helpers import config_validation as cv  # noqa: PLC0415

    _home(core)
    validated = cv.SERVICE_SCHEMA({"action": "switch.turn_off", "target": {"entity_id": "{{ 'switch.' ~ x }}"}})
    problems = [p for p in find_vetted_target_violations(core, {"actions": [validated]}) if SELECTS in p]
    assert any("selects by entity_id" in p for p in problems), problems


async def test_selection_is_left_alone_where_no_guarded_device_could_be_reached(core: HomeAssistant) -> None:
    """The controls: a domain no guarded device holds, a home with no guarded device, and a script's own selection."""
    _home(core)
    for action in (
        {"action": "light.turn_on", "target": {"area_id": "hall"}},
        {"action": "light.turn_on", "target": "{{ {'area_id': 'hall'} }}"},
        {"action": "light.turn_on", "target": {"entity_id": "all"}},
        {"action": "light.turn_on", "target": {"entity_id": "{{ 'light.' ~ x }}"}},
        {"action": "switch.turn_off", "target": {"entity_id": ["switch.kettle"]}},
        {"action": "script.turn_on", "target": "{{ {'entity_id': 'script.x'} }}"},
        {"action": "script.tell", "data": "{{ {'device_id': 'x'} }}"},
    ):
        assert _selects(core, action) == [], action


async def test_a_home_without_a_guarded_device_selects_freely(core: HomeAssistant) -> None:
    dev_reg, ent_reg = dr.async_get(core), er.async_get(core)
    entry = MockConfigEntry(domain="hearth_test")
    entry.add_to_hass(core)
    plug = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hearth_test", "plug-1")}).id
    ent_reg.async_get_or_create("switch", "hearth_test", "kettle", suggested_object_id="kettle", device_id=plug)
    for action in (
        {"action": "switch.turn_off", "target": {"area_id": "kitchen"}},
        {"action": "switch.turn_off", "target": "{{ {'device_id': x} }}"},
        {"action": "homeassistant.turn_off", "target": {"label_id": "night"}},
    ):
        assert _selects(core, action) == [], action


async def test_the_any_domain_refusal_says_name_the_entities_and_a_vetted_one_keeps_its_own(core: HomeAssistant) -> None:
    ids = _home(core)
    (plain,) = find_vetted_target_violations(core, _automation({"action": "switch.turn_off", "target": {"device_id": ids["lock"]}}))
    assert GUARDED_ADDRESS_RULE in plain and GUARDED_DEVICE_RULE not in plain
    assert "name the entities" in plain and "اذكر الكيانات" in plain
    (vetted,) = find_vetted_target_violations(core, _automation({"action": "alexa_devices.send_sound", "data": {"device_id": ids["lock"], "sound": "x"}}))
    assert GUARDED_DEVICE_RULE in vetted and GUARDED_ADDRESS_RULE not in vetted
