"""No assistant opens an entrance; closing one is allowed (R3 of the council's ruling of 2026-11-09, AgDR-0074).

The walk itself is pinned against the hub by the shared corpus (`test_policy.py`, the entrance cases in
`policy-cases.ts`). These rows cover what only the box can do: decide which entities are entrances
(`handlers/entrances.py`), and hand that set to every place a model can reach one — `devices.call`, an
authored automation, script or scene, a config-flow form value, a script an automation starts, a stored
scene when it runs, and the `entrance` flag the hub's voice path reads. Every refusal has a control that
differs from it in one thing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers import flows
from custom_components.hearth_ai.handlers.entrances import entrance_ids, entrance_of
from custom_components.hearth_ai.policy import ENTRANCE_RULE, ENTRANCE_RULE_AR
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

GARAGE = "cover.garage_door"
BLIND = "cover.kitchen_blind"
RELAY = "switch.gate_relay"
LAMP = "switch.hall_lamp"


def _share(hass: HomeAssistant, eid: str, state: str, **attrs: Any) -> None:
    hass.states.async_set(eid, state, attrs)
    async_expose_entity(hass, "conversation", eid, True)


async def _home(hass: HomeAssistant) -> None:
    """A garage-class cover, a blind, a relay named for a gate and an ordinary switch, all shared."""
    _share(hass, GARAGE, "closed", device_class="garage", friendly_name="Garage door")
    _share(hass, BLIND, "closed", device_class="blind", friendly_name="Kitchen blind")
    _share(hass, RELAY, "off", friendly_name="Gate relay")
    _share(hass, LAMP, "off", friendly_name="Hall lamp")
    await hl.async_ensure_labels(hass)


def _calls(hass: HomeAssistant, domain: str, *services: str) -> dict[str, list[Any]]:
    return {s: async_mock_service(hass, domain, s) for s in services}


async def _call(hass: HomeAssistant, domain: str, service: str, ids: list[str], data: dict[str, Any] | None = None) -> Any:
    params: dict[str, Any] = {"domain": domain, "service": service, "entity_id": ids}
    if data is not None:
        params["data"] = data
    return await build_dispatcher(hass).dispatch("devices.call", params)


# --------------------------------------------------------------------------- what an entrance is


async def test_the_box_decides_which_entities_are_entrances(core: HomeAssistant) -> None:
    await _home(core)
    _share(core, "cover.driveway", "closed", device_class="gate")
    _share(core, "cover.side", "closed", device_class="door")
    _share(core, "switch.garage", "off")
    _share(core, "switch.relay_2", "off", friendly_name="بوابة الحديقة")
    _share(core, "switch.relay_3", "off", friendly_name="الكراج")
    _share(core, "switch.zigbee_gateway_led", "off", friendly_name="Gateway LED")
    _share(core, "switch.tailgate_sensor_power", "off")
    _share(core, "light.garage", "off")
    found = entrance_ids(core)
    for eid in (GARAGE, "cover.driveway", "cover.side", RELAY, "switch.garage", "switch.relay_2", "switch.relay_3"):
        assert eid in found, eid
    # A word, not a substring; and the name rule is for switches only, so a light needs the label.
    for eid in (BLIND, LAMP, "switch.zigbee_gateway_led", "switch.tailgate_sensor_power", "light.garage"):
        assert eid not in found, eid
    for eid in found | {BLIND, LAMP}:
        assert entrance_of(core, eid) == (eid in found), eid


async def test_a_class_set_only_in_the_registry_counts(core: HomeAssistant) -> None:
    ent_reg = er.async_get(core)
    entry = ent_reg.async_get_or_create("cover", "test", "drive", suggested_object_id="drive", original_device_class="gate")
    _share(core, entry.entity_id, "closed")
    assert entry.entity_id in entrance_ids(core)


async def test_the_label_counts_on_the_entity_and_on_its_own_device(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    label = hl.label_id_for(core, "entrance")
    assert label is not None
    ent_reg, dev_reg = er.async_get(core), dr.async_get(core)
    config = MockConfigEntry(domain="test")
    config.add_to_hass(core)
    device = dev_reg.async_get_or_create(config_entry_id=config.entry_id, identifiers={("test", "shelly")})
    by_entity = ent_reg.async_get_or_create("light", "test", "porch", suggested_object_id="porch")
    by_device = ent_reg.async_get_or_create("input_boolean", "test", "relay", suggested_object_id="relay", device_id=device.id)
    plain = ent_reg.async_get_or_create("light", "test", "hall", suggested_object_id="hall")
    for e in (by_entity, by_device, plain):
        _share(core, e.entity_id, "off")
    assert by_entity.entity_id not in entrance_ids(core)
    ent_reg.async_update_entity(by_entity.entity_id, labels={label})
    dev_reg.async_update_device(device.id, labels={label})
    found = entrance_ids(core)
    assert by_entity.entity_id in found
    assert by_device.entity_id in found
    assert plain.entity_id not in found
    # A label the owner made by hand, with the exact name, counts as well (labels are matched by name too).
    assert hl.is_entrance_labelled(core, ent_reg.async_get(by_entity.entity_id))


async def test_a_group_holding_an_entrance_is_one(core: HomeAssistant) -> None:
    await _home(core)
    _share(core, "cover.outside", "closed", entity_id=[BLIND, GARAGE])
    _share(core, "group.everything", "off", entity_id=["cover.outside", LAMP])
    _share(core, "cover.inside", "closed", entity_id=[BLIND])
    found = entrance_ids(core)
    assert {"cover.outside", "group.everything"} <= found
    assert "cover.inside" not in found
    assert entrance_of(core, "group.everything") and not entrance_of(core, "cover.inside")


async def test_the_list_carries_the_flag_the_hub_reads(core: HomeAssistant) -> None:
    await _home(core)
    rows = {r["entity_id"]: r for r in await build_dispatcher(core).dispatch("entities.list", {})}
    assert rows[GARAGE]["entrance"] is True
    assert rows[RELAY]["entrance"] is True
    assert rows[BLIND]["entrance"] is False
    assert rows[LAMP]["entrance"] is False


async def test_adding_the_label_tells_the_hub(core: HomeAssistant) -> None:
    """Otherwise the voice model would learn of a new entrance only on its timed refresh (#613, criterion 11)."""
    await hl.async_ensure_labels(core)
    sent: list[int] = []

    async def send() -> None:
        sent.append(1)

    watcher = hl.RegistryWatcher(core, send)
    assert watcher._touches({hl.label_id_for(core, "entrance")})
    assert not watcher._touches({"some_other_label"})


async def test_a_model_cannot_strip_or_delete_the_label(core: HomeAssistant) -> None:
    """The guard from #617 covers every label in `ALL_LABELS`, which now holds this one."""
    await _home(core)
    d = build_dispatcher(core)
    label = hl.label_id_for(core, "entrance")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.delete", {"label_id": label})
    assert "Hearth: entrance" in ei.value.message
    assert hl.ROLES.keys() == {"tile", "read_only", "setting", "diagnostic", "hide"}


# --------------------------------------------------------------------------- devices.call


async def test_opening_an_entrance_is_refused_and_closing_it_is_not(core: HomeAssistant) -> None:
    await _home(core)
    calls = _calls(core, "cover", "open_cover", "close_cover", "toggle", "stop_cover", "set_cover_position")
    for service, data in [("open_cover", None), ("toggle", None), ("stop_cover", None), ("set_cover_position", {"position": 40})]:
        with pytest.raises(RpcError) as ei:
            await _call(core, "cover", service, [GARAGE], data)
        assert ei.value.code == "method_not_allowed", service
        assert ENTRANCE_RULE in ei.value.message
        assert ei.value.data == {"refusal": "entrance", "message_en": ENTRANCE_RULE, "message_ar": ENTRANCE_RULE_AR}
    assert not any(calls[s] for s in calls)
    await _call(core, "cover", "close_cover", [GARAGE])
    await _call(core, "cover", "set_cover_position", [GARAGE], {"position": 0})
    assert len(calls["close_cover"]) == 1 and len(calls["set_cover_position"]) == 1
    # Control: the blind beside it opens.
    await _call(core, "cover", "open_cover", [BLIND])
    assert len(calls["open_cover"]) == 1


async def test_forwarding_does_not_open_an_entrance(core: HomeAssistant) -> None:
    await _home(core)
    generic = _calls(core, "homeassistant", "turn_on", "toggle", "turn_off")
    for service in ("turn_on", "toggle", "turn_off"):
        with pytest.raises(RpcError):
            await _call(core, "homeassistant", service, [GARAGE])
    assert not any(generic[s] for s in generic)


async def test_an_entrance_relay_may_be_switched_off_only(core: HomeAssistant) -> None:
    await _home(core)
    calls = _calls(core, "switch", "turn_on", "toggle", "turn_off")
    for service in ("turn_on", "toggle"):
        with pytest.raises(RpcError):
            await _call(core, "switch", service, [LAMP, RELAY])
    assert not calls["turn_on"] and not calls["toggle"]
    await _call(core, "switch", "turn_off", [RELAY])
    await _call(core, "switch", "turn_on", [LAMP])
    assert len(calls["turn_off"]) == 1 and len(calls["turn_on"]) == 1


async def test_a_group_holding_an_entrance_is_not_opened(core: HomeAssistant) -> None:
    await _home(core)
    _share(core, "cover.outside", "closed", entity_id=[BLIND, GARAGE])
    calls = _calls(core, "cover", "open_cover", "close_cover")
    with pytest.raises(RpcError):
        await _call(core, "cover", "open_cover", ["cover.outside"])
    await _call(core, "cover", "close_cover", ["cover.outside"])
    assert not calls["open_cover"] and len(calls["close_cover"]) == 1


async def test_an_unshared_entrance_is_still_unknown_rather_than_named(core: HomeAssistant) -> None:
    """#648's rule: an id the model may not see is refused in the words an unknown id gets."""
    await _home(core)
    core.states.async_set("cover.secret_gate", "closed", {"device_class": "gate"})
    async_expose_entity(core, "conversation", "cover.secret_gate", False)
    _calls(core, "cover", "open_cover")
    with pytest.raises(RpcError) as ei:
        await _call(core, "cover", "open_cover", ["cover.secret_gate"])
    assert ei.value.code == "not_found", ei.value.message
    assert ENTRANCE_RULE not in ei.value.message


# --------------------------------------------------------------------------- authoring


def _automation(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"alias": "probe", "triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": list(actions)}


async def _validate(hass: HomeAssistant, kind: str, *actions: dict[str, Any]) -> dict[str, Any]:
    config = _automation(*actions) if kind == "automation" else {"alias": "probe", "sequence": list(actions)}
    return await build_dispatcher(hass).dispatch(f"{kind}s.validate", {"config": config})


OPEN = {"action": "cover.open_cover", "target": {"entity_id": GARAGE}}
CLOSE = {"action": "cover.close_cover", "target": {"entity_id": GARAGE}}


@pytest.mark.parametrize("kind", ["automation", "script"])
async def test_an_authored_routine_may_close_an_entrance_and_never_open_one(core: HomeAssistant, kind: str) -> None:
    await _home(core)
    refused = [
        [OPEN],
        [{"delay": {"minutes": 5}}, OPEN],
        [{"if": [{"condition": "template", "value_template": "{{ true }}"}], "then": [{"delay": 1}, OPEN]}],
        [{"action": "switch.turn_on", "target": {"entity_id": RELAY}}],
        [{"action": "homeassistant.toggle", "target": {"entity_id": GARAGE}}],
        [{"action": "cover.open_cover", "target": {"area_id": "garage"}}],
        [{"action": "cover.open_cover", "target": {"entity_id": "{{ 'cover.garage' ~ '_door' }}"}}],
        [{"action": "scene.apply", "data": {"entities": {GARAGE: "open"}}}],
    ]
    for actions in refused:
        res = await _validate(core, kind, *actions)
        assert res["ok"] is False and res["status"] == "policy", actions
        assert "entrance" in res["error"], actions
    for actions in ([CLOSE], [{"delay": 5}, CLOSE], [{"action": "cover.open_cover", "target": {"entity_id": BLIND}}], [{"action": "switch.turn_off", "target": {"entity_id": RELAY}}]):
        res = await _validate(core, kind, *actions)
        assert res["ok"] is True, (actions, res)


async def test_an_area_target_is_allowed_where_the_home_has_no_entrance_in_that_domain(core: HomeAssistant) -> None:
    _share(core, BLIND, "closed", device_class="blind")
    res = await _validate(core, "automation", {"action": "cover.open_cover", "target": {"area_id": "kitchen"}})
    assert res["ok"] is True, res
    await _home(core)
    res = await _validate(core, "automation", {"action": "cover.open_cover", "target": {"area_id": "kitchen"}})
    assert res["ok"] is False


async def test_nothing_is_written_when_a_routine_opens_an_entrance(core: HomeAssistant, tmp_path: Path) -> None:
    await _home(core)
    with pytest.raises(RpcError) as ei:
        await build_dispatcher(core).dispatch("automations.create", {"config": _automation({"delay": 30}, OPEN)})
    assert ei.value.code == "validation_failed"
    assert (tmp_path / "automations.yaml").read_text() == "[]\n"


async def test_an_automation_may_not_start_a_household_script_that_opens_an_entrance(core: HomeAssistant, tmp_path: Path) -> None:
    """One hop removed: an unattended model-written rule starting the owner's opener is a model opening it."""
    await _home(core)
    (tmp_path / "scripts.yaml").write_text(
        "open_garage:\n  sequence:\n    - action: cover.open_cover\n      target:\n        entity_id: cover.garage_door\n"
        "close_garage:\n  sequence:\n    - action: cover.close_cover\n      target:\n        entity_id: cover.garage_door\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    refused = await _validate(core, "automation", {"action": "script.turn_on", "target": {"entity_id": "script.open_garage"}})
    assert refused["ok"] is False and "entrance" in refused["error"]
    allowed = await _validate(core, "automation", {"action": "script.turn_on", "target": {"entity_id": "script.close_garage"}})
    assert allowed["ok"] is True, allowed


async def test_a_form_value_that_opens_an_entrance_is_refused(core: HomeAssistant) -> None:
    """A template switch whose `turn_on` opens the garage is a stored opener (#616's wrapper path)."""
    await _home(core)
    with pytest.raises(RpcError) as ei:
        await flows.check_flow_input(core, {"name": "Garage 2", "turn_on": [OPEN], "turn_off": [CLOSE]})
    assert "entrance" in ei.value.message
    # Control: a template switch over the blind is not an entrance's business. A form that names an
    # entrance at all is refused since the review of 3a01d37 (`test_a_form_may_not_name_an_entrance`).
    blind = {"action": "cover.open_cover", "target": {"entity_id": BLIND}}
    await flows.check_flow_input(core, {"name": "Blind 2", "turn_on": [blind], "turn_off": [blind]})


async def test_a_scene_may_set_an_entrance_only_shut(core: HomeAssistant) -> None:
    await _home(core)
    d = build_dispatcher(core)
    for entities in ({GARAGE: "open"}, {GARAGE: {"state": "open", "current_position": 50}}, {RELAY: "on"}):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scenes.create", {"config": {"name": "Arrive", "entities": entities}})
        assert ei.value.code == "validation_failed", entities
        assert "entrance" in ei.value.message
    await d.dispatch("scenes.create", {"config": {"name": "Leave", "entities": {GARAGE: "closed", RELAY: "off", BLIND: "open"}}})


async def test_a_household_scene_that_opens_an_entrance_is_not_activated(core: HomeAssistant, tmp_path: Path) -> None:
    """A scene is checked when it runs (AgDR-0012), so the owner's own "Arrive home" is not the way round."""
    await _home(core)
    (tmp_path / "scenes.yaml").write_text(
        "- id: 'arrive'\n  name: Arrive\n  entities:\n    cover.garage_door: open\n"
        "- id: 'leave'\n  name: Leave\n  entities:\n    cover.garage_door: closed\n"
    )
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()
    for scene in ("scene.arrive", "scene.leave"):
        async_expose_entity(core, "conversation", scene, True)
    d = build_dispatcher(core)
    rows = {r["entity_id"]: r for r in await d.dispatch("scenes.list", {})}
    assert rows["scene.arrive"]["can_run"] is False
    assert rows["scene.leave"]["can_run"] is True
    _calls(core, "cover", "open_cover", "close_cover")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.arrive"})
    assert "entrance" in ei.value.message.lower() or ENTRANCE_RULE in ei.value.message


# --------------------------------------------------------------------------- review of 3a01d37


async def test_a_name_makes_a_class_less_cover_or_a_button_an_entrance(core: HomeAssistant) -> None:
    await _home(core)
    _share(core, "cover.garage", "closed")
    _share(core, "button.gate_open", "unknown")
    _share(core, "input_button.open_the_garage", "unknown")
    _share(core, "button.doorbell_restart", "unknown")
    found = entrance_ids(core)
    assert {"cover.garage", "button.gate_open", "input_button.open_the_garage"} <= found
    assert "button.doorbell_restart" not in found
    presses = _calls(core, "button", "press")
    with pytest.raises(RpcError):
        await _call(core, "button", "press", ["button.gate_open"])
    assert not presses["press"]


async def test_a_group_chain_too_deep_to_walk_counts_as_an_entrance(core: HomeAssistant) -> None:
    """Home Assistant follows a chain the walk gives up on, so g10 -> ... -> garage must not be a way round."""
    await _home(core)
    _share(core, "cover.g0", "closed", entity_id=[GARAGE])
    for i in range(1, 11):
        _share(core, f"cover.g{i}", "closed", entity_id=[f"cover.g{i - 1}"])
    assert "cover.g10" in entrance_ids(core)
    res = await _validate(core, "automation", {"action": "cover.open_cover", "target": {"entity_id": "cover.g10"}})
    assert res["ok"] is False and "entrance" in res["error"]
    # Control: the same chain in a home with no entrance is not one.
    core.states.async_remove(GARAGE)
    core.states.async_remove(RELAY)
    core.states.async_set("cover.g0", "closed", {"entity_id": [BLIND]})
    assert entrance_ids(core) == frozenset()


async def test_a_form_may_not_name_an_entrance(core: HomeAssistant) -> None:
    """A group or wrapper over one is a new id an open could already have been authored against."""
    await _home(core)
    for value in ({"name": "Later", "entities": [BLIND, GARAGE]}, {"name": "x", "state": "{{ is_state('cover.garage_door', 'open') }}"}):
        with pytest.raises(RpcError) as ei:
            await flows.check_flow_input(core, value)
        assert "is an entrance" in ei.value.message
    await flows.check_flow_input(core, {"name": "Blinds", "entities": [BLIND]})


async def test_an_entrance_relay_is_not_wrapped(core: HomeAssistant) -> None:
    from .test_switch_as_x import _source

    await _home(core)
    relay = await _source(core, "gate_opener")
    plain = await _source(core, "hall_fan")
    with pytest.raises(RpcError) as ei:
        flows.check_switch_as_x_source(core, "switch_as_x", {"entity_id": relay, "target_domain": "light"})
    assert ei.value.code == "method_not_allowed"
    flows.check_switch_as_x_source(core, "switch_as_x", {"entity_id": plain, "target_domain": "light"})


async def test_a_wrapper_an_entrance_rests_on_is_not_deleted(core: HomeAssistant) -> None:
    """Deleting it would unhide the relay, share it, and leave a switch whose name says nothing."""
    from .test_switch_as_x import _source, _wrap, _wrapper

    await _home(core)
    relay = await _source(core, "relay_1")
    entry = await _wrap(core, relay, "cover")
    er.async_get(core).async_update_entity(_wrapper(core, entry), device_class="garage")
    assert relay in entrance_ids(core)
    other = await _wrap(core, await _source(core, "relay_2"), "cover")
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.delete", {"id": entry.entry_id})
    assert ei.value.code == "method_not_allowed" and "entrance" in ei.value.message
    await d.dispatch("helpers.delete", {"id": other.entry_id})


async def test_a_relay_behind_a_template_cover_is_an_entrance(core: HomeAssistant) -> None:
    await _home(core)
    _share(core, "switch.relay_7", "off")
    _share(core, "switch.relay_8", "off")
    entry = MockConfigEntry(
        domain="template",
        options={
            "template_type": "cover",
            "name": "Big door",
            "device_class": "garage",
            "open_cover": [{"action": "switch.turn_on", "target": {"entity_id": "switch.relay_7"}}],
            "close_cover": [{"action": "switch.turn_on", "target": {"entity_id": "switch.relay_7"}}],
            "state": "{{ is_state('switch.relay_8', 'on') }}",
        },
    )
    entry.add_to_hass(core)
    cover = er.async_get(core).async_get_or_create("cover", "template", "big", suggested_object_id="big_door", config_entry=entry)
    _share(core, cover.entity_id, "closed", device_class="garage")
    found = entrance_ids(core)
    assert "switch.relay_7" in found
    # A reading template names what the helper reads, not what it moves.
    assert "switch.relay_8" not in found


async def _scripts(core: HomeAssistant, tmp_path: Path, body: str) -> None:
    (tmp_path / "scripts.yaml").write_text(body)
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    for state in core.states.async_all("script"):
        async_expose_entity(core, "conversation", state.entity_id, True)


HOUSEHOLD = (
    "open_garage:\n  sequence:\n    - delay: 1\n    - action: cover.open_cover\n      target:\n        entity_id: cover.garage_door\n"
    "close_garage:\n  sequence:\n    - action: cover.close_cover\n      target:\n        entity_id: cover.garage_door\n"
    "arrive:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: script.open_garage\n"
    "arrive_scene:\n  sequence:\n    - scene: scene.arrive\n"
    "ring:\n  sequence:\n    - action: homeassistant.turn_on\n      target:\n        entity_id: cover.garage_door\n    - action: shell_command.x\n"
)


async def test_a_household_script_that_opens_an_entrance_does_not_run(core: HomeAssistant, tmp_path: Path) -> None:
    """The council's R3 over AgDR-0005's licence: no model opens an entrance, by the household's script either."""
    await _home(core)
    (tmp_path / "scenes.yaml").write_text("- id: 'arrive'\n  name: Arrive\n  entities:\n    cover.garage_door: open\n")
    await core.services.async_call("scene", "reload", blocking=True)
    await _scripts(core, tmp_path, HOUSEHOLD)
    _calls(core, "cover", "open_cover", "close_cover")
    d = build_dispatcher(core)
    rows = {r["entity_id"]: r for r in await d.dispatch("scripts.list", {})}
    for refused in ("script.open_garage", "script.arrive", "script.arrive_scene", "script.ring"):
        assert rows[refused]["can_run"] is False, refused
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scripts.run", {"entity_id": refused})
        assert ei.value.code == "method_not_allowed" and "entrance" in ei.value.message, refused
        # Only the entrance rule: the `shell_command` in `ring` is still the household's own business.
        assert "shell_command" not in ei.value.message
    assert rows["script.close_garage"]["can_run"] is True
    await d.dispatch("scripts.run", {"entity_id": "script.close_garage"})


async def test_a_group_holding_such_a_script_is_not_switched_on(core: HomeAssistant, tmp_path: Path) -> None:
    await _home(core)
    await _scripts(core, tmp_path, HOUSEHOLD)
    _share(core, "group.evening", "off", entity_id=["script.open_garage"])
    _share(core, "group.night", "off", entity_id=["script.close_garage"])
    _calls(core, "homeassistant", "turn_on")
    with pytest.raises(RpcError) as ei:
        await _call(core, "homeassistant", "turn_on", ["group.evening"])
    # Since security's third round the group is itself an entrance (it holds a script that opens one).
    assert "entrance" in ei.value.message
    await _call(core, "homeassistant", "turn_on", ["group.night"])


# --------------------------------------------------------------------------- code review of 3a01d37


@pytest.mark.parametrize(
    ("name", "entrance"),
    [
        ("Garage", True),
        ("Gate opener", True),
        ("Front door opener", True),
        ("Roller door", True),
        ("بوابه الحديقة", True),
        ("جراج", True),
        ("قراج البيت", True),
        ("الكـــراج", True),  # tatweel
        ("Garage light", False),
        ("Garage lights", False),
        ("Gate lamp", False),
        ("Garage fan", False),
        ("Garage socket", False),
        ("Garage plug", False),
        ("إنارة الكراج", False),
        ("ضوء البوابة", False),
        ("نور الجراج", False),
        ("لمبة الكراج", False),
        ("مروحة الكراج", False),
        ("فيش الكراج", False),
        ("أباجورة البوابة", False),
        ("والإنارة في الكراج", False),
        ("بوابة المنورة", True),  # a whole word only: نور inside another word does not clear it
        ("كراج النوري", True),
        ("بوابة منور", True),  # ends in نور, but is another word
        # The head noun decides (security's third round): Arabic first, English last.
        ("بوابة النور", True),
        ("بوابة حي النور", True),
        ("كراج نور", True),
        ("بوابه النور الرئيسيه", True),
        # A trailing "switch" or number is not the head noun: these are lights, and a person must be able
        # to switch them on from the app (B2).
        ("Garage light switch", False),
        ("Front gate light switch 2", False),
        ("Garage lights 2", False),
        # Opener, motor or relay make it an entrance whatever follows.
        ("Garage door opener (light)", True),
        ("Gate opener light", True),
        ("Garage motor lamp", True),
        ("Garage door lights relay", True),
        ("Light garage", True),
        ("Gateway", False),
        ("Front door", False),
    ],
)
def test_the_name_rule(name: str, entrance: bool) -> None:
    from custom_components.hearth_ai.handlers.entrances import named_like_entrance

    assert named_like_entrance(name) is entrance


async def test_a_rename_in_home_assistant_clears_a_guess(core: HomeAssistant) -> None:
    """Only the name the household sees counts, so renaming it is how a wrong guess is undone."""
    await _home(core)
    ent_reg = er.async_get(core)
    entry = ent_reg.async_get_or_create("switch", "test", "r9", suggested_object_id="garage_relay", original_name="Garage relay")
    _share(core, entry.entity_id, "off", friendly_name="Garage relay")
    assert entry.entity_id in entrance_ids(core)
    ent_reg.async_update_entity(entry.entity_id, name="Workshop")
    _share(core, entry.entity_id, "off", friendly_name="Workshop")
    # The entity id still says garage, and the integration's own name still says garage relay.
    assert entry.entity_id not in entrance_ids(core)


async def test_a_switch_as_x_pair_is_one_entrance_both_ways(core: HomeAssistant) -> None:
    from .test_switch_as_x import _source, _wrap, _wrapper

    await _home(core)
    # A relay whose own name says gate makes its wrapper one ...
    named = await _source(core, "gate_opener")
    named_wrapper = _wrapper(core, await _wrap(core, named, "cover"))
    # ... and a garage-class wrapper makes its relay one, which an authored config could switch on.
    plain = await _source(core, "relay_5")
    classed = await _wrap(core, plain, "cover")
    er.async_get(core).async_update_entity(_wrapper(core, classed), device_class="garage")
    found = entrance_ids(core)
    assert named_wrapper in found
    assert plain in found
    res = await _validate(core, "automation", {"action": "switch.turn_on", "target": {"entity_id": plain}})
    assert res["ok"] is False and "entrance" in res["error"]
    # Control: an unrelated wrapper pair is not one.
    other = await _source(core, "relay_6")
    other_wrapper = _wrapper(core, await _wrap(core, other, "light"))
    assert other not in entrance_ids(core) and other_wrapper not in entrance_ids(core)


async def test_a_labelled_scene_script_is_checked_against_the_entrances(core: HomeAssistant, tmp_path: Path) -> None:
    """`script_run_violations` itself, so dropping `entrances=` there is caught even though `scripts.run` asks twice."""
    from custom_components.hearth_ai.handlers.scripts import script_run_violations

    await _home(core)
    await _scripts(core, tmp_path, HOUSEHOLD)
    await hl.async_mark_hearth_scene(core, "script.open_garage", True)
    await hl.async_mark_hearth_scene(core, "script.close_garage", True)
    assert any("entrance" in v for v in await script_run_violations(core, "script.open_garage"))
    assert await script_run_violations(core, "script.close_garage") == []


async def test_a_scene_with_no_stored_config_that_names_an_entrance_is_not_activated(core: HomeAssistant) -> None:
    """Its membership names the garage but not the state it sets, so which way it moves cannot be read."""
    await _home(core)
    _share(core, "scene.cloud_arrive", "unknown", entity_id=[GARAGE, BLIND])
    _share(core, "scene.cloud_blinds", "unknown", entity_id=[BLIND])
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.cloud_arrive"})
    assert "sets an entrance and Hearth cannot read which way" in ei.value.message
    _calls(core, "scene", "turn_on")
    await d.dispatch("scenes.activate", {"entity_id": "scene.cloud_blinds"})


async def test_the_entrance_label_guard_says_what_the_label_is(core: HomeAssistant) -> None:
    await _home(core)
    with pytest.raises(RpcError) as ei:
        await build_dispatcher(core).dispatch("labels.delete", {"label_id": hl.label_id_for(core, "entrance")})
    assert "marks an entrance that no assistant may open" in ei.value.message


async def test_a_reclassified_cover_or_a_renamed_relay_tells_the_hub(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    sent: list[int] = []

    async def send() -> None:
        sent.append(1)

    watcher = hl.RegistryWatcher(core, send)
    watcher.start()
    ent_reg = er.async_get(core)
    cover = ent_reg.async_get_or_create("cover", "test", "c1", suggested_object_id="big")
    lamp = ent_reg.async_get_or_create("light", "test", "l1", suggested_object_id="lamp")
    await core.async_block_till_done()
    assert watcher._flush is None
    ent_reg.async_update_entity(lamp.entity_id, name="Desk")
    await core.async_block_till_done()
    assert watcher._flush is None  # a light's name decides nothing
    ent_reg.async_update_entity(cover.entity_id, device_class="garage")
    await core.async_block_till_done()
    assert watcher._flush is not None
    watcher.stop()


# --------------------------------------------------------------------------- security re-review of e55c05e


async def test_a_household_script_may_not_reach_an_entrance_through_a_fan_out_target(core: HomeAssistant, tmp_path: Path) -> None:
    """F1: the general fan-out refusal is not part of the entrance-only check, so the entrance rule has its own."""
    await _home(core)
    await _scripts(
        core,
        tmp_path,
        "by_area:\n  sequence:\n    - action: homeassistant.turn_on\n      target:\n        area_id: garage\n"
        "by_label:\n  sequence:\n    - action: homeassistant.toggle\n      target:\n        label_id: outside\n"
        "off_by_area:\n  sequence:\n    - action: homeassistant.turn_off\n      target:\n        area_id: garage\n",
    )
    d = build_dispatcher(core)
    _calls(core, "homeassistant", "turn_on", "toggle", "turn_off")
    for refused in ("script.by_area", "script.by_label"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scripts.run", {"entity_id": refused})
        assert "home with an entrance" in ei.value.message, refused
    # Control: switching everything in the garage off only closes.
    await d.dispatch("scripts.run", {"entity_id": "script.off_by_area"})


async def test_a_household_script_is_followed_however_deep_and_refused_where_it_cannot_be(core: HomeAssistant, tmp_path: Path) -> None:
    """F2: three hops down still opens the garage, and a templated start cannot be followed at all."""
    await _home(core)
    await _scripts(
        core,
        tmp_path,
        HOUSEHOLD
        + "hop1:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: script.hop2\n"
        "hop2:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: script.arrive\n"
        "templated:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: \"{{ 'script.open_' ~ 'garage' }}\"\n"
        "ghost:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: script.not_there\n"
        "stray:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: switch.hall_lamp\n"
        "generic_ghost:\n  sequence:\n    - action: homeassistant.turn_on\n      target:\n        entity_id: script.not_there\n"
        "tidy:\n  sequence:\n    - action: script.turn_on\n      target:\n        entity_id: script.close_garage\n",
    )
    _calls(core, "cover", "open_cover", "close_cover")
    d = build_dispatcher(core)
    # `generic_ghost` starts an unreadable script through `homeassistant.turn_on`: only following it as a
    # start can refuse it, since the script it names is no entrance's id (security's third round).
    for refused in ("script.hop1", "script.templated", "script.ghost", "script.stray", "script.generic_ghost"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scripts.run", {"entity_id": refused})
        assert "entrance" in ei.value.message, refused
    # Only the start rule can say this one: a named id, not a template and not a fan-out target.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.stray"})
    assert "not one script Hearth can follow" in ei.value.message
    await d.dispatch("scripts.run", {"entity_id": "script.tidy"})


async def test_a_helper_that_is_an_entrance_keeps_its_name(core: HomeAssistant) -> None:
    """F3: renaming "Garage opener" to "Workshop" would make it pressable, for a collection or a config-entry helper."""
    assert await async_setup_component(core, "input_button", {})
    await _home(core)
    d = build_dispatcher(core)
    created = await d.dispatch("helpers.create", {"domain": "input_button", "config": {"name": "Garage opener"}})
    button = created["id"]
    other = (await d.dispatch("helpers.create", {"domain": "input_button", "config": {"name": "Doorbell test"}}))["id"]
    async_expose_entity(core, "conversation", button, True)
    assert button in entrance_ids(core)
    for method, params in (("helpers.rename", {"id": button, "name": "Workshop"}), ("helpers.update", {"id": button, "config": {"name": "Workshop"}})):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed" and "entrance" in ei.value.message, method
    await d.dispatch("helpers.rename", {"id": other, "name": "Chime test"})
    # A config-entry helper: a template cover of class garage.
    entry = MockConfigEntry(domain="template", title="Big door", options={"template_type": "cover", "name": "Big door"})
    entry.add_to_hass(core)
    cover = er.async_get(core).async_get_or_create("cover", "template", "big2", suggested_object_id="big_door_2", config_entry=entry)
    _share(core, cover.entity_id, "closed", device_class="garage")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.rename", {"id": entry.entry_id, "name": "Shed"})
    assert ei.value.code == "method_not_allowed"


async def test_a_template_helper_that_drives_an_entrance_relay_is_one(core: HomeAssistant) -> None:
    """F4: the relay is the entrance, so a template switch whose action switches it on is one too."""
    await _home(core)
    entry = MockConfigEntry(
        domain="template",
        options={"template_type": "switch", "name": "Opener 2", "turn_on": [{"action": "switch.turn_on", "target": {"entity_id": RELAY}}], "turn_off": []},
    )
    entry.add_to_hass(core)
    wrapper = er.async_get(core).async_get_or_create("switch", "template", "op2", suggested_object_id="opener_2", config_entry=entry)
    _share(core, wrapper.entity_id, "off", friendly_name="Opener 2")
    other = MockConfigEntry(domain="template", options={"template_type": "switch", "name": "Lamp 2", "turn_on": [{"action": "switch.turn_on", "target": {"entity_id": LAMP}}]})
    other.add_to_hass(core)
    lamp_wrapper = er.async_get(core).async_get_or_create("switch", "template", "lp2", suggested_object_id="lamp_2", config_entry=other)
    _share(core, lamp_wrapper.entity_id, "off", friendly_name="Lamp 2")
    found = entrance_ids(core)
    assert wrapper.entity_id in found
    assert lamp_wrapper.entity_id not in found


# --------------------------------------------------------------------------- code re-review of e55c05e


async def test_a_renamed_relay_tells_the_hub(core: HomeAssistant) -> None:
    """S1: renaming is how a wrong guess is cleared, so voice must hear of it at once."""
    await hl.async_ensure_labels(core)

    async def send() -> None:
        return None

    watcher = hl.RegistryWatcher(core, send)
    watcher.start()
    relay = er.async_get(core).async_get_or_create("switch", "test", "rr", suggested_object_id="garage_relay")
    await core.async_block_till_done()
    assert watcher._flush is None
    er.async_get(core).async_update_entity(relay.entity_id, name="Workshop")
    await core.async_block_till_done()
    assert watcher._flush is not None
    watcher.stop()


async def test_every_scene_and_script_path_tags_the_entrance_refusal(core: HomeAssistant, tmp_path: Path) -> None:
    """N2: the app says its own sentence only if the box tags the refusal, on activate, a scene-script run and a save."""
    from custom_components.hearth_ai.policy import ENTRANCE_REFUSAL

    await _home(core)
    (tmp_path / "scenes.yaml").write_text(
        "- id: 'arrive'\n  name: Arrive\n  entities:\n    cover.garage_door: open\n"
        "- id: 'both'\n  name: Both\n  entities:\n    cover.garage_door: open\n    lock.front_door: unlocked\n"
    )
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()
    for scene in ("scene.arrive", "scene.both"):
        async_expose_entity(core, "conversation", scene, True)
    await _scripts(core, tmp_path, HOUSEHOLD)
    await hl.async_mark_hearth_scene(core, "script.open_garage", True)
    d = build_dispatcher(core)

    for method, params in (("scenes.activate", {"entity_id": "scene.arrive"}), ("scripts.run", {"entity_id": "script.open_garage"})):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.data == dict(ENTRANCE_REFUSAL), method
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": {"name": "Arrive 2", "entities": {GARAGE: "open"}}})
    assert ei.value.code == "validation_failed" and ei.value.data == dict(ENTRANCE_REFUSAL)
    # Any other refusal wins, in its own words and untagged: a scene that also sets a lock is refused as that.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.both"})
    assert ei.value.data is None


# --------------------------------------------------------------------------- security's third round


async def test_a_script_that_opens_an_entrance_is_one(core: HomeAssistant, tmp_path: Path) -> None:
    """A: then every route to it is an entrance's id, however it is reached."""
    await _home(core)
    await _scripts(
        core,
        tmp_path,
        HOUSEHOLD + "arrive_home:\n  sequence:\n    - action: homeassistant.turn_on\n      target:\n        entity_id: script.open_garage\n",
    )
    _share(core, "group.evening", "off", entity_id=["script.open_garage"])
    found = entrance_ids(core)
    assert {"script.open_garage", "script.arrive", "script.arrive_home", "group.evening"} <= found
    assert "script.close_garage" not in found
    d = build_dispatcher(core)
    _calls(core, "homeassistant", "turn_on")
    _calls(core, "cover", "open_cover", "close_cover")
    # A household script that runs `homeassistant.turn_on` on the garage script.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.arrive_home"})
    assert "entrance" in ei.value.message
    # Authored, the same: the garage script is an entrance's id under another domain's service.
    res = await _validate(core, "automation", {"action": "homeassistant.turn_on", "target": {"entity_id": "script.open_garage"}})
    assert res["ok"] is False and "entrance" in res["error"]


async def test_a_template_switch_over_the_garage_script_is_an_entrance(core: HomeAssistant, tmp_path: Path) -> None:
    await _home(core)
    await _scripts(core, tmp_path, HOUSEHOLD)
    entry = MockConfigEntry(
        domain="template",
        options={"template_type": "switch", "name": "Opener 3", "turn_on": [{"action": "script.turn_on", "target": {"entity_id": "script.open_garage"}}]},
    )
    entry.add_to_hass(core)
    wrapper = er.async_get(core).async_get_or_create("switch", "template", "op3", suggested_object_id="opener_3", config_entry=entry)
    _share(core, wrapper.entity_id, "off", friendly_name="Opener 3")
    assert wrapper.entity_id in entrance_ids(core)
    _calls(core, "switch", "turn_on")
    with pytest.raises(RpcError):
        await _call(core, "switch", "turn_on", [wrapper.entity_id])
    # And the form that would make one is refused for naming the garage script.
    with pytest.raises(RpcError) as ei:
        await flows.check_flow_input(core, {"name": "Opener 4", "turn_on": [{"action": "script.turn_on", "target": {"entity_id": "script.open_garage"}}]})
    assert "is an entrance" in ei.value.message


async def test_a_collection_helper_is_found_by_its_unique_id(core: HomeAssistant) -> None:
    """Its entity id comes from the name it was created with, not from the item id the rename is given."""
    assert await async_setup_component(core, "input_button", {})
    await _home(core)
    d = build_dispatcher(core)
    created = await d.dispatch("helpers.create", {"domain": "input_button", "config": {"name": "Gate opener"}})
    item_id = er.async_get(core).async_get(created["id"]).unique_id
    # The owner gave it another entity id in Home Assistant, so `<domain>.<item id>` names nothing now.
    entity_id = "input_button.front_gate"
    er.async_get(core).async_update_entity(created["id"], new_entity_id=entity_id)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", entity_id, True)
    assert entity_id in entrance_ids(core) and f"input_button.{item_id}" not in entrance_ids(core)
    with pytest.raises(RpcError):
        await d.dispatch("helpers.rename", {"id": f"input_button.{item_id}", "name": "Workshop"})


async def test_a_renamed_device_tells_the_hub(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)

    async def send() -> None:
        return None

    watcher = hl.RegistryWatcher(core, send)
    watcher.start()
    config = MockConfigEntry(domain="test")
    config.add_to_hass(core)
    device = dr.async_get(core).async_get_or_create(config_entry_id=config.entry_id, identifiers={("test", "relay-dev")}, name="Garage relay")
    await core.async_block_till_done()
    watcher._flush = None
    dr.async_get(core).async_update_device(device.id, name_by_user="Workshop")
    await core.async_block_till_done()
    assert watcher._flush is not None
    watcher.stop()


# --------------------------------------------------------------------------- security's fourth round


WRAP = HOUSEHOLD + "wrap:\n  sequence:\n    - action: script.open_garage\n" + "wrap_off:\n  sequence:\n    - action: script.close_garage\n"


async def test_a_script_started_by_its_own_service_is_followed(core: HomeAssistant, tmp_path: Path) -> None:
    """`{action: script.open_garage}` starts the script it is named after, so a wrapper over it is an entrance."""
    await _home(core)
    await _scripts(core, tmp_path, WRAP)
    found = entrance_ids(core)
    assert "script.wrap" in found and "script.wrap_off" not in found
    # Wrapper -> template switch.
    entry = MockConfigEntry(domain="template", options={"template_type": "switch", "name": "Evening", "turn_on": [{"action": "script.wrap"}]})
    entry.add_to_hass(core)
    evening = er.async_get(core).async_get_or_create("switch", "template", "ev", suggested_object_id="evening", config_entry=entry)
    _share(core, evening.entity_id, "off", friendly_name="Evening")
    # Wrapper -> group.
    _share(core, "group.night", "off", entity_id=["script.wrap"])
    found = entrance_ids(core)
    assert evening.entity_id in found and "group.night" in found
    _calls(core, "switch", "turn_on")
    _calls(core, "homeassistant", "turn_on")
    for domain, target in (("switch", evening.entity_id), ("homeassistant", "group.night")):
        with pytest.raises(RpcError) as ei:
            await _call(core, domain, "turn_on", [target])
        assert "entrance" in ei.value.message, target


async def test_a_template_helper_with_a_templated_action_is_an_entrance(core: HomeAssistant) -> None:
    """What it moves cannot be known before it runs, as for a group the walk cannot finish."""
    await _home(core)
    for i, action in enumerate(
        (
            {"action": "{{ 'cover.open' ~ '_cover' }}", "target": {"entity_id": BLIND}},
            {"action": "cover.open_cover", "target": {"entity_id": "{{ 'cover.garage' ~ '_door' }}"}},
            {"action": "cover.open_cover", "target": "{{ {'entity_id': 'cover.garage_door'} }}"},
        )
    ):
        entry = MockConfigEntry(domain="template", options={"template_type": "switch", "name": f"T{i}", "turn_on": [action]})
        entry.add_to_hass(core)
        ent = er.async_get(core).async_get_or_create("switch", "template", f"t{i}", suggested_object_id=f"t{i}", config_entry=entry)
        _share(core, ent.entity_id, "off", friendly_name=f"T{i}")
        assert ent.entity_id in entrance_ids(core), action
    plain = MockConfigEntry(domain="template", options={"template_type": "switch", "name": "Plain", "turn_on": [{"action": "light.turn_on", "target": {"entity_id": "light.x"}, "data": {"brightness": "{{ 3 }}"}}]})
    plain.add_to_hass(core)
    ent = er.async_get(core).async_get_or_create("switch", "template", "plain", suggested_object_id="plain_t", config_entry=plain)
    _share(core, ent.entity_id, "off", friendly_name="Plain")
    # A templated value is not a templated target.
    assert ent.entity_id not in entrance_ids(core)


async def test_the_set_is_cached_until_something_that_decides_it_changes(core: HomeAssistant, tmp_path: Path) -> None:
    """Code review of cec104a: cached, and dropped by events, never by a timer."""
    from custom_components.hearth_ai.handlers import entrances as mod

    await _home(core)
    first = entrance_ids(core)
    assert entrance_ids(core) is first  # cached
    # A new label counts at once.
    ent_reg = er.async_get(core)
    lamp = ent_reg.async_get_or_create("light", "test", "porch9", suggested_object_id="porch9")
    _share(core, lamp.entity_id, "off")
    assert lamp.entity_id not in entrance_ids(core)
    ent_reg.async_update_entity(lamp.entity_id, labels={hl.label_id_for(core, "entrance")})
    assert lamp.entity_id in entrance_ids(core)
    # A script reload counts at once.
    cached = entrance_ids(core)
    assert "script.wrap" not in cached
    await _scripts(core, tmp_path, WRAP)
    assert "script.wrap" in entrance_ids(core)
    # An unrelated state change (a sensor reading) keeps the cache.
    # The reload call itself drops it too, before Home Assistant re-adds the script entities.
    entrance_ids(core)
    core.bus.async_fire(EVENT_CALL_SERVICE, {"domain": "script", "service": "reload", "service_data": {}})
    await core.async_block_till_done()
    assert core.data.get(mod._CACHE_KEY) is None
    _share(core, "sensor.temp", "20")  # a new entity does drop it
    cached = entrance_ids(core)
    core.states.async_set("sensor.temp", "21")
    await core.async_block_till_done()
    assert core.data.get(mod._CACHE_KEY) is cached


# --------------------------------------------------------------------------- security's fifth round


# Each shape as `(opening, closing)` built from the garage's registry id `u`, its device `d` and its
# entity id `e`. Home Assistant's own UI writes a device action's `entity_id`, and a target picked in a
# form, as the registry id, so those rows carry one (security's sixth round).
_INDIRECT: dict[str, Any] = {
    "open by area": lambda u, d, e: ({"action": "cover.open_cover", "target": {"area_id": "garage"}}, {"action": "cover.close_cover", "target": {"area_id": "garage"}}),
    "open by device": lambda u, d, e: ({"action": "cover.open_cover", "target": {"device_id": d}}, {"action": "cover.close_cover", "target": {"device_id": d}}),
    "device action by registry id": lambda u, d, e: (
        {"device_id": d, "domain": "cover", "entity_id": u, "type": "open"},
        {"device_id": d, "domain": "cover", "entity_id": u, "type": "close"},
    ),
    "service target by registry id": lambda u, d, e: ({"action": "cover.open_cover", "target": {"entity_id": u}}, {"action": "cover.close_cover", "target": {"entity_id": u}}),
    "generic alias by registry id": lambda u, d, e: ({"action": "homeassistant.turn_on", "target": {"entity_id": [u]}}, {"action": "cover.close_cover", "target": {"entity_id": [u]}}),
    "generic alias by area": lambda u, d, e: ({"action": "homeassistant.turn_on", "target": {"area_id": "garage"}}, {"action": "homeassistant.turn_off", "target": {"area_id": "garage"}}),
    "generic alias by label": lambda u, d, e: ({"action": "homeassistant.turn_on", "target": {"label_id": "outside"}}, {"action": "homeassistant.turn_off", "target": {"label_id": "outside"}}),
    "generic alias by floor": lambda u, d, e: ({"action": "homeassistant.toggle", "target": {"floor_id": "ground"}}, {"action": "homeassistant.turn_off", "target": {"floor_id": "ground"}}),
    "open all": lambda u, d, e: ({"action": "cover.open_cover", "target": {"entity_id": "all"}}, {"action": "cover.close_cover", "target": {"entity_id": "all"}}),
}


@pytest.mark.parametrize("shape", list(_INDIRECT))
async def test_a_template_helper_that_opens_an_entrance_indirectly_is_one(core: HomeAssistant, shape: str) -> None:
    """A template's actions are resolved and judged as a script's config is, so an indirect open makes it an entrance."""
    await _home(core)
    reg = er.async_get(core)
    source = MockConfigEntry(domain="demo")
    source.add_to_hass(core)
    device = dr.async_get(core).async_get_or_create(config_entry_id=source.entry_id, identifiers={("demo", "g2")})
    garage = reg.async_get_or_create("cover", "demo", "g2", suggested_object_id="garage2", device_id=device.id, original_device_class="garage", config_entry=source)
    _share(core, garage.entity_id, "closed", device_class="garage", friendly_name="Back garage")
    opening, closing = _INDIRECT[shape](garage.id, device.id, garage.entity_id)
    for name, action, expected in (("opens", opening, True), ("closes", closing, False)):
        entry = MockConfigEntry(domain="template", options={"template_type": "switch", "name": name, "turn_on": [action]})
        entry.add_to_hass(core)
        ent = reg.async_get_or_create("switch", "template", name, suggested_object_id=f"t_{name}", config_entry=entry)
        _share(core, ent.entity_id, "off", friendly_name=f"T {name}")
        if "registry id" in shape and not expected:
            # A close that names the garage, resolved from its registry id, still makes the helper one by
            # the older rule (a template whose actions name an entrance is one). That fails closed, and the
            # rule needs the resolve to see the name at all, so this row pins the resolve as well.
            expected = True
        assert (ent.entity_id in entrance_ids(core)) is expected, (shape, name, action)


async def test_a_template_helper_with_an_unresolvable_registry_id_is_an_entrance(core: HomeAssistant) -> None:
    """An id that names nothing cannot be checked, so the helper is opaque, as an unwalkable group is."""
    await _home(core)
    entry = MockConfigEntry(domain="template", options={"template_type": "switch", "name": "Ghost", "turn_on": [{"action": "switch.turn_on", "target": {"entity_id": "0123456789abcdef0123456789abcdef"}}]})
    entry.add_to_hass(core)
    ent = er.async_get(core).async_get_or_create("switch", "template", "ghost", suggested_object_id="ghost_t", config_entry=entry)
    _share(core, ent.entity_id, "off", friendly_name="Ghost")
    assert ent.entity_id in entrance_ids(core)
