"""Entity registry ids in an authored config are resolved before it is checked (#663).

Home Assistant accepts an entity's 32-hex registry id wherever a config names an entity by
`entity_id`, and a registry id carries no domain for a walker to read. Every row here goes through the
real handler, against a real registry, except the two that pin the resolver's own contract.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
import yaml

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_config_flow, mock_integration, mock_platform

from custom_components.hearth_ai.handlers.flows import check_flow_input
from custom_components.hearth_ai.handlers.resolve import resolve_registry_ids
from custom_components.hearth_ai.handlers.scenes import UNREACHABLE_SCENE
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

# A device id is 32 hex too, and lives in another registry. Nothing here may touch it.
DEVICE_ID = "0123456789abcdef0123456789abcdef"
NEVER_EXISTED = "fedcba9876543210fedcba9876543210"


def _registry_id(hass: HomeAssistant, domain: str, object_id: str) -> str:
    entry = er.async_get(hass).async_get_or_create(domain, "hearth_test", f"{domain}_{object_id}", suggested_object_id=object_id)
    assert entry.entity_id == f"{domain}.{object_id}"
    return entry.id


def _deleted_registry_id(hass: HomeAssistant) -> str:
    """A siren's id after the siren is deleted: HA keeps it, and gives it back on restore."""
    reg = er.async_get(hass)
    rid = _registry_id(hass, "siren", "garage")
    reg.async_remove("siren.garage")
    assert reg.entities.get_entry(rid) is None and er.async_resolve_entity_id(reg, rid) is None
    return rid


def _automation(*actions: dict[str, Any], triggers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "alias": "Registry ids",
        "triggers": triggers or [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
        "actions": list(actions),
    }


async def test_a_registry_id_is_checked_as_the_entity_it_names(core: HomeAssistant) -> None:
    """#663's own row: `homeassistant.turn_on` at a siren's registry id. HA would call `siren.turn_on`."""
    siren = _registry_id(core, "siren", "hall")
    light = _registry_id(core, "light", "kitchen")
    d = build_dispatcher(core)
    for method, cfg in (
        ("automations.validate", _automation({"action": "homeassistant.turn_on", "target": {"entity_id": siren}})),
        ("scripts.validate", {"alias": "Siren", "sequence": [{"action": "homeassistant.turn_on", "target": {"entity_id": siren}}]}),
        # The legacy spellings, a list, and a comma-joined string: every `entity_id` the walk reads.
        ("automations.validate", _automation({"action": "homeassistant.turn_on", "data": {"entity_id": siren}})),
        ("automations.validate", _automation({"action": "homeassistant.turn_on", "entity_id": [light, siren]})),
        ("automations.validate", _automation({"action": "homeassistant.turn_on", "target": {"entity_id": f"{light}, {siren}"}})),
    ):
        res = await d.dispatch(method, {"key": "siren", "config": cfg})
        assert res["ok"] is False and res["status"] == "policy", cfg
        assert "siren.hall" in res["error"], res

    # Control: the same row at a light's registry id is allowed.
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "homeassistant.turn_on", "target": {"entity_id": light}})})
    assert res == {"ok": True, "status": "ok"}


async def test_a_registry_id_reaches_the_policy_walker(core: HomeAssistant) -> None:
    """A valve, which only the policy walker refuses ("could open a valve", #527).

    Every siren row above is refused by the reference walker as well, so a resolved copy that never
    reached `find_policy_violations` would pass them all. This row tells the two apart: the reference
    walker allows a valve, so the refusal here can only come from the policy walker reading the copy.
    """
    valve = _registry_id(core, "valve", "main")
    d = build_dispatcher(core)
    for target in ("valve.main", valve):
        for method, cfg in (
            ("automations.validate", _automation({"action": "homeassistant.turn_on", "target": {"entity_id": target}})),
            ("scripts.validate", {"alias": "Valve", "sequence": [{"action": "homeassistant.turn_on", "target": {"entity_id": target}}]}),
        ):
            res = await d.dispatch(method, {"key": "valve", "config": cfg})
            assert res["ok"] is False and res["status"] == "policy", (method, target)
            assert "valve.main could open a valve" in res["error"], res


