"""Which remotes a command may be sent to (#568, Security's F1): the box-only platform allowlist.

Android TV runs a command it has no key for as an `adb shell` line on the TV, and Xiaomi plays an
unnamed payload as a raw IR code, so `remote.send_command` is allowed only to a literal `remote.*` id
whose registry platform is in `REMOTE_NAMED_COMMAND_PLATFORMS`. Every surface is pinned: devices.call,
an authored automation (both of `_validate`'s passes), a `Hearth: scene` script at run time, and a
config-flow form value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.flows import check_flow_input
from custom_components.hearth_ai.handlers.remotes import REMOTE_NAMED_COMMAND_PLATFORMS, find_remote_violations
from custom_components.hearth_ai.handlers.scenes import find_config_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

BROADLINK = "remote.living_room"
ANDROIDTV = "remote.shield"
XIAOMI = "remote.bedroom_ir"
MYSENSORS = "remote.ir_node"
UNREGISTERED = "remote.loose"


def _register(core: HomeAssistant) -> None:
    reg = er.async_get(core)
    for platform, object_id in (("broadlink", "living_room"), ("androidtv", "shield"), ("xiaomi_miio", "bedroom_ir"), ("mysensors", "ir_node")):
        reg.async_get_or_create("remote", platform, f"{platform}-1", suggested_object_id=object_id)
    for eid in (BROADLINK, ANDROIDTV, XIAOMI, MYSENSORS, UNREGISTERED):
        core.states.async_set(eid, "on")
        async_expose_entity(core, "conversation", eid, True)


def _send(target: Any, **extra: Any) -> dict[str, Any]:
    return {"action": "remote.send_command", "target": target, "data": {"device": "television", "command": ["power"], **extra}}


def _automation(action: dict[str, Any]) -> dict[str, Any]:
    return {"alias": "TV", "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}], "actions": [action]}


def test_broadlink_is_the_only_platform_so_far() -> None:
    # Literal: a platform joins only after someone reads its send_command.
    assert REMOTE_NAMED_COMMAND_PLATFORMS == frozenset({"broadlink"})


async def test_devices_call_sends_only_to_a_broadlink_remote(core: HomeAssistant) -> None:
    _register(core)
    sent = async_mock_service(core, "remote", "send_command")
    d = build_dispatcher(core)
    data = {"device": "television", "command": ["power"]}

    await d.dispatch("devices.call", {"domain": "remote", "service": "send_command", "entity_id": [BROADLINK], "data": data})
    await core.async_block_till_done()
    assert [c.data["entity_id"] for c in sent] == [[BROADLINK]]

    for ids, why in (
        ([ANDROIDTV], "belongs to androidtv"),
        ([XIAOMI], "belongs to xiaomi_miio"),
        ([UNREGISTERED], "has no registry entry"),
        ([BROADLINK, ANDROIDTV], "belongs to androidtv"),
    ):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("devices.call", {"domain": "remote", "service": "send_command", "entity_id": ids, "data": data})
        assert ei.value.code == "method_not_allowed", ids
        assert why in ei.value.message, ids
    await core.async_block_till_done()
    assert len(sent) == 1

    # Mixed case reaches the same service, and the same refusal.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "Remote", "service": "Send_Command", "entity_id": [ANDROIDTV], "data": data})
    assert "belongs to androidtv" in ei.value.message

    # On/off/toggle are not limited.
    toggled = async_mock_service(core, "remote", "turn_off")
    await d.dispatch("devices.call", {"domain": "remote", "service": "turn_off", "entity_id": [ANDROIDTV]})
    await core.async_block_till_done()
    assert len(toggled) == 1


async def test_devices_call_teaches_only_a_broadlink_remote(core: HomeAssistant) -> None:
    """MySensors' `async_learn_command` writes each `command` string to the node as `V_IR_RECORD`."""
    _register(core)
    learned = async_mock_service(core, "remote", "learn_command")
    d = build_dispatcher(core)
    data = {"device": "television", "command": ["power"]}

    await d.dispatch("devices.call", {"domain": "remote", "service": "learn_command", "entity_id": [BROADLINK], "data": data})
    for ids, why in (([MYSENSORS], "belongs to mysensors"), ([ANDROIDTV], "belongs to androidtv"), ([UNREGISTERED], "has no registry entry")):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("devices.call", {"domain": "remote", "service": "learn_command", "entity_id": ids, "data": data})
        assert ei.value.code == "method_not_allowed", ids
        assert why in ei.value.message, ids
    await core.async_block_till_done()
    assert [c.data["entity_id"] for c in learned] == [[BROADLINK]]


