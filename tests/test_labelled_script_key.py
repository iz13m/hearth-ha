"""A labelled script is found by its `scripts.yaml` key, not by its entity id (#720).

The key is the registry `unique_id`; the entity id is only where Home Assistant started. An owner may
rename the entity id and keep the key and the `Hearth: scene` label, so both the run-time scan and the
keep-the-label check have to reach the key through the registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.scripts import script_run_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

LOCK = "lock.front_door"
UNLOCKS = f"  alias: Evening\n  sequence:\n  - action: lock.unlock\n    target:\n      entity_id: {LOCK}\n"


async def _load(hass: HomeAssistant, tmp_path: Path, body: str) -> None:
    (tmp_path / "scripts.yaml").write_text(body)
    await hass.services.async_call("script", "reload", blocking=True)
    await hass.async_block_till_done()


async def test_a_renamed_labelled_script_is_still_scanned_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """Renamed from `script.evening` to `script.night`, the script keeps its key and its label. The run
    check read the key off the new id, found no `night` in `scripts.yaml`, and let it unlock the door.
    Control: before the rename the same script is refused the same way."""
    await hl.async_ensure_labels(core)
    core.states.async_set(LOCK, "locked")
    unlocked = async_mock_service(core, "lock", "unlock")
    await _load(core, tmp_path, "evening:\n" + UNLOCKS)
    await hl.async_mark_hearth_scene(core, "script.evening", True)
    assert await script_run_violations(core, "script.evening") != []

    er.async_get(core).async_update_entity("script.evening", new_entity_id="script.night")
    await core.async_block_till_done()
    assert core.states.get("script.night") is not None
    assert hl.is_hearth_scene(core, "script.night")
    async_expose_entity(core, "conversation", "script.night", True)

    assert await script_run_violations(core, "script.night") != []
    with pytest.raises(RpcError) as ei:
        await build_dispatcher(core).dispatch("scripts.run", {"entity_id": "script.night"})
    assert ei.value.code == "method_not_allowed"
    await core.async_block_till_done()
    assert unlocked == []


async def test_a_script_the_registry_does_not_know_is_read_by_its_object_id(core: HomeAssistant, tmp_path: Path) -> None:
    """The fallback: with no registry entry, the key is the entity id's object id, as before."""
    (tmp_path / "scripts.yaml").write_text("ghost:\n" + UNLOCKS)
    assert er.async_get(core).async_get("script.ghost") is None
    assert await script_run_violations(core, "script.ghost") != []


async def test_a_create_that_lands_on_a_labelled_scripts_old_key_keeps_the_label(core: HomeAssistant, tmp_path: Path) -> None:
    """`_free_key` checks `scripts.yaml` and live states, not the registry. A labelled script deleted
    from the file leaves its registry entry, so a create with no id may choose its key, and the reload
    re-attaches the entry, label and all; `hearth_scene: false` on that create then took the label off.
    It is refused, before anything is written. Control: the same create with the flag absent goes
    through and the label stays."""
    await hl.async_ensure_labels(core)
    d = build_dispatcher(core)
    config = {"alias": "Evening", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.test"}}]}
    made = await d.dispatch("scripts.create", {"id": "evening", "config": config, "hearth_scene": True})
    await core.async_block_till_done()
    assert hl.is_hearth_scene(core, made["entity_id"])

    # Disabled, then deleted by hand from scripts.yaml: a disabled entity has no state, so nothing
    # `_free_key` reads says the key was ever used.
    er.async_get(core).async_update_entity(made["entity_id"], disabled_by=er.RegistryEntryDisabler.USER)
    await _load(core, tmp_path, "{}\n")
    assert core.states.get(made["entity_id"]) is None
    assert er.async_get(core).async_get(made["entity_id"]) is not None

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.create", {"config": config, "hearth_scene": False})
    assert ei.value.code == "method_not_allowed"
    assert (tmp_path / "scripts.yaml").read_text().strip() == "{}"
    assert hl.is_hearth_scene(core, made["entity_id"])

    again = await d.dispatch("scripts.create", {"config": config})
    await core.async_block_till_done()
    assert again == {"id": "evening", "entity_id": made["entity_id"]}
    assert hl.is_hearth_scene(core, made["entity_id"])
