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


async def test_areas_carry_their_floor(core: HomeAssistant) -> None:
    """The layout editor stacks storeys by `level`; a slug alone does not say which is upstairs."""
    from homeassistant.helpers import area_registry as ar, floor_registry as fr

    floors = fr.async_get(core)
    ground = floors.async_create("Ground", level=0)
    upstairs = floors.async_create("Upstairs", level=1)
    unordered = floors.async_create("Garden room")  # no level set
    areas = ar.async_get(core)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    areas.async_create("Studio", floor_id=unordered.floor_id)
    areas.async_create("Shed")  # on no floor

    d = build_dispatcher(core)
    by_name = {a["name"]: a for a in await d.dispatch("areas.list", {})}

    assert by_name["Kitchen"]["floor_level"] == 0
    assert by_name["Kitchen"]["floor_name"] == "Ground"
    assert by_name["Bedroom"]["floor_level"] == 1
    # A floor without a level reports none, rather than a guessed one.
    assert by_name["Studio"]["floor_name"] == "Garden room"
    assert by_name["Studio"]["floor_level"] is None
    assert by_name["Shed"]["floor_id"] is None
    assert by_name["Shed"]["floor_name"] is None
    assert by_name["Shed"]["floor_level"] is None


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


async def test_entities_list_pages_by_keyset(core: HomeAssistant) -> None:
    """#12: the 500 cap used to be the end of the list, with no way to read past it."""
    d = build_dispatcher(core)
    # Lights are exposed to Assist by default, so plain states are enough; no registry entry needed.
    for name in ("delta", "alpha", "charlie", "bravo", "echo"):
        core.states.async_set(f"light.{name}", "off")
    await core.async_block_till_done()

    all_ids = [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "light"})]
    assert all_ids == sorted(all_ids)
    assert len(all_ids) == 5

    # Walk it two at a time, the way the hub does: a short page is the last one.
    seen: list[str] = []
    after: str | None = None
    for _ in range(10):
        params: dict[str, object] = {"domain": "light", "limit": 2}
        if after is not None:
            params["after"] = after
        page = await d.dispatch("entities.list", params)
        seen.extend(e["entity_id"] for e in page)
        if len(page) < 2:
            break
        after = page[-1]["entity_id"]

    # Every entity exactly once, in order, and no page repeated the cursor's own row.
    assert seen == all_ids

    # A cursor past the end is empty, not an error, and one before the start changes nothing.
    assert await d.dispatch("entities.list", {"domain": "light", "after": "zzz.zzz"}) == []
    assert [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "light", "after": "aaa.aaa"})] == all_ids

    with pytest.raises(RpcError) as ei:
        await d.dispatch("entities.list", {"after": 7})
    assert ei.value.code == "invalid_params"


async def test_refuses_a_script_that_turns_on_a_scene_setting_a_lock(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The last way round the promise that a lock is refused to every model (#126).

    `scene.turn_on` is an allowed action, its target is in `scene`, and what the scene *contains* is
    only knowable here — so the hub cannot decide it and `find_policy_violations` never saw it. A
    scene the owner wrote by hand could set a lock, and a script Hearth wrote could activate it.
    Scenes are checked when they *run* (AgDR-0012), but a script is not, which is why this is
    checked when the script is *written*.
    """
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    core.states.async_set("scene.movie_night", "unknown", {"friendly_name": "Movie night", "entity_id": ["light.hall"]})
    d = build_dispatcher(core)

    nested = {"alias": "Let me in", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.open_the_gate"}}]}
    res = await d.dispatch("scripts.validate", {"key": "let_me_in", "config": nested})
    assert res["ok"] is False and res["status"] == "policy"
    assert "lock.gate" in res["error"]
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.create", {"key": "let_me_in", "config": nested})
    assert ei.value.code == "validation_failed"
    assert "scripts.yaml" not in [p.name for p in tmp_path.iterdir()] or not (tmp_path / "scripts.yaml").read_text().strip("{}\n")

    # Nested in a branch, and reached through an automation, which is the default-on capability.
    buried = {
        "alias": "Let me in quietly",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
        "actions": [{"choose": [{"conditions": [], "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.open_the_gate"}}]}]}],
    }
    res = await d.dispatch("automations.validate", {"config": buried})
    assert res["ok"] is False and res["status"] == "policy"

    # A target that names no scene is refused rather than ignored: `all` is ENTITY_MATCH_ALL, which
    # is every scene in the house including the gate, and an area cannot be resolved to scenes here.
    for target in ({"entity_id": "all"}, {"area_id": "hall"}, {"device_id": "abc"}):
        res = await d.dispatch("scripts.validate", {"key": "sweep", "config": {"alias": "Sweep", "sequence": [{"action": "scene.turn_on", "target": target}]}})
        assert res["ok"] is False and res["status"] == "policy", target

    # A scene of lights is the ordinary case and must still be writable.
    fine = {"alias": "Film time", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.movie_night"}}]}
    assert (await d.dispatch("scripts.validate", {"key": "film_time", "config": fine}))["ok"] is True