async def test_an_authored_send_reaches_only_a_broadlink_remote_named_literally(core: HomeAssistant) -> None:
    _register(core)
    assert await find_config_violations(core, _automation(_send({"entity_id": BROADLINK}))) == []
    assert await find_config_violations(core, _automation(_send({"entity_id": [BROADLINK]}))) == []

    def refused(action: dict[str, Any], why: str) -> None:
        problems = find_remote_violations(core, _automation(action))
        assert any(why in p for p in problems), (action, problems)

    refused(_send({"entity_id": ANDROIDTV}), "belongs to androidtv")
    refused(_send({"entity_id": XIAOMI}), "belongs to xiaomi_miio")
    refused(_send({"entity_id": UNREGISTERED}), "has no registry entry")
    refused(_send({"entity_id": f"{BROADLINK}, {ANDROIDTV}"}), "belongs to androidtv")
    refused(_send({"area_id": "living_room"}), "targets by area_id")
    refused(_send({"device_id": "abc"}), "targets by device_id")
    refused(_send({"floor_id": "ground"}), "targets by floor_id")
    refused(_send({"label_id": "tv"}), "targets by label_id")
    refused(_send({"entity_id": "all"}), "all is not a remote Hearth can check")
    refused(_send({"entity_id": "{{ 'remote.living_room' }}"}), "named by a template")
    refused(_send("{{ {'entity_id': 'remote.shield'} }}"), "templated target")
    refused(_send({"entity_id": BROADLINK, "{{ 'area_id' }}": "x"}), "templated target key")
    refused({"action": "remote.send_command", "data": {"entity_id": BROADLINK, "command": ["power"]}}, "names its target in data")
    refused({"action": "remote.send_command", "data": {"command": ["power"]}}, "names no remote")
    refused({"action": "remote.send_command", "entity_id": ANDROIDTV, "data": {"command": ["power"]}}, "belongs to androidtv")
    # Learning is limited the same way.
    assert find_remote_violations(core, _automation({**_send({"entity_id": BROADLINK}), "action": "remote.learn_command"})) == []
    refused({**_send({"entity_id": MYSENSORS}), "action": "remote.learn_command"}, "belongs to mysensors")
    refused({**_send({"area_id": "living_room"}), "action": "remote.learn_command"}, "targets by area_id")
    # Nested, and spelled the legacy way.
    refused({"choose": [{"conditions": [], "sequence": [{"service": "remote.send_command", "target": {"entity_id": ANDROIDTV}, "data": {"command": ["x"]}}]}]}, "belongs to androidtv")


async def test_automations_create_refuses_an_android_tv_send_on_the_box(core: HomeAssistant) -> None:
    """The whole path, including the validated pass, through the dispatcher."""
    _register(core)
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": _automation(_send({"entity_id": ANDROIDTV}))})
    assert "belongs to androidtv" in ei.value.message
    assert (await d.dispatch("automations.validate", {"config": _automation(_send({"entity_id": BROADLINK}))}))["ok"] is True


async def test_a_blueprint_sending_to_android_tv_is_caught_on_the_validated_pass(core: HomeAssistant) -> None:
    """Only `_validate`'s second pass sees a blueprint's body, so this pins that pass."""
    _register(core)
    bp = Path(core.config.path("blueprints/automation/hearth_remote"))
    bp.mkdir(parents=True, exist_ok=True)
    (bp / "p.yaml").write_text(
        "blueprint:\n  name: remote\n  domain: automation\n  input: {}\n"
        "triggers:\n  - trigger: state\n    entity_id: input_boolean.test\n    to: 'on'\n"
        f"actions:\n  - action: remote.send_command\n    target:\n      entity_id: {ANDROIDTV}\n"
        "    data:\n      command: power\n"
    )
    cfg = {"alias": "looks harmless", "use_blueprint": {"path": "hearth_remote/p.yaml", "input": {}}}
    # The raw config names no remote at all.
    assert find_remote_violations(core, cfg) == []
    res = await build_dispatcher(core).dispatch("automations.validate", {"config": cfg})
    assert res["ok"] is False and res["status"] == "policy"
    assert "belongs to androidtv" in res["error"]


