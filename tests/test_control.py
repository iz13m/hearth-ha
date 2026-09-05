"""Operating the home: devices.call, scenes.activate, scripts.run."""

from __future__ import annotations

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

TEST = "input_boolean.test"


async def test_capability_is_required(core: HomeAssistant) -> None:
    """Without devices.control the method is refused before any handler runs."""
    d = build_dispatcher(core, frozenset({"entities.read", "scripts.read"}))
    for method, params in [
        ("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]}),
        ("scenes.activate", {"entity_id": "scene.x"}),
        ("scripts.run", {"entity_id": "script.x"}),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed"
        assert "disabled" in ei.value.message


async def test_turns_a_device_on_and_reports_the_new_state(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    assert core.states.get(TEST).state == "off"
    result = await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    await core.async_block_till_done()
    assert result["called"] == "input_boolean.turn_on"
    assert result["entities"] == [{"entity_id": TEST, "state": "on"}]
    assert core.states.get(TEST).state == "on"


async def test_refuses_blocked_domains_entities_and_routines(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    for params in [
        {"domain": "lock", "service": "unlock", "entity_id": ["lock.front"]},
        {"domain": "input_boolean", "service": "turn_on", "entity_id": ["camera.door"]},
        {"domain": "homeassistant", "service": "restart", "entity_id": [TEST]},
        {"domain": "shell_command", "service": "rm", "entity_id": [TEST]},
        {"domain": "script", "service": "turn_on", "entity_id": ["script.x"]},
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("devices.call", params)
        assert ei.value.code == "method_not_allowed", params
    assert core.states.get(TEST).state == "off"


async def test_requires_a_real_exposed_entity_and_service(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "nope", "entity_id": [TEST]})
    assert ei.value.code == "not_found"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": ["input_boolean.ghost"]})
    assert ei.value.code == "not_found"
    async_expose_entity(core, "conversation", TEST, False)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    assert ei.value.code == "not_found"
    assert "exposed" in ei.value.message
    async_expose_entity(core, "conversation", TEST, True)


async def test_data_cannot_widen_the_target(core: HomeAssistant) -> None:
    """entity_id smuggled through `data` is stripped, not honoured."""
    core.states.async_set("input_boolean.other", "off")
    d = build_dispatcher(core)
    await d.dispatch(
        "devices.call",
        {
            "domain": "input_boolean",
            "service": "turn_on",
            "entity_id": [TEST],
            "data": {"entity_id": ["input_boolean.other"], "area_id": "kitchen"},
        },
    )
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "on"
    assert core.states.get("input_boolean.other").state == "off"


async def test_activate_scene_and_run_script(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "All on", "entities": {TEST: "on"}}})
    await core.async_block_till_done()
    result = await d.dispatch("scenes.activate", {"entity_id": scene["entity_id"]})
    await core.async_block_till_done()
    assert result["entity_id"] == scene["entity_id"]
    assert core.states.get(TEST).state == "on"

    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Turn it off", "sequence": [{"action": "input_boolean.turn_off", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()
    await d.dispatch("scripts.run", {"entity_id": script["entity_id"]})
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "off"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "light.not_a_script"})
    assert ei.value.code == "invalid_params"