async def test_a_trigger_on_a_registry_id_is_read_by_the_reference_check(core: HomeAssistant) -> None:
    """A `state` trigger on a lock's registry id is reading a lock (AgDR-0038), and is refused.

    **This row is green on `main` too, and that is the point of keeping it.** HA's own trigger and
    condition validation resolves registry ids (`triggers/state.py:91`, `helpers/condition.py:1979,1990`),
    so the second, validated pass of `_validate` already saw `lock.front_door`; the resolver only makes
    the raw pass refuse it first. Service targets are not resolved by validation, which is why #663
    was open. This pins the half of the defence that is HA's, so a release that stops resolving there
    turns it red here rather than nowhere.
    """
    lock = _registry_id(core, "lock", "front_door")
    light = _registry_id(core, "light", "kitchen")
    d = build_dispatcher(core)
    action = {"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.test"}}
    res = await d.dispatch("automations.validate", {"config": _automation(action, triggers=[{"trigger": "state", "entity_id": lock, "to": "unlocked"}])})
    assert res["ok"] is False and res["status"] == "policy"
    assert "lock.front_door" in res["error"]

    res = await d.dispatch("automations.validate", {"config": _automation(action, triggers=[{"trigger": "state", "entity_id": light, "to": "on"}])})
    assert res == {"ok": True, "status": "ok"}


async def test_a_registry_id_that_does_not_resolve_is_refused(core: HomeAssistant) -> None:
    """Refused, not passed: a deleted siren's id names nothing today and the siren again on restore."""
    d = build_dispatcher(core)
    for rid in (_deleted_registry_id(core), NEVER_EXISTED):
        res = await d.dispatch("automations.validate", {"config": _automation({"action": "homeassistant.turn_on", "target": {"entity_id": rid}})})
        assert res["ok"] is False and res["status"] == "policy", rid
        assert "cannot resolve" in res["error"] and rid not in res["error"]


async def test_a_device_id_is_not_an_entity_position(core: HomeAssistant) -> None:
    """Only `entity_id` is resolved. A 32-hex `device_id` passes untouched, at the resolver and through it."""
    node = {"action": "light.turn_on", "target": {"device_id": DEVICE_ID}}
    resolved, problems = resolve_registry_ids(core, {"actions": [node]})
    assert problems == []
    assert resolved == {"actions": [node]}

    d = build_dispatcher(core)
    res = await d.dispatch("automations.validate", {"config": _automation(node)})
    assert res == {"ok": True, "status": "ok"}


async def test_the_owner_yaml_keeps_the_registry_id(core: HomeAssistant, tmp_path: Path) -> None:
    """The resolved copy is checked; the caller's config is what is written, byte for byte."""
    light = _registry_id(core, "light", "kitchen")
    d = build_dispatcher(core)
    auto = _automation({"action": "light.turn_on", "target": {"entity_id": light}})
    # Compared against a copy taken first: a resolved copy written back into `auto` would change it too.
    expected = copy.deepcopy(auto)
    created = await d.dispatch("automations.create", {"config": auto})
    text = (tmp_path / "automations.yaml").read_text()
    assert light in text and "light.kitchen" not in text
    [written] = yaml.safe_load(text)
    assert written == {**expected, "id": created["id"]}

    script = {"alias": "Kitchen", "sequence": [{"action": "light.turn_on", "target": {"entity_id": [light]}}]}
    expected = copy.deepcopy(script)
    await d.dispatch("scripts.create", {"key": "kitchen", "config": script})
    text = (tmp_path / "scripts.yaml").read_text()
    assert light in text and "light.kitchen" not in text
    assert yaml.safe_load(text) == {"kitchen": expected}


async def test_the_resolver_never_edits_its_input(core: HomeAssistant) -> None:
    """A new structure, so no caller can write the copy by accident through a shared dict."""
    light = _registry_id(core, "light", "kitchen")
    config = {"actions": [{"action": "light.turn_on", "target": {"entity_id": [light]}, "entity_id": light}]}
    before = yaml.safe_dump(config)
    resolved, problems = resolve_registry_ids(core, config)
    assert problems == []
    assert resolved == {"actions": [{"action": "light.turn_on", "target": {"entity_id": ["light.kitchen"]}, "entity_id": "light.kitchen"}]}
    assert yaml.safe_dump(config) == before