async def test_a_hearth_scene_script_sending_to_android_tv_is_refused_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    _register(core)
    await hl.async_ensure_labels(core)
    sent = async_mock_service(core, "remote", "send_command")
    for target, ok in ((ANDROIDTV, False), (BROADLINK, True)):
        (tmp_path / "scripts.yaml").write_text(
            f"tv:\n  alias: TV\n  sequence:\n  - action: remote.send_command\n    target:\n      entity_id: {target}\n"
            "    data:\n      device: television\n      command: power\n"
        )
        await core.services.async_call("script", "reload", blocking=True)
        await core.async_block_till_done()
        async_expose_entity(core, "conversation", "script.tv", True)
        await hl.async_mark_hearth_scene(core, "script.tv", True)
        d = build_dispatcher(core)
        if ok:
            await d.dispatch("scripts.run", {"entity_id": "script.tv"})
        else:
            with pytest.raises(RpcError) as ei:
                await d.dispatch("scripts.run", {"entity_id": "script.tv"})
            assert "belongs to androidtv" in ei.value.message
        await core.async_block_till_done()
    assert [c.data["entity_id"] for c in sent] == [[BROADLINK]]


async def test_a_form_value_sending_to_android_tv_is_refused(core: HomeAssistant) -> None:
    _register(core)
    await check_flow_input(core, {"turn_on": [_send({"entity_id": BROADLINK})]})
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [_send({"entity_id": ANDROIDTV})]})
    assert "belongs to androidtv" in ei.value.message


# --- #765: a remote's activity, and a household script's remote one hop away -------------------

TV = "remote.android_tv"
HARMONY = "remote.harmony_hub"
APPS = ["Netflix", "YouTube"]


def _register_activities(core: HomeAssistant) -> None:
    reg = er.async_get(core)
    reg.async_get_or_create("remote", "androidtv_remote", "atv-1", suggested_object_id="android_tv")
    reg.async_get_or_create("remote", "harmony", "hub-1", suggested_object_id="harmony_hub")
    core.states.async_set(TV, "on", {"activity_list": APPS, "current_activity": "Netflix"})
    core.states.async_set(HARMONY, "on", {})  # lists nothing
    core.states.async_set("light.lamp", "on")
    for eid in (TV, HARMONY, "light.lamp"):
        async_expose_entity(core, "conversation", eid, True)


async def test_devices_call_starts_only_an_activity_the_remote_lists(core: HomeAssistant) -> None:
    """`androidtv_remote` launches whatever `activity` names, a URL included (HA 2026.9.3, `async_turn_on`)."""
    _register_activities(core)
    turned = async_mock_service(core, "remote", "turn_on")
    toggled = async_mock_service(core, "remote", "toggle")
    d = build_dispatcher(core)

    async def call(domain: str, service: str, ids: list[str], data: dict[str, Any] | None = None) -> None:
        await d.dispatch("devices.call", {"domain": domain, "service": service, "entity_id": ids, **({"data": data} if data is not None else {})})

    await call("remote", "turn_on", [TV], {"activity": "Netflix"})
    await call("remote", "turn_on", [TV])  # the control: no activity at all
    await call("remote", "turn_on", [HARMONY])
    for domain, service, ids, activity, why in (
        ("remote", "turn_on", [TV], "https://example.org/phish", "does not list the activity"),
        ("remote", "turn_on", [TV], "com.example.anything", "does not list the activity"),
        ("remote", "turn_on", [TV], "netflix", "does not list the activity"),  # exact, as the integration compares
        ("Remote", "Turn_On", [TV], "https://example.org", "does not list the activity"),
        ("remote", "toggle", [TV], "https://example.org", "does not list the activity"),
        ("remote", "turn_on", [HARMONY], "Netflix", "does not list the activity"),
        ("remote", "turn_on", [TV, HARMONY], "Netflix", f"{HARMONY} does not list"),
        ("homeassistant", "turn_on", [TV], "https://example.org", "does not list the activity"),
        ("homeassistant", "turn_on", ["light.lamp"], "Netflix", "light.lamp is not a remote"),
    ):
        with pytest.raises(RpcError) as ei:
            await call(domain, service, ids, {"activity": activity})
        assert ei.value.code == "method_not_allowed", (service, activity)
        assert why in ei.value.message, (service, activity, ei.value.message)
    await core.async_block_till_done()
    assert [c.data.get("activity") for c in turned] == ["Netflix", None, None]
    assert toggled == []

    # homeassistant.turn_on forwards its data to remote.turn_on, so a listed activity goes through it too.
    await call("homeassistant", "turn_on", [TV], {"activity": "YouTube"})
    await core.async_block_till_done()
    assert [c.data.get("activity") for c in turned][-1] == "YouTube"


