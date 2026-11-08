"""Why a listed scene or script cannot run (#758), and a labelled script listed as it runs (#759).

`can_run` was one bit, and the app read every `false` as "not shared yet": it sent a curator to the
sharing screen for a scene hidden in Home Assistant, which sharing cannot un-hide. `run_refusal` says
which of three things to fix. It is for the hub's list only: a run of a hidden or unshared routine
still answers `not_found` in the words an unknown id gets (#642), and the hub strips the field from
every model-facing answer, so a model cannot tell a hidden routine from an unshared one.

#759: `scripts.list` asked `visible()` alone, so a `Hearth: scene` script with a step the run-time
scan refuses was listed runnable while `scripts.run` refused it. Scenes already folded their scan in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.registry import RUN_REFUSALS
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"

# A step the run-time scan refuses: a raw remote code (#568).
RAW_CODE_SCRIPT = (
    "disarm:\n  alias: Disarm\n  sequence:\n  - action: remote.send_command\n    target:\n"
    "      entity_id: remote.living_room\n    data:\n      device: alarm_keyfob\n      command: b64:JgBQAAABKZIUEhQ2\n"
)
# The same script, playing a code the household taught the remote, by name. Allowed.
TAUGHT_CODE_SCRIPT = (
    "disarm:\n  alias: Disarm\n  sequence:\n  - action: remote.send_command\n    target:\n"
    "      entity_id: remote.living_room\n    data:\n      device: television\n      command: power\n"
)


def _row(rows: list[dict], entity_id: str) -> dict:
    return next(r for r in rows if r["entity_id"] == entity_id)


async def _script(core: HomeAssistant, tmp_path: Path, body: str, *, labelled: bool) -> None:
    await hl.async_ensure_labels(core)
    (tmp_path / "scripts.yaml").write_text(body)
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.disarm", True)
    await hl.async_mark_hearth_scene(core, "script.disarm", labelled)


async def test_a_labelled_script_with_a_refused_step_is_listed_as_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """#759: listed `can_run: false` with `run_refusal: "contents"`, and the run is refused."""
    sent = async_mock_service(core, "remote", "send_command")
    await _script(core, tmp_path, RAW_CODE_SCRIPT, labelled=True)
    d = build_dispatcher(core)

    row = _row(await d.dispatch("scripts.list", {}), "script.disarm")
    assert (row["can_run"], row.get("run_refusal"), row["hearth_scene"]) == (False, "contents", True)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    assert ei.value.code == "method_not_allowed"
    await core.async_block_till_done()
    assert sent == []

    # Control: unlabelled, it is a plain script, which is not scanned (AgDR-0005), and it runs.
    await hl.async_mark_hearth_scene(core, "script.disarm", False)
    row = _row(await d.dispatch("scripts.list", {}), "script.disarm")
    assert row["can_run"] is True and "run_refusal" not in row
    await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    await core.async_block_till_done()
    assert len(sent) == 1


async def test_a_labelled_script_with_nothing_refused_is_runnable(core: HomeAssistant, tmp_path: Path) -> None:
    """Control for the fold: the scan finds nothing, so the label alone does not stop a run."""
    async_mock_service(core, "remote", "send_command")
    er.async_get(core).async_get_or_create("remote", "broadlink", "rm-1", suggested_object_id="living_room")
    await _script(core, tmp_path, TAUGHT_CODE_SCRIPT, labelled=True)
    d = build_dispatcher(core)
    row = _row(await d.dispatch("scripts.list", {}), "script.disarm")
    assert row["can_run"] is True and "run_refusal" not in row
    await d.dispatch("scripts.run", {"entity_id": "script.disarm"})


async def test_hidden_wins_over_contents_and_still_answers_as_an_unknown_id(core: HomeAssistant, tmp_path: Path) -> None:
    """A hidden routine is not read, so its reason is "hidden", and the run's words do not change."""
    await _script(core, tmp_path, RAW_CODE_SCRIPT, labelled=True)
    er.async_get(core).async_update_entity("script.disarm", hidden_by=er.RegistryEntryHider.USER)
    d = build_dispatcher(core)
    row = _row(await d.dispatch("scripts.list", {}), "script.disarm")
    assert (row["can_run"], row.get("run_refusal")) == (False, "hidden")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    assert (ei.value.code, ei.value.message) == ("not_found", "unknown entity script.disarm")