async def test_a_started_script_is_resolved_too(core: HomeAssistant, tmp_path: Path) -> None:
    """Both sides of the nested check (#225), now that it reads the resolved copy.

    On `main` a registry id in a `script.turn_on` target was refused as "not a script". It is now
    checked as the script it names, so a clean one is allowed — what its entity id already was. And a
    script whose own body sounds a siren by registry id is refused, one hop removed.
    """
    siren = _registry_id(core, "siren", "hall")
    light = _registry_id(core, "light", "kitchen")
    gone = _deleted_registry_id(core)
    (tmp_path / "scripts.yaml").write_text(
        "evening:\n"
        "  alias: Evening\n"
        "  sequence:\n"
        "  - action: input_boolean.turn_on\n"
        "    target:\n"
        "      entity_id: input_boolean.test\n"
        "alarm:\n"
        "  alias: Alarm\n"
        "  sequence:\n"
        "  - action: homeassistant.turn_on\n"
        "    target:\n"
        f"      entity_id: {siren}\n"
        "pair:\n"
        "  alias: Pair\n"
        "  sequence:\n"
        "  - action: homeassistant.turn_on\n"
        "    target:\n"
        f"      entity_id: '{light}, {siren}'\n"
        "stale:\n"
        "  alias: Stale\n"
        "  sequence:\n"
        "  - action: homeassistant.turn_on\n"
        "    target:\n"
        f"      entity_id: {gone}\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    reg = er.async_get(core)
    evening = reg.async_get(reg.async_get_entity_id("script", "script", "evening")).id
    d = build_dispatcher(core)

    res = await d.dispatch("automations.validate", {"config": _automation({"action": "script.turn_on", "target": {"entity_id": evening}})})
    assert res == {"ok": True, "status": "ok"}

    for target in ("script.alarm", reg.async_get(reg.async_get_entity_id("script", "script", "alarm")).id):
        res = await d.dispatch("automations.validate", {"config": _automation({"action": "script.turn_on", "target": {"entity_id": target}})})
        assert res["ok"] is False and res["status"] == "policy", target
        assert "siren.hall" in res["error"], res

    # Comma-joined, with HA's usual space after the comma. One hop removed there is no validated
    # second pass to split and strip the list, so the resolver's own strip is the only thing that
    # reads the siren's id here.
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "script.turn_on", "target": {"entity_id": "script.pair"}})})
    assert res["ok"] is False and res["status"] == "policy", res
    assert "siren.hall" in res["error"], res

    # A started script holding a deleted siren's id is refused too (`find_nested_script_violations`'s
    # `problems.extend(unresolved)`): the id names nothing today and the siren again once it is restored.
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "script.turn_on", "target": {"entity_id": "script.stale"}})})
    assert res["ok"] is False and res["status"] == "policy", res
    assert "cannot resolve" in res["error"] and "->script.stale" in res["error"], res


async def test_an_activated_scene_is_resolved_too(core: HomeAssistant) -> None:
    """The scene half of the nested check (#126): `main` refused a scene's registry id outright, as not
    "a named scene entity". It is now checked as the scene it names, both ways."""
    gate = _registry_id(core, "scene", "open_the_gate")
    film = _registry_id(core, "scene", "movie_night")
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    core.states.async_set("scene.movie_night", "unknown", {"friendly_name": "Movie night", "entity_id": ["light.hall"]})
    d = build_dispatcher(core)
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "scene.turn_on", "target": {"entity_id": gate}})})
    assert res["ok"] is False and res["status"] == "policy"
    # The scene's lock is refused without being named (#722).
    assert UNREACHABLE_SCENE in res["error"] and "lock.gate" not in res["error"]
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "scene.turn_on", "target": {"entity_id": film}})})
    assert res == {"ok": True, "status": "ok"}


async def test_a_flow_form_value_is_resolved(core: HomeAssistant) -> None:
    """A form field is an authoring surface (AgDR-0038); `entity_id` there resolves as it does in an automation."""
    lock = _registry_id(core, "lock", "front_door")
    light = _registry_id(core, "light", "kitchen")
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"name": "Door", "entity_id": lock})
    assert ei.value.code == "validation_failed" and "lock.front_door" in ei.value.message
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"name": "Gone", "entity_id": NEVER_EXISTED})
    assert "cannot resolve" in ei.value.message
    await check_flow_input(core, {"name": "Kitchen", "entity_id": light})