async def test_an_authored_activity_must_be_one_the_remote_lists(core: HomeAssistant) -> None:
    _register_activities(core)

    def on(target: Any, activity: Any = "Netflix", action: str = "remote.turn_on", key: str = "data") -> dict[str, Any]:
        return {"action": action, "target": target, key: {"activity": activity}}

    assert await find_config_violations(core, _automation(on({"entity_id": TV}))) == []
    assert await find_config_violations(core, _automation({"action": "remote.turn_on", "target": {"entity_id": HARMONY}})) == []
    assert await find_config_violations(core, _automation(on({"entity_id": TV}, action="homeassistant.turn_on"))) == []

    def refused(action: dict[str, Any], why: str) -> None:
        problems = find_remote_violations(core, _automation(action))
        assert any(why in p for p in problems), (action, problems)

    refused(on({"entity_id": TV}, "https://example.org"), "does not list the activity")
    refused(on({"entity_id": TV}, "https://example.org", action="Remote.Toggle"), "does not list the activity")
    refused(on({"entity_id": TV}, "https://example.org", action="homeassistant.turn_on"), "does not list the activity")
    refused(on({"entity_id": TV}, "https://example.org", key="data_template"), "does not list the activity")
    refused(on({"entity_id": HARMONY}), "does not list the activity")
    refused(on({"area_id": "living_room"}), "targets by area_id")
    refused(on({"label_id": "tv"}), "targets by label_id")
    refused(on({"entity_id": "all"}), "all is not a remote Hearth can check")
    refused(on({"entity_id": "{{ r }}"}), "named by a template")
    refused(on({"entity_id": "light.lamp"}, action="homeassistant.turn_on"), "light.lamp is not a remote")
    refused({"choose": [{"conditions": [], "sequence": [{"service": "remote.turn_on", "target": {"entity_id": TV}, "data": {"activity": "https://x"}}]}]}, "does not list the activity")


async def test_automations_validate_refuses_an_unlisted_activity_on_the_box(core: HomeAssistant) -> None:
    _register_activities(core)
    d = build_dispatcher(core)
    bad = _automation({"action": "remote.turn_on", "target": {"entity_id": TV}, "data": {"activity": "https://example.org"}})
    res = await d.dispatch("automations.validate", {"config": bad})
    assert res["ok"] is False and "does not list the activity" in res["error"]
    good = _automation({"action": "remote.turn_on", "target": {"entity_id": TV}, "data": {"activity": "Netflix"}})
    assert (await d.dispatch("automations.validate", {"config": good}))["ok"] is True


async def test_a_hearth_scene_script_starting_an_unlisted_activity_is_refused_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    _register_activities(core)
    await hl.async_ensure_labels(core)
    turned = async_mock_service(core, "remote", "turn_on")
    for activity, ok in (("https://example.org", False), ("Netflix", True)):
        (tmp_path / "scripts.yaml").write_text(
            f"tv:\n  alias: TV\n  sequence:\n  - action: remote.turn_on\n    target:\n      entity_id: {TV}\n"
            f"    data:\n      activity: '{activity}'\n"
        )
        await core.services.async_call("script", "reload", blocking=True)
        await core.async_block_till_done()
        async_expose_entity(core, "conversation", "script.tv", True)
        await hl.async_mark_hearth_scene(core, "script.tv", True)
        d = build_dispatcher(core)
        if ok:
            await d.dispatch("scripts.run", {"entity_id": "script.tv"})
        else:
            with pytest.raises(RpcError) as ei:
                await d.dispatch("scripts.run", {"entity_id": "script.tv"})
            assert "does not list the activity" in ei.value.message
        await core.async_block_till_done()
    assert [c.data.get("activity") for c in turned] == ["Netflix"]


