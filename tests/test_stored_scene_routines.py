"""A stored scene that switches a routine (#713, invariant 1, AgDR-0042).

`automation/reproduce_state.py` maps a scene's `on`/`off` for an `automation.*` key to
`automation.turn_on`/`turn_off`. So a model with the default-on `scenes.write` and
`automations.write` could store `{"automation.lock_doors_at_night": "off"}` and fire it from an
authored `scene.turn_on`, switching a household rule that only `automations.set_enabled` may switch.

Three answers, kept apart on purpose:

- **Writing** such a scene is refused (`find_scene_write_violations`, and `SceneWriteConfig` on the hub).
- **Linking** to such a scene from an authored automation or script is refused
  (`find_nested_scene_violations(..., authoring=True)`), since the household may already have one.
- **Running** it is not refused: the household's own scene is their configuration, and the
  run-time check (`find_scene_run_violations`, and a labelled script's `script_run_violations`) is
  left exactly as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.policy import find_scene_policy_violations, find_scene_write_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"
RULE = "automation.lock_doors_at_night"
STORED = "- id: 'night1'\n  name: Night off\n  entities:\n    automation.lock_doors_at_night: 'off'\n"


def test_the_walkers_agree_on_every_stored_scene_case() -> None:
    """Both verdicts the hub gives each stored scene, held on the box: run-time and write-time."""
    cases = json.loads(SCHEMA.read_text())["stored_scene_cases"]
    assert len(cases) >= 20, "run pnpm --filter @hearth/shared export:jsonschema first"
    assert any(c["write_violations"] and not c["run_violations"] for c in cases), "the corpus must pin a write-only refusal"
    for case in cases:
        assert find_scene_policy_violations(case["config"], frozenset(case.get("entrances") or ())) == case["run_violations"], case["name"]
        assert find_scene_write_violations(case["config"]) == case["write_violations"], case["name"]


async def _install_stored_scene(core: HomeAssistant, tmp_path: Path) -> None:
    """The household's own scene, written to scenes.yaml by hand as the owner would."""
    (tmp_path / "scenes.yaml").write_text(STORED)
    await core.services.async_call("scene", "reload", blocking=True)
    await core.async_block_till_done()
    assert core.states.get("scene.night_off") is not None


async def test_home_assistant_switches_an_automation_off_through_a_stored_scene(core: HomeAssistant, tmp_path: Path) -> None:
    """The vector is real on the pinned release: activating the stored scene calls automation.turn_off."""
    await _install_stored_scene(core, tmp_path)
    core.states.async_set(RULE, "on")
    turned_off = async_mock_service(core, "automation", "turn_off")
    await core.services.async_call("scene", "turn_on", {"entity_id": "scene.night_off"}, blocking=True)
    await core.async_block_till_done()
    assert [c.data.get("entity_id") for c in turned_off] in ([RULE], [[RULE]])


@pytest.mark.parametrize("key", [RULE, "Automation.Morning", "script.open_gate", "scene.gate"])
async def test_a_scene_that_sets_a_routine_cannot_be_written(core: HomeAssistant, tmp_path: Path, key: str) -> None:
    d = build_dispatcher(core)
    config = {"name": "Night off", "entities": {"input_boolean.test": "on", key: "off"}}
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": config})
    assert ei.value.code == "validation_failed" and key in ei.value.message
    assert (tmp_path / "scenes.yaml").read_text().strip() == "[]"

    created = await d.dispatch("scenes.create", {"config": {"name": "Plain", "entities": {"input_boolean.test": "on"}}})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.update", {"id": created["id"], "config": config})
    assert ei.value.code == "validation_failed" and key in ei.value.message


async def test_the_household_scene_still_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """Refused at write, never at run: the run-time check says nothing about a routine key."""
    from custom_components.hearth_ai.handlers.scenes import find_scene_run_violations, scene_run_violations

    await _install_stored_scene(core, tmp_path)
    assert await scene_run_violations(core, "scene.night_off") == []
    assert find_scene_run_violations(core, "scene.night_off", {"id": "night1", "entities": {RULE: "off"}}) == []


