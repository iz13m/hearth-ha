"""A scene or script hidden in the registry is not run, and is not listed as runnable (#644).

#642 ruled "hidden wins" for `states.get` and `devices.call`. `scenes.activate`, `scripts.run` and
the `can_run` flags of `scenes.list` and `scripts.list` asked only whether the owner exposed the
entity, so a hidden scene or script that was still exposed ran, and the app offered its Run button.
They move together: a run refused under a Run button the list still offers is its own bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

TEST = "input_boolean.test"

ROUTINES = [
    ("scenes.activate", "scenes.list", "scene.evening"),
    ("scripts.run", "scripts.list", "script.nudge"),
]


async def _routines(core: HomeAssistant) -> None:
    """A scene and a script, both in the registry, both exposed to Assist."""
    d = build_dispatcher(core)
    await d.dispatch("scenes.create", {"config": {"name": "Evening", "entities": {TEST: "on"}}})
    await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Nudge", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()
    reg = er.async_get(core)
    for _, _, entity_id in ROUTINES:
        # The registry entry is what carries `hidden_by`; without one this file tests nothing.
        assert reg.async_get(entity_id) is not None, entity_id
        async_expose_entity(core, "conversation", entity_id, True)


def _can_run(rows: list[dict], entity_id: str) -> bool:
    return next(r for r in rows if r["entity_id"] == entity_id)["can_run"]


@pytest.mark.parametrize(("run", "listing", "entity_id"), ROUTINES, ids=["scene", "script"])
async def test_a_hidden_routine_is_not_runnable_and_not_run(core: HomeAssistant, run: str, listing: str, entity_id: str) -> None:
    await _routines(core)
    d = build_dispatcher(core)
    reg = er.async_get(core)
    domain = entity_id.split(".", 1)[0]

    # Control first: exposed and not hidden, it is runnable and runs.
    assert _can_run(await d.dispatch(listing, {}), entity_id) is True
    await d.dispatch(run, {"entity_id": entity_id})
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "on"
    await core.services.async_call("input_boolean", "turn_off", {"entity_id": TEST}, blocking=True)

    reg.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.USER)
    await core.async_block_till_done()
    # Still exposed: the hidden clause alone is what refuses it.
    rows = await d.dispatch(listing, {})
    assert _can_run(rows, entity_id) is False
    assert any(r["entity_id"] == entity_id for r in rows), "listed either way, so it can still be edited"

    with pytest.raises(RpcError) as ghost:
        await d.dispatch(run, {"entity_id": f"{domain}.ghost"})
    with pytest.raises(RpcError) as hidden:
        await d.dispatch(run, {"entity_id": entity_id})
    # In the words an unknown id gets, as `devices.call` refuses a hidden entity (#642, #648).
    assert (hidden.value.code, hidden.value.message) == ("not_found", f"unknown entity {entity_id}")
    assert (ghost.value.code, ghost.value.message) == ("not_found", f"unknown entity {domain}.ghost")
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "off", "a refused run must not run"

    # Un-hidden, the same entity is runnable again and runs.
    reg.async_update_entity(entity_id, hidden_by=None)
    await core.async_block_till_done()
    assert _can_run(await d.dispatch(listing, {}), entity_id) is True
    await d.dispatch(run, {"entity_id": entity_id})
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "on"


@pytest.mark.parametrize(("run", "listing", "entity_id"), ROUTINES, ids=["scene", "script"])
async def test_a_group_holding_a_hidden_routine_does_not_run_it(core: HomeAssistant, run: str, listing: str, entity_id: str) -> None:
    """`devices.call` on a group fans out to a scene or script in it (#657), so it asks the same rule."""
    await _routines(core)
    core.states.async_set("group.evening", "off", {"entity_id": [entity_id]})
    async_expose_entity(core, "conversation", "group.evening", True)
    calls = async_mock_service(core, "homeassistant", "turn_on")
    d = build_dispatcher(core)

    def call():
        return d.dispatch("devices.call", {"domain": "homeassistant", "service": "turn_on", "entity_id": ["group.evening"]})

    er.async_get(core).async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.USER)
    with pytest.raises(RpcError) as ei:
        await call()
    assert ei.value.code == "method_not_allowed"
    assert f"holds {entity_id}, which Hearth cannot see" in ei.value.message
    assert calls == []

    er.async_get(core).async_update_entity(entity_id, hidden_by=None)
    await call()
    assert len(calls) == 1


async def _refusal(d, method: str, entity_id: str) -> tuple[str, str]:
    with pytest.raises(RpcError) as ei:
        await d.dispatch(method, {"entity_id": entity_id})
    return ei.value.code, ei.value.message


async def test_a_hidden_scene_that_sets_a_lock_answers_as_an_unknown_id(core: HomeAssistant) -> None:
    """Visibility is asked before contents: the run check's reasons would say the scene exists.

    The scene has a registry entry so it can be hidden, and an `entity_id` attribute naming a lock,
    which `scenes.activate` refuses with reasons. Un-hidden is the control: those reasons are what a
    hidden one used to answer.
    """
    reg = er.async_get(core)
    reg.async_get_or_create("scene", "hearth_test", "open_the_gate", suggested_object_id="open_the_gate")
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    async_expose_entity(core, "conversation", "scene.open_the_gate", True)
    d = build_dispatcher(core)

    code, _ = await _refusal(d, "scenes.activate", "scene.open_the_gate")
    assert code == "method_not_allowed"

    reg.async_update_entity("scene.open_the_gate", hidden_by=er.RegistryEntryHider.USER)
    assert await _refusal(d, "scenes.activate", "scene.open_the_gate") == ("not_found", "unknown entity scene.open_the_gate")
    assert await _refusal(d, "scenes.activate", "scene.ghost") == ("not_found", "unknown entity scene.ghost")


async def test_a_hidden_labelled_script_with_a_refused_step_answers_as_an_unknown_id(core: HomeAssistant, tmp_path: Path) -> None:
    """The same order for a script Hearth presents as a scene, whose steps are checked when it runs."""
    await hl.async_ensure_labels(core)
    sent = async_mock_service(core, "remote", "send_command")
    (tmp_path / "scripts.yaml").write_text(
        "disarm:\n  alias: Disarm\n  sequence:\n  - action: remote.send_command\n    target:\n"
        "      entity_id: remote.living_room\n    data:\n      device: alarm_keyfob\n      command: b64:JgBQAAABKZIUEhQ2\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.disarm", True)
    await hl.async_mark_hearth_scene(core, "script.disarm", True)
    d = build_dispatcher(core)

    code, _ = await _refusal(d, "scripts.run", "script.disarm")
    assert code == "method_not_allowed"

    er.async_get(core).async_update_entity("script.disarm", hidden_by=er.RegistryEntryHider.USER)
    assert await _refusal(d, "scripts.run", "script.disarm") == ("not_found", "unknown entity script.disarm")
    assert await _refusal(d, "scripts.run", "script.ghost") == ("not_found", "unknown entity script.ghost")
    await core.async_block_till_done()
    assert sent == []