async def _scene(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    await d.dispatch("scenes.create", {"config": {"name": "Evening", "entities": {"input_boolean.test": "on"}}})
    await core.async_block_till_done()
    assert er.async_get(core).async_get("scene.evening") is not None


@pytest.mark.parametrize("listing", ["scenes.list", "scripts.list"])
async def test_each_reason(core: HomeAssistant, listing: str) -> None:
    """Shared and shown: none. Not shared: "not_shared". Hidden or disabled-and-hidden: "hidden"."""
    d = build_dispatcher(core)
    if listing == "scenes.list":
        await _scene(core)
        entity_id = "scene.evening"
    else:
        await d.dispatch(
            "scripts.create",
            {"config": {"alias": "Nudge", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.test"}}]}},
        )
        await core.async_block_till_done()
        entity_id = "script.nudge"
    reg = er.async_get(core)

    async_expose_entity(core, "conversation", entity_id, True)
    row = _row(await d.dispatch(listing, {}), entity_id)
    assert row["can_run"] is True and "run_refusal" not in row

    async_expose_entity(core, "conversation", entity_id, False)
    row = _row(await d.dispatch(listing, {}), entity_id)
    assert (row["can_run"], row.get("run_refusal")) == (False, "not_shared")

    # Hidden and not shared: un-hiding comes first, since sharing alone would not make it run.
    reg.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.USER)
    row = _row(await d.dispatch(listing, {}), entity_id)
    assert (row["can_run"], row.get("run_refusal")) == (False, "hidden")

    async_expose_entity(core, "conversation", entity_id, True)
    row = _row(await d.dispatch(listing, {}), entity_id)
    assert (row["can_run"], row.get("run_refusal")) == (False, "hidden")


async def test_a_scene_that_sets_a_lock_is_refused_for_its_contents(core: HomeAssistant) -> None:
    er.async_get(core).async_get_or_create("scene", "hearth_test", "open_the_gate", suggested_object_id="open_the_gate")
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    async_expose_entity(core, "conversation", "scene.open_the_gate", True)
    d = build_dispatcher(core)
    row = _row(await d.dispatch("scenes.list", {}), "scene.open_the_gate")
    assert (row["can_run"], row.get("run_refusal")) == (False, "contents")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.open_the_gate"})
    assert ei.value.code == "method_not_allowed"


async def test_the_list_reads_scripts_yaml_once(core: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`scripts.list` hands its own parsed copy to the scan, as `scenes.list` does, rather than one read per script.

    A run still reads the file fresh: that is the check that refuses it.
    """
    from custom_components.hearth_ai.handlers import scripts as scripts_mod

    await hl.async_ensure_labels(core)
    (tmp_path / "scripts.yaml").write_text(RAW_CODE_SCRIPT + RAW_CODE_SCRIPT.replace("disarm:", "arm:").replace("Disarm", "Arm"))
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    for entity_id in ("script.disarm", "script.arm"):
        async_expose_entity(core, "conversation", entity_id, True)
        await hl.async_mark_hearth_scene(core, entity_id, True)

    reads: list[str] = []
    real = scripts_mod.read_yaml

    async def counting(hass, path, default):
        reads.append(str(path))
        return await real(hass, path, default)

    monkeypatch.setattr(scripts_mod, "read_yaml", counting)
    d = build_dispatcher(core)
    rows = await d.dispatch("scripts.list", {})
    assert [(r["entity_id"], r.get("run_refusal")) for r in rows] == [("script.arm", "contents"), ("script.disarm", "contents")]
    assert len([p for p in reads if p.endswith("scripts.yaml")]) == 1

    reads.clear()
    with pytest.raises(RpcError):
        await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    assert len([p for p in reads if p.endswith("scripts.yaml")]) == 1


def test_reasons_match_shared() -> None:
    """The box's reasons equal the hub's enum on both lists.

    The hub catches a reason it does not know to absent (unknown) rather than refusing the list, so
    drift fails quietly there: the app would show the old "share it" advice for the new reason. It has
    to fail loudly here instead.
    """
    assert SCHEMA.is_file(), "run `pnpm --filter @hearth/shared export:jsonschema` first"
    methods = json.loads(SCHEMA.read_text())["methods"]
    for method in ("scenes.list", "scripts.list"):
        props = methods[method]["result"]["items"]["properties"]
        assert props["run_refusal"]["enum"] == list(RUN_REFUSALS), method


async def test_rows_carry_only_declared_fields(core: HomeAssistant, tmp_path: Path) -> None:
    """Every key a row sends is one the shared schema declares, and every reason one of its values."""
    await _scene(core)
    await _script(core, tmp_path, RAW_CODE_SCRIPT, labelled=True)
    methods = json.loads(SCHEMA.read_text())["methods"]
    d = build_dispatcher(core)
    for method in ("scenes.list", "scripts.list"):
        props = methods[method]["result"]["items"]["properties"]
        rows = await d.dispatch(method, {})
        assert rows
        for row in rows:
            assert set(row) <= set(props), (method, row)
            if "run_refusal" in row:
                assert row["can_run"] is False
                assert row["run_refusal"] in props["run_refusal"]["enum"]