async def test_a_form_value_is_resolved_under_any_key_but_a_device_id_is_left_alone(core: HomeAssistant) -> None:
    """`EntitySelector` takes a registry id whatever its field is called (`helpers/selector.py:1115-1118`).

    So in a form a 32-hex value is looked up under any key. One that names a device in the device
    registry is the `device_id` control: left alone, since a device selector field holds exactly that.
    Only a value that names neither is refused.
    """
    lock = _registry_id(core, "lock", "front_door")
    entry = MockConfigEntry(domain="hearth_test")
    entry.add_to_hass(core)
    device = dr.async_get(core).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hearth_test", "hub")}).id
    assert len(device) == 32

    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"name": "Door", "source": lock})
    assert ei.value.code == "validation_failed" and "lock.front_door" in ei.value.message
    await check_flow_input(core, {"name": "Hub", "device": device})
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"name": "Gone", "device": NEVER_EXISTED})
    assert "cannot resolve" in ei.value.message and NEVER_EXISTED not in ei.value.message


async def _start_flow(core: HomeAssistant, d: Any, domain: str, schema: vol.Schema) -> str:
    """A throwaway config flow with `schema`, started through the dispatcher; its id."""
    from homeassistant import config_entries

    class FakeFlow(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_user(self, user_input=None):
            if user_input is None:
                return self.async_show_form(step_id="user", data_schema=schema)
            return self.async_create_entry(title="Fake", data=user_input)

    mock_integration(core, MockModule(domain), built_in=False)
    mock_platform(core, f"{domain}.config_flow", None)
    with (
        mock_config_flow(domain, FakeFlow),
        patch("custom_components.hearth_ai.handlers.integrations.async_get_config_flows", return_value={domain}),
    ):
        start = await d.dispatch("integrations.flow_start", {"domain": domain})
    assert start["type"] == "form", start
    return start["flow_id"]


async def test_a_registry_id_in_a_list_or_a_section_of_a_form_is_resolved(core: HomeAssistant) -> None:
    """A multi `EntitySelector` holds a list, and a `section` nests its fields one level down.

    Through `integrations.flow_step`, so Home Assistant runs the real form schema. The allowed row is
    the premise: HA takes a registry id in both positions and keeps it, so the refusals are of input
    HA would really have accepted, and what HA stores is the caller's input, not the resolved copy.
    """
    from homeassistant.data_entry_flow import section
    from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

    lock = _registry_id(core, "lock", "front_door")
    light = _registry_id(core, "light", "kitchen")
    schema = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("sources"): EntitySelector(EntitySelectorConfig(multiple=True)),
            vol.Optional("advanced"): section(vol.Schema({vol.Optional("heater"): EntitySelector()})),
        }
    )
    d = build_dispatcher(core, frozenset({"integrations.manage"}))
    flow_id = await _start_flow(core, d, "demo_selectors", schema)

    for user_input in ({"name": "Listed", "sources": [light, lock]}, {"name": "Sectioned", "advanced": {"heater": lock}}):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": user_input})
        assert ei.value.code == "validation_failed", user_input
        assert "lock.front_door" in ei.value.message, user_input

    # Two equal literals: a copy written back into the caller's dict would change `sent` as well.
    sent = {"name": "Kitchen", "sources": [light], "advanced": {"heater": light}}
    done = await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": sent})
    assert done["type"] == "create_entry"
    [created] = core.config_entries.async_entries("demo_selectors")
    assert dict(created.data) == {"name": "Kitchen", "sources": [light], "advanced": {"heater": light}}


async def test_a_32_hex_api_key_is_refused_as_a_credential_before_it_is_read_as_an_id(core: HomeAssistant) -> None:
    """OpenWeatherMap's form takes a 32-hex `api_key` (`openweathermap/config_flow.py:48`).

    `integrations_flow_step` runs `check_secrets` before `check_flow_input`, and `SECRET_NAME` matches
    `api_key`, so the step is refused as a credential and the resolver never sees it.
    """
    d = build_dispatcher(core, frozenset({"integrations.manage"}))
    flow_id = await _start_flow(core, d, "demo_weather", vol.Schema({vol.Required("api_key"): str}))
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": {"api_key": NEVER_EXISTED}})
    assert ei.value.code == "method_not_allowed"
    assert "credentials" in ei.value.message and "cannot resolve" not in ei.value.message


