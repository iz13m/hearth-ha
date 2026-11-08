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
from custom_components.hearth_ai.handlers.remotes import REMOTE_NAMED_COMMAND_PLATFORMS, find_remote_send_violations
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
        problems = find_remote_send_violations(core, _automation(action))
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
    assert find_remote_send_violations(core, _automation({**_send({"entity_id": BROADLINK}), "action": "remote.learn_command"})) == []
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
    assert find_remote_send_violations(core, cfg) == []
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