async def test_a_form_value_starting_an_unlisted_activity_is_refused(core: HomeAssistant) -> None:
    _register_activities(core)
    await check_flow_input(core, {"turn_on": [{"action": "remote.turn_on", "target": {"entity_id": TV}, "data": {"activity": "Netflix"}}]})
    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [{"action": "remote.turn_on", "target": {"entity_id": TV}, "data": {"activity": "https://example.org"}}]})
    assert "does not list the activity" in ei.value.message


async def _household_script(core: HomeAssistant, tmp_path: Path, body: str) -> None:
    """A script the household wrote, which a model-authored automation then starts."""
    (tmp_path / "scripts.yaml").write_text(f"house:\n  alias: House\n  sequence:\n{body}")
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()


def _starts_house(**extra: Any) -> dict[str, Any]:
    return _automation({"action": "script.turn_on", "target": {"entity_id": "script.house"}, **extra})


async def test_a_household_script_may_not_send_to_a_remote_the_caller_chooses(core: HomeAssistant, tmp_path: Path) -> None:
    """One hop away the household's own fixed command could be aimed by a variable at an Android TV."""
    _register(core)
    _register_activities(core)
    templated = "  - action: remote.send_command\n    target:\n      entity_id: '{{ r }}'\n    data:\n      command: power\n"
    await _household_script(core, tmp_path, templated)
    problems = await find_config_violations(core, _starts_house(data={"variables": {"r": ANDROIDTV}}))
    assert any("script.house" in p and "filled in when it runs" in p for p in problems), problems

    # Refused for every way a caller can steer the target or the command, and for learning too.
    for body, why in (
        ("  - action: remote.send_command\n    target: '{{ t }}'\n    data:\n      command: power\n", "templated target"),
        ("  - action: remote.send_command\n    entity_id: '{{ r }}'\n    data:\n      command: power\n", "filled in when it runs"),
        ("  - action: Remote.Send_Command\n    target:\n      entity_id: '{{ r }}'\n    data:\n      command: power\n", "filled in when it runs"),
        ("  - action: remote.send_command\n    target:\n      entity_id: all\n    data:\n      command: power\n", "every remote in the house"),
        ("  - action: remote.send_command\n    target:\n      area_id: living_room\n    data:\n      command: power\n", "targets by area_id"),
        ("  - action: remote.send_command\n    target:\n      label_id: tv\n    data:\n      command: power\n", "targets by label_id"),
        ("  - action: remote.send_command\n    data:\n      entity_id: '{{ r }}'\n      command: power\n", "names its target in data"),
        ("  - action: remote.learn_command\n    target:\n      entity_id: '{{ r }}'\n    data:\n      command: power\n", "filled in when it runs"),
        (f"  - action: remote.learn_command\n    target:\n      entity_id: {MYSENSORS}\n    data:\n      command: '{{{{ name }}}}'\n", "fills in its data"),
        ("  - action: remote.turn_on\n    target:\n      entity_id: '{{ r }}'\n    data:\n      activity: Netflix\n", "filled in when it runs"),
        ("  - choose:\n    - conditions: []\n      sequence:\n      - action: remote.send_command\n        target:\n          entity_id: '{{ r }}'\n        data:\n          command: power\n", "filled in when it runs"),
    ):
        await _household_script(core, tmp_path, body)
        problems = await find_config_violations(core, _starts_house())
        assert any(why in p for p in problems), (body, problems)

    # The household's own fixed command at its own fixed remote is its decision (AgDR-0005), whatever
    # the platform: the allowlist is not applied one hop away. A remote turned on with no activity is
    # not steerable either.
    for body in (
        f"  - action: remote.send_command\n    target:\n      entity_id: {ANDROIDTV}\n    data:\n      command: HOME\n",
        f"  - action: remote.send_command\n    target:\n      entity_id: {BROADLINK}\n    data:\n      device: television\n      command: power\n",
        f"  - action: remote.learn_command\n    target:\n      entity_id: {BROADLINK}\n    data:\n      device: television\n      command: power\n",
        f"  - action: remote.turn_on\n    target:\n      entity_id: {TV}\n    data:\n      activity: Netflix\n",
        "  - action: remote.turn_on\n    target:\n      entity_id: '{{ r }}'\n",
    ):
        await _household_script(core, tmp_path, body)
        assert await find_config_violations(core, _starts_house()) == [], body