async def test_an_authored_link_to_that_scene_is_refused(core: HomeAssistant, tmp_path: Path) -> None:
    """A model cannot write `automation.turn_off`, so it cannot write a link to a scene that does it."""
    await _install_stored_scene(core, tmp_path)
    d = build_dispatcher(core)
    link = [{"action": "scene.turn_on", "target": {"entity_id": "scene.night_off"}}]
    automation = {"alias": "Quiet", "triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": link}
    res = await d.dispatch("automations.validate", {"config": automation})
    assert res["ok"] is False and res["status"] == "policy" and RULE in res["error"]
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": automation})
    assert ei.value.code == "validation_failed"
    res = await d.dispatch("scripts.validate", {"key": "quiet", "config": {"alias": "Quiet", "sequence": link}})
    assert res["ok"] is False and res["status"] == "policy" and RULE in res["error"]

    # Through a script the authored automation starts, one hop removed.
    (tmp_path / "scripts.yaml").write_text("quiet:\n  alias: Quiet\n  sequence:\n  - action: scene.turn_on\n    target:\n      entity_id: scene.night_off\n")
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    starts = {"alias": "Start", "triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": [{"action": "script.quiet"}]}
    res = await d.dispatch("automations.validate", {"config": starts})
    assert res["ok"] is False and res["status"] == "policy" and RULE in res["error"]


async def test_the_link_is_refused_from_either_source(core: HomeAssistant, tmp_path: Path) -> None:
    """The membership attribute alone (a scene outside scenes.yaml), and scenes.yaml alone."""
    d = build_dispatcher(core)
    link = {"alias": "Quiet", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.elsewhere"}}]}
    core.states.async_set("scene.elsewhere", "unknown", {"friendly_name": "Elsewhere", "entity_id": ["light.a", "script.open_gate"]})
    res = await d.dispatch("scripts.validate", {"key": "quiet", "config": link})
    assert res["ok"] is False and res["status"] == "policy" and "script.open_gate" in res["error"]
    # The hub reads these words to tell the app which rule it was (`SCENE_SETS_ROUTINE_MARK`, appapi/errors.ts).
    assert "an authored config may not switch an automation, script or scene through a scene" in res["error"]

    # Case-folded like every other domain check: HA lower-cases an entity id itself.
    core.states.async_set("scene.elsewhere", "unknown", {"friendly_name": "Elsewhere", "entity_id": ["Automation.Night_Rule"]})
    res = await d.dispatch("scripts.validate", {"key": "quiet", "config": link})
    assert res["ok"] is False and res["status"] == "policy" and "Automation.Night_Rule" in res["error"]

    await _install_stored_scene(core, tmp_path)
    core.states.async_set("scene.night_off", "unknown", {"friendly_name": "Night off", "id": "night1", "entity_id": []})
    link = {"alias": "Quiet", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.night_off"}}]}
    res = await d.dispatch("scripts.validate", {"key": "quiet", "config": link})
    assert res["ok"] is False and res["status"] == "policy" and RULE in res["error"]

    # A scene of ordinary things is still a link a model may write.
    core.states.async_set("scene.movie_night", "unknown", {"friendly_name": "Movie night", "entity_id": ["light.hall"]})
    fine = {"alias": "Film", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.movie_night"}}]}
    assert (await d.dispatch("scripts.validate", {"key": "film", "config": fine}))["ok"] is True


async def test_a_labelled_script_that_runs_the_scene_still_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """The run-time check of a Hearth-labelled script does not take the authoring refusal with it."""
    from custom_components.hearth_ai.handlers.scripts import script_run_violations

    await _install_stored_scene(core, tmp_path)
    (tmp_path / "scripts.yaml").write_text("evening:\n  alias: Evening\n  sequence:\n  - action: scene.turn_on\n    target:\n      entity_id: scene.night_off\n")
    assert await script_run_violations(core, "script.evening") == []
