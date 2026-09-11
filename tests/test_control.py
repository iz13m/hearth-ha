"""Operating the home: devices.call, scenes.activate, scripts.run."""

from __future__ import annotations

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

TEST = "input_boolean.test"


def row_can_run(rows: list[dict], entity_id: str) -> bool:
    return next(r for r in rows if r["entity_id"] == entity_id)["can_run"]


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
    # `scene` is in Home Assistant's DEFAULT_EXPOSED_DOMAINS and `script` is not, so a script has to
    # be exposed deliberately before Hearth will run it — the same bar HA's own Assist applies.
    async_expose_entity(core, "conversation", script["entity_id"], True)
    await d.dispatch("scripts.run", {"entity_id": script["entity_id"]})
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "off"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "light.not_a_script"})
    assert ei.value.code == "invalid_params"


async def test_running_a_routine_requires_exposure(core: HomeAssistant) -> None:
    """
    Exposure gates activating a scene and running a script, exactly as it gates a direct service
    call. Without this, activation was the one way to reach something the owner kept out of Assist.
    """
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "Evening", "entities": {TEST: "on"}}})
    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Nudge", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()

    # A script is not exposed by default, so this also pins that `run_script` refuses one the owner
    # never exposed — today the `routines.run` capability alone let the AI run *any* script in the
    # house, and a script performs whatever its author wrote, locks included.
    assert row_can_run(await d.dispatch("scripts.list", {}), script["entity_id"]) is False

    for method, entity_id in [("scenes.activate", scene["entity_id"]), ("scripts.run", script["entity_id"])]:
        async_expose_entity(core, "conversation", entity_id, False)
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, {"entity_id": entity_id})
        assert ei.value.code == "not_found"
        assert "exposed" in ei.value.message
        async_expose_entity(core, "conversation", entity_id, True)
        await d.dispatch(method, {"entity_id": entity_id})
        await core.async_block_till_done()


async def test_refuses_to_activate_a_scene_that_sets_a_denied_entity(core: HomeAssistant) -> None:
    """
    The hole this closes: the scene policy only ever ran over scenes Hearth *writes*, so a scene the
    owner wrote by hand could set a lock and activating it applied that. Membership is read from the
    scene entity's own `entity_id` attribute, so it works for scenes we cannot edit.
    """
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    async_expose_entity(core, "conversation", "scene.open_the_gate", True)
    d = build_dispatcher(core)

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.open_the_gate"})
    assert ei.value.code == "method_not_allowed"
    assert "lock.gate" in ei.value.message


async def test_scene_and_script_lists_report_whether_they_can_be_run(core: HomeAssistant) -> None:
    """Listed either way — the catalog is also how a scene is found in order to edit it."""
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "Reading", "entities": {TEST: "on"}}})
    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Later", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()

    def row(rows: list[dict], entity_id: str) -> dict:
        return next(r for r in rows if r["entity_id"] == entity_id)

    assert row(await d.dispatch("scenes.list", {}), scene["entity_id"])["can_run"] is True
    # Not exposed by default, unlike a scene.
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is False
    async_expose_entity(core, "conversation", script["entity_id"], True)
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is True

    async_expose_entity(core, "conversation", scene["entity_id"], False)
    async_expose_entity(core, "conversation", script["entity_id"], False)
    scenes = await d.dispatch("scenes.list", {})
    assert row(scenes, scene["entity_id"])["can_run"] is False
    assert row(scenes, scene["entity_id"])["editable"] is True, "an un-exposed scene is still listed and editable"
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is False

    core.states.async_set("scene.side_door", "unknown", {"friendly_name": "Side door", "entity_id": ["lock.side"]})
    async_expose_entity(core, "conversation", "scene.side_door", True)
    assert row(await d.dispatch("scenes.list", {}), "scene.side_door")["can_run"] is False
