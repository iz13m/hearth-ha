"""Helpers, both machines (AgDR-0039).

The load-bearing test here is `test_every_collection_helper_can_be_made_and_removed`, parametrised
over all nine storage-collection domains. Reaching those collections means walking the `__wrapped__`
chain of the websocket command Home Assistant registered, so a release that changes that stack has
to fail in CI rather than in someone's house.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_config_flow, mock_integration, mock_platform

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.handlers import helpers
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

CAPS = frozenset({"helpers.manage"})

# name -> the smallest config Home Assistant accepts for it.
INPUT_CONFIGS: dict[str, dict[str, Any]] = {
    "input_boolean": {"name": "Guests coming"},
    "input_button": {"name": "Goodnight"},
    "input_number": {"name": "Target", "min": 0, "max": 30},
    "input_text": {"name": "Note"},
    "input_select": {"name": "Mode", "options": ["home", "away"]},
    "input_datetime": {"name": "Wake", "has_date": False, "has_time": True},
    "counter": {"name": "Cups"},
    "timer": {"name": "Pasta"},
    "schedule": {"name": "Quiet hours"},
}


@pytest.mark.parametrize("domain", helpers.INPUT_DOMAINS)
async def test_every_collection_helper_can_be_made_and_removed(core: HomeAssistant, domain: str) -> None:
    """Create, list, rename and delete, for each of the nine, with the entity really appearing.

    This is the test that catches Home Assistant changing the decorator stack on its collection
    websocket commands, which is the one fragile thing in this handler.
    """
    assert await async_setup_component(core, domain, {domain: {}})
    await core.async_block_till_done()
    d = build_dispatcher(core, CAPS)

    made = await d.dispatch("helpers.create", {"domain": domain, "config": dict(INPUT_CONFIGS[domain])})
    assert made["kind"] == "input"
    assert made["domain"] == domain
    entity_id = made["entities"][0]
    assert core.states.get(entity_id) is not None

    listed = await d.dispatch("helpers.list", {})
    assert any(r["id"] == made["id"] for r in listed)

    renamed = await d.dispatch("helpers.rename", {"id": made["id"], "name": "Renamed"})
    assert renamed["name"] == "Renamed"
    assert renamed["entities"] == [entity_id]  # renaming never moves the entity id

    removed = await d.dispatch("helpers.delete", {"id": made["id"]})
    assert removed["removed_entities"] == [entity_id]
    await core.async_block_till_done()
    assert core.states.get(entity_id) is None


async def test_a_new_helper_says_whether_hearth_can_read_it(core: HomeAssistant) -> None:
    """The seam that read as a bug: Hearth makes an entity and then cannot see its state.

    Home Assistant shares a new entity with Assist only for a few domains and device classes, and
    most helpers are neither — an `input_boolean` is not a shared domain, a `trend` binary_sensor has
    no device class. So `shared: false` is the ordinary outcome, and the answer has to say so rather
    than leave a model to guess at a sync delay that will never end.
    """
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity

    assert await async_setup_component(core, "input_boolean", {"input_boolean": {}})
    await core.async_block_till_done()
    d = build_dispatcher(core, CAPS)

    made = await d.dispatch("helpers.create", {"domain": "input_boolean", "config": {"name": "Guests coming"}})
    assert made["shared"] is False
    assert next(r for r in await d.dispatch("helpers.list", {}) if r["id"] == made["id"])["shared"] is False

    # ...and it flips once the household shares it, which only a person can do.
    async_expose_entity(core, "conversation", made["entities"][0], True)
    assert next(r for r in await d.dispatch("helpers.list", {}) if r["id"] == made["id"])["shared"] is True


async def test_a_helper_with_nothing_readable_is_not_called_shared(core: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain="group", title="All doors")
    entry.add_to_hass(core)
    er.async_get(core).async_get_or_create("lock", "group", "all_doors", config_entry=entry, suggested_object_id="all_doors")
    d = build_dispatcher(core, CAPS)

    row = next(r for r in await d.dispatch("helpers.list", {}) if r["id"] == entry.entry_id)
    assert row["entities"] == [] and row["restricted"] is True and row["shared"] is False


async def test_a_collection_helper_can_be_reconfigured(core: HomeAssistant) -> None:
    assert await async_setup_component(core, "timer", {"timer": {}})
    await core.async_block_till_done()
    d = build_dispatcher(core, CAPS)

    made = await d.dispatch("helpers.create", {"domain": "timer", "config": {"name": "Pasta", "duration": "00:08:00"}})
    updated = await d.dispatch("helpers.update", {"id": made["id"], "config": {"name": "Pasta", "duration": "00:11:00"}})
    assert updated["id"] == made["id"]
    assert core.states.get(made["entities"][0]).attributes["duration"] == "0:11:00"


async def test_describing_a_type_answers_the_same_shape_for_both_machines(core: HomeAssistant) -> None:
    assert await async_setup_component(core, "input_number", {"input_number": {}})
    await core.async_block_till_done()
    d = build_dispatcher(core, CAPS)

    described = await d.dispatch("helpers.describe", {"domain": "input_number"})
    assert described["kind"] == "input"
    names = [f["name"] for f in described["fields"]]
    assert "name" in names and "min" in names
    # The websocket envelope is not part of the helper.
    assert "id" not in names and "type" not in names


async def test_types_lists_both_machines(core: HomeAssistant) -> None:
    assert await async_setup_component(core, "counter", {"counter": {}})
    await core.async_block_till_done()
    d = build_dispatcher(core, CAPS)

    rows = await d.dispatch("helpers.types", {})
    by_domain = {r["domain"]: r for r in rows}
    assert by_domain["counter"]["kind"] == "input"
    # A config-flow helper every Home Assistant has.
    assert by_domain["template"]["kind"] == "flow"
    assert all(isinstance(r["configured"], int) for r in rows)


async def test_a_helper_may_not_be_built_on_something_off_limits(core: HomeAssistant) -> None:
    """The AgDR-0038 check, reached through the helper path: a trend over a lock is not a trend."""
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.create", {"domain": "trend", "config": {"name": "Door", "entity_id": "lock.front_door"}})
    assert ei.value.code == "validation_failed"
    assert "lock.front_door" in ei.value.message


async def test_an_action_bearing_helper_is_policed_and_leaves_no_flow(core: HomeAssistant) -> None:
    """A refused create must not leave a half-finished setup for a person to find and clean up."""
    d = build_dispatcher(core, CAPS)
    evil = {"name": "Evil", "turn_on": [{"action": "lock.unlock", "target": {"entity_id": "all"}}]}
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.create", {"domain": "template", "variant": "switch", "config": evil})
    assert ei.value.code == "validation_failed"
    assert core.config_entries.flow.async_progress() == []


async def test_a_menu_type_asks_for_a_variant_rather_than_guessing(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    described = await d.dispatch("helpers.describe", {"domain": "template"})
    assert "sensor" in described["variants"]
    assert described["fields"] == []
    # Describing is a read: the flow it opened to look at the form is gone again.
    assert core.config_entries.flow.async_progress() == []

    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.create", {"domain": "template", "config": {"name": "x"}})
    assert ei.value.code == "invalid_params"
    assert "variant" in ei.value.message
    assert core.config_entries.flow.async_progress() == []


async def test_an_unknown_type_points_at_the_right_tool(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.create", {"domain": "hue", "config": {"name": "x"}})
    assert ei.value.code == "not_found"
    assert "search_available_integrations" in ei.value.message


async def test_an_integration_is_never_deleted_as_if_it_were_a_helper(core: HomeAssistant) -> None:
    """The guard that keeps `helpers.delete` off a Hue bridge."""
    entry = MockConfigEntry(domain="demo_hub", title="Hall bridge")
    entry.add_to_hass(core)
    d = build_dispatcher(core, CAPS)

    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.delete", {"id": entry.entry_id})
    assert ei.value.code == "method_not_allowed"
    assert "not a helper" in ei.value.message
    assert core.config_entries.async_get_entry(entry.entry_id) is not None

    # ...and it is not listed as one either.
    assert all(r["id"] != entry.entry_id for r in await d.dispatch("helpers.list", {}))


async def test_reconfiguring_a_flow_helper_says_so_rather_than_pretending(core: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="template", title="Both lamps")
    entry.add_to_hass(core)
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.update", {"id": entry.entry_id, "config": {"name": "x"}})
    assert ei.value.code == "method_not_allowed"
    assert "not supported yet" in ei.value.message


async def test_an_off_limits_entity_is_never_named_in_a_listing(core: HomeAssistant) -> None:
    """A group of locks stays listed so it can be removed; its entity ids do not cross the wire."""
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain="group", title="All doors")
    entry.add_to_hass(core)
    reg = er.async_get(core)
    reg.async_get_or_create("lock", "group", "all_doors", config_entry=entry, suggested_object_id="all_doors")
    d = build_dispatcher(core, CAPS)

    row = next(r for r in await d.dispatch("helpers.list", {}) if r["id"] == entry.entry_id)
    assert row["entities"] == []
    assert row["restricted"] is True


async def test_the_capability_is_required(core: HomeAssistant) -> None:
    d = build_dispatcher(core, frozenset({"entities.read", "integrations.manage"}))
    for method in ("helpers.types", "helpers.describe", "helpers.list", "helpers.create", "helpers.update", "helpers.rename", "helpers.delete"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, {})
        assert ei.value.code == "method_not_allowed"
        assert "disabled" in ei.value.message