_CAPS = frozenset({"devices.control", "routines.run"})


async def _grouped_household_script(core: HomeAssistant, tmp_path: Path, body: str) -> None:
    """An exposed old-style group holding an exposed household script, the shape Security probed."""
    await _household_script(core, tmp_path, body)
    core.states.async_set("group.tv", "off", {"entity_id": ["script.house"]})
    for eid in ("group.tv", "script.house"):
        async_expose_entity(core, "conversation", eid, True)


async def test_a_call_on_a_group_cannot_fill_in_a_household_scripts_variables(core: HomeAssistant, tmp_path: Path) -> None:
    """`homeassistant.turn_on` on a group forwards its `data` to `script.turn_on` as the script's variables.

    Security's probe for #765: the model chose which remote received the household's command, which URL
    an Android TV opened, and which lock a script with a templated target unlocked.
    """
    reg = er.async_get(core)
    reg.async_get_or_create("remote", "androidtv", "a-1", suggested_object_id="shield")
    core.states.async_set("remote.shield", "on")
    core.states.async_set("remote.android_tv", "on", {"activity_list": ["Netflix"]})
    core.states.async_set("lock.front", "locked")
    sent = async_mock_service(core, "remote", "send_command")
    on = async_mock_service(core, "remote", "turn_on")
    unlocked = async_mock_service(core, "lock", "unlock")
    d = build_dispatcher(core, _CAPS)

    async def call(data: dict[str, Any] | None) -> None:
        await d.dispatch("devices.call", {"domain": "homeassistant", "service": "turn_on", "entity_id": ["group.tv"], **({"data": data} if data is not None else {})})

    for body, variables in (
        ("  - action: remote.send_command\n    target:\n      entity_id: '{{ r }}'\n    data:\n      command: power\n", {"r": "remote.shield"}),
        ("  - action: remote.turn_on\n    target:\n      entity_id: remote.android_tv\n    data:\n      activity: '{{ a }}'\n", {"a": "https://evil.example"}),
        ("  - action: lock.unlock\n    target:\n      entity_id: '{{ door }}'\n", {"door": "lock.front"}),
    ):
        await _grouped_household_script(core, tmp_path, body)
        with pytest.raises(RpcError) as ei:
            await call({"variables": variables})
        assert ei.value.code == "method_not_allowed", body
        assert "may not pass variables" in ei.value.message, ei.value.message
        # Any data at all reaches the script the same way, so the group half refuses it without the key.
        with pytest.raises(RpcError) as ei:
            await call({"r": "remote.shield"})
        assert "would pass its data to the script as variables" in ei.value.message, ei.value.message
    await core.async_block_till_done()
    assert (sent, on, unlocked) == ([], [], [])

    # The control: the same group, called with no data, still runs the household script as written.
    await _grouped_household_script(core, tmp_path, "  - action: remote.turn_on\n    target:\n      entity_id: remote.android_tv\n")
    await call(None)
    await call({})
    await core.async_block_till_done()
    assert len(on) == 2


async def test_a_direct_call_may_not_pass_variables(core: HomeAssistant) -> None:
    core.states.async_set("light.lamp", "on")
    async_expose_entity(core, "conversation", "light.lamp", True)
    with pytest.raises(RpcError) as ei:
        await build_dispatcher(core, _CAPS).dispatch("devices.call", {"domain": "light", "service": "turn_on", "entity_id": ["light.lamp"], "data": {"variables": {}}})
    assert "may not pass variables" in ei.value.message
