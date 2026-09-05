"""End-to-end handler tests against a real (test) Home Assistant core."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

AUTOMATION = {
    "alias": "Hearth test automation",
    "description": "created by tests",
    "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
    "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.test"}}],
    "mode": "single",
}


async def test_catalog(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    entities = await d.dispatch("entities.list", {"domain": "input_boolean"})
    assert [e["entity_id"] for e in entities] == ["input_boolean.test"]
    state = await d.dispatch("states.get", {"entity_id": "input_boolean.test"})
    assert state["state"] == "off"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.get", {"entity_id": "lock.front_door"})
    assert ei.value.code == "not_found"
    services = await d.dispatch("services.list", {"domain": "input_boolean"})
    assert any(s["service"] == "turn_on" for s in services)
    assert await d.dispatch("areas.list", {}) == []


async def test_automation_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    assert (await d.dispatch("automations.validate", {"config": AUTOMATION}))["ok"] is True
    bad = await d.dispatch("automations.validate", {"config": {"alias": "x", "triggers": "nope"}})
    assert bad["ok"] is False

    created = await d.dispatch("automations.create", {"config": AUTOMATION})
    await core.async_block_till_done()
    auto_id = created["id"]
    assert created["entity_id"] == "automation.hearth_test_automation"
    assert (tmp_path / "automations.yaml").is_file()
    assert core.states.get("automation.hearth_test_automation") is not None

    listed = await d.dispatch("automations.list", {})
    assert [a for a in listed if a["id"] == auto_id][0]["editable"] is True

    got = await d.dispatch("automations.get", {"id": auto_id})
    assert got["config"]["alias"] == "Hearth test automation"
    assert got["config"]["id"] == auto_id

    updated = await d.dispatch("automations.update", {"id": auto_id, "config": {**AUTOMATION, "alias": "Renamed"}})
    await core.async_block_till_done()
    assert updated["id"] == auto_id
    assert (await d.dispatch("automations.get", {"id": auto_id}))["config"]["alias"] == "Renamed"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.update", {"id": "missing", "config": AUTOMATION})
    assert ei.value.code == "not_found"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": {"alias": "broken", "triggers": [{"trigger": "nope"}]}})
    assert ei.value.code == "validation_failed"

    await d.dispatch("automations.delete", {"id": auto_id})
    await core.async_block_till_done()
    with pytest.raises(RpcError):
        await d.dispatch("automations.get", {"id": auto_id})


async def test_scene_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    created = await d.dispatch("scenes.create", {"config": {"name": "Movie night", "entities": {"input_boolean.test": "on"}}})
    await core.async_block_till_done()
    assert created["entity_id"] == "scene.movie_night"
    assert (tmp_path / "scenes.yaml").is_file()
    got = await d.dispatch("scenes.get", {"id": created["id"]})
    assert got["config"]["name"] == "Movie night"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": {"entities": "not-a-dict"}})
    assert ei.value.code == "validation_failed"
    await d.dispatch("scenes.delete", {"id": created["id"]})


async def test_script_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    cfg = {"alias": "Blink", "sequence": [{"action": "input_boolean.toggle", "target": {"entity_id": "input_boolean.test"}}]}
    assert (await d.dispatch("scripts.validate", {"config": cfg}))["ok"] is True
    created = await d.dispatch("scripts.create", {"config": cfg})
    await core.async_block_till_done()
    assert created["id"] == "blink"
    assert created["entity_id"] == "script.blink"
    # second create with the same alias gets a suffixed id
    created2 = await d.dispatch("scripts.create", {"config": cfg})
    await core.async_block_till_done()
    assert created2["id"] == "blink_2"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.get", {"id": "Not A Slug"})
    assert ei.value.code == "invalid_params"
    await d.dispatch("scripts.delete", {"id": "blink"})
    await d.dispatch("scripts.delete", {"id": "blink_2"})
    assert (await d.dispatch("scripts.list", {})) == []


async def test_no_service_calls_leak(core: HomeAssistant) -> None:
    """Even a read-only catalog call must never invoke a service."""
    calls = async_mock_service(core, "input_boolean", "turn_on")
    d = build_dispatcher(core)
    await d.dispatch("services.list", {})
    await d.dispatch("entities.list", {})
    assert calls == []


async def test_assist_exposure_is_honoured(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    assert [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "input_boolean"})] == ["input_boolean.test"]
    async_expose_entity(core, "conversation", "input_boolean.test", False)
    assert await d.dispatch("entities.list", {"domain": "input_boolean"}) == []
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.get", {"entity_id": "input_boolean.test"})
    assert ei.value.code == "not_found"
    async_expose_entity(core, "conversation", "input_boolean.test", True)
    assert (await d.dispatch("states.get", {"entity_id": "input_boolean.test"}))["state"] == "off"