async def test_a_32_hex_key_under_a_name_the_secret_check_misses_is_refused_as_unresolvable(core: HomeAssistant) -> None:
    """The named cost. `xiaomi_ble` and `bthome` take a 32-hex `bindkey`, `yalexs_ble` a 32-hex `key`,
    `switchbot` a 32-hex `encryption_key`, and `SECRET_NAME` matches none of those names. A lower-case
    one names neither an entity nor a device, and is refused, with a message about registry ids.

    That these keys reach `check_flow_input` at all is a gap in `check_secrets`, not this rule. It is
    reported separately, so this row pins only the refusal and says nothing about other spellings.
    """
    d = build_dispatcher(core, frozenset({"integrations.manage"}))
    flow_id = await _start_flow(core, d, "demo_bindkey", vol.Schema({vol.Required("bindkey"): str}))
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": {"bindkey": NEVER_EXISTED}})
    assert ei.value.code == "validation_failed" and "cannot resolve" in ei.value.message
    # The message reaches the hub and `audit_log.error`, so the key itself must not be in it.
    assert NEVER_EXISTED not in ei.value.message


async def test_an_upper_case_registry_id_is_not_one_home_assistant_accepts(core: HomeAssistant) -> None:
    """Why the resolver does not fold case: HA does not either.

    `cv.entity_id_or_uuid` tries `cv.entity_id`, which lower-cases but needs a dot, then
    `fake_uuid4_hex`, whose `_FAKE_UUID_4_HEX` is `^[0-9a-f]{32}$` with no flag
    (`helpers/config_validation.py:386-392,911`). Registry ids are `uuid4().hex`, lower-case. So an
    upper-case one fails HA's own validation, in a selector and in an automation's target alike. These
    rows pin that, so a release that starts accepting one turns red here and the resolver gets a fold.
    """
    from homeassistant.helpers.selector import EntitySelector

    siren = _registry_id(core, "siren", "hall")
    assert siren == siren.lower()
    with pytest.raises(vol.Invalid):
        cv.entity_id_or_uuid(siren.upper())
    with pytest.raises(vol.Invalid):
        EntitySelector()(siren.upper())
    assert EntitySelector()(siren) == siren

    d = build_dispatcher(core)
    res = await d.dispatch("automations.validate", {"config": _automation({"action": "homeassistant.turn_on", "target": {"entity_id": siren.upper()}})})
    assert res["ok"] is False and res["status"] != "ok", res


async def test_a_labelled_script_is_resolved_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """`scripts.run` checks a script Hearth presents as a scene (`script_run_violations`). It reads the
    resolved copy too, so a siren's registry id is refused there exactly as `siren.hall` is.

    An id that does not resolve is **not** refused at run time: HA's call raises on it
    (`helpers/service.py:305-319`), so it reaches nothing. That differs from authoring on purpose.
    """
    from custom_components.hearth_ai import labels as hl
    from custom_components.hearth_ai.handlers.scripts import script_run_violations

    await hl.async_ensure_labels(core)
    siren = _registry_id(core, "siren", "hall")
    light = _registry_id(core, "light", "kitchen")
    gone = _deleted_registry_id(core)
    # The valve pair is the one the policy walker alone refuses; the siren is refused by both walkers.
    valve = _registry_id(core, "valve", "main")
    rows = {"by_name": "siren.hall", "by_id": siren, "valve_by_name": "valve.main", "valve_by_id": valve, "lamp": light, "stale": gone}
    (tmp_path / "scripts.yaml").write_text(
        "".join(
            f"{key}:\n  alias: {key}\n  sequence:\n  - action: homeassistant.turn_on\n    target:\n      entity_id: {target}\n"
            for key, target in rows.items()
        )
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    d = build_dispatcher(core)
    for key in rows:
        async_expose_entity(core, "conversation", f"script.{key}", True)
        await hl.async_mark_hearth_scene(core, f"script.{key}", True)

    for key in ("by_name", "by_id"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scripts.run", {"entity_id": f"script.{key}"})
        assert ei.value.code == "method_not_allowed", key
        assert "siren.hall" in ei.value.message, key
    for key in ("valve_by_name", "valve_by_id"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("scripts.run", {"entity_id": f"script.{key}"})
        assert ei.value.code == "method_not_allowed", key
        assert "valve.main could open a valve" in ei.value.message, key

    assert (await d.dispatch("scripts.run", {"entity_id": "script.lamp"}))["entity_id"] == "script.lamp"
    assert await script_run_violations(core, "script.stale") == []

