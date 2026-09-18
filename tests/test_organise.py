"""Labels and categories (AgDR-0040).

The two tests that matter most are the ones about Hearth's own labels and about what may be
labelled: everything else is CRUD over two Home Assistant registries.
"""

from __future__ import annotations

import pytest

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr, entity_registry as er, label_registry as lr

from custom_components.hearth_ai.labels import async_ensure_labels
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

CAPS = frozenset({"labels.manage"})


def _entity(hass: HomeAssistant, domain: str, object_id: str, *, expose: bool = True) -> str:
    ent = er.async_get(hass).async_get_or_create(domain, "demo", object_id, suggested_object_id=object_id)
    hass.states.async_set(ent.entity_id, "on")
    if expose:
        async_expose_entity(hass, "conversation", ent.entity_id, True)
    return ent.entity_id


async def test_a_label_is_made_applied_and_removed(core: HomeAssistant) -> None:
    light = _entity(core, "light", "hall")
    d = build_dispatcher(core, CAPS)

    made = await d.dispatch("labels.create", {"name": "Holiday", "icon": "mdi:beach"})
    assert made["name"] == "Holiday" and made["hearth"] is False

    assigned = await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [light]})
    assert assigned["changed"] == [light]
    assert made["label_id"] in er.async_get(core).async_get(light).labels

    # Idempotent: applying it twice is not an error and changes nothing the second time.
    assert (await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [light]}))["changed"] == []

    removed = await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [light], "mode": "remove"})
    assert removed["changed"] == [light]
    assert made["label_id"] not in er.async_get(core).async_get(light).labels


async def test_hearths_own_labels_are_listed_but_never_touched(core: HomeAssistant) -> None:
    """Labels are not an AI control (AgDR-0034), and this is the surface that could have made them one."""
    await async_ensure_labels(core)
    light = _entity(core, "light", "hall")
    d = build_dispatcher(core, CAPS)

    listed = await d.dispatch("labels.list", {})
    ours = [row for row in listed if row["hearth"]]
    assert {row["name"] for row in ours} == {"Hearth: tile", "Hearth: read only", "Hearth: setting", "Hearth: diagnostic", "Hearth: hide"}

    hide = next(row for row in ours if row["name"] == "Hearth: hide")
    for method, params in [
        ("labels.update", {"label_id": hide["label_id"], "name": "Not hide"}),
        ("labels.delete", {"label_id": hide["label_id"]}),
        ("labels.assign", {"label_id": hide["label_id"], "entity_ids": [light]}),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed"
        assert "Hearth" in ei.value.message
    # ...and nothing happened.
    assert lr.async_get(core).async_get_label(hide["label_id"]).name == "Hearth: hide"
    assert hide["label_id"] not in er.async_get(core).async_get(light).labels


async def test_a_renamed_hearth_label_is_still_refused(core: HomeAssistant) -> None:
    """`role_of` resolves by stored id as well as by name, so renaming one out of the way fails."""
    await async_ensure_labels(core)
    d = build_dispatcher(core, CAPS)
    hide = next(row for row in await d.dispatch("labels.list", {}) if row["name"] == "Hearth: hide")
    lr.async_get(core).async_update(hide["label_id"], name="Something else")

    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.delete", {"label_id": hide["label_id"]})
    assert ei.value.code == "method_not_allowed"


@pytest.mark.parametrize("domain", ["lock", "camera", "device_tracker", "person", "image", "alarm_control_panel"])
async def test_an_off_limits_entity_cannot_be_labelled(core: HomeAssistant, domain: str) -> None:
    entity_id = _entity(core, domain, "front")
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": f"Tag {domain}"})

    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [entity_id]})
    assert ei.value.code == "not_found"


async def test_an_unexposed_entity_cannot_be_labelled(core: HomeAssistant) -> None:
    """Exposure is the boundary here as everywhere else: labelling is not a way to find things."""
    hidden = _entity(core, "sensor", "boiler_pressure", expose=False)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Plumbing"})

    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [hidden]})
    assert ei.value.code == "not_found"


async def test_automations_can_be_labelled_though_assist_never_shares_them(core: HomeAssistant) -> None:
    """The model reaches automations through its own list, so exposure is the wrong gate for them."""
    automation = _entity(core, "automation", "hall_at_sunset", expose=False)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Evening"})
    assert (await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [automation]}))["changed"] == [automation]


async def test_deleting_a_label_says_what_it_came_off(core: HomeAssistant) -> None:
    light = _entity(core, "light", "hall")
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Holiday"})
    await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [light]})

    assert (await d.dispatch("labels.delete", {"label_id": made["label_id"]}))["removed_from"] == [light]
    assert lr.async_get(core).async_get_label(made["label_id"]) is None


async def test_a_duplicate_name_is_reported_not_duplicated(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    await d.dispatch("labels.create", {"name": "Holiday"})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.create", {"name": "Holiday"})
    assert ei.value.code == "validation_failed"
    assert "already exists" in ei.value.message


async def test_categories_sort_the_three_lists_separately(core: HomeAssistant) -> None:
    automation = _entity(core, "automation", "hall_at_sunset", expose=False)
    d = build_dispatcher(core, CAPS)

    made = await d.dispatch("categories.create", {"scope": "automation", "name": "Lighting"})
    assert made["scope"] == "automation"
    # The same name in another list is a different category, not a duplicate.
    assert (await d.dispatch("categories.create", {"scope": "script", "name": "Lighting"}))["category_id"] != made["category_id"]
    assert [c["name"] for c in await d.dispatch("categories.list", {"scope": "automation"})] == ["Lighting"]

    filed = await d.dispatch("categories.assign", {"scope": "automation", "category_id": made["category_id"], "entity_ids": [automation]})
    assert filed["changed"] == [automation]
    assert er.async_get(core).async_get(automation).categories["automation"] == made["category_id"]

    # null takes it back out, without the caller having to know where it started.
    await d.dispatch("categories.assign", {"scope": "automation", "category_id": None, "entity_ids": [automation]})
    assert "automation" not in (er.async_get(core).async_get(automation).categories or {})


async def test_a_category_only_takes_its_own_kind(core: HomeAssistant) -> None:
    light = _entity(core, "light", "hall")
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("categories.create", {"scope": "automation", "name": "Lighting"})

    with pytest.raises(RpcError) as ei:
        await d.dispatch("categories.assign", {"scope": "automation", "category_id": made["category_id"], "entity_ids": [light]})
    assert ei.value.code == "invalid_params"
    assert "not a automation" in ei.value.message


async def test_deleting_a_category_empties_it_without_deleting_what_was_in_it(core: HomeAssistant) -> None:
    automation = _entity(core, "automation", "hall_at_sunset", expose=False)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("categories.create", {"scope": "automation", "name": "Lighting"})
    await d.dispatch("categories.assign", {"scope": "automation", "category_id": made["category_id"], "entity_ids": [automation]})

    assert (await d.dispatch("categories.delete", {"scope": "automation", "category_id": made["category_id"]}))["removed_from"] == [automation]
    assert cr.async_get(core).async_get_category(scope="automation", category_id=made["category_id"]) is None
    assert er.async_get(core).async_get(automation) is not None


async def test_a_bad_scope_is_refused(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("categories.list", {"scope": "light"})
    assert ei.value.code == "invalid_params"


async def test_the_capability_is_required(core: HomeAssistant) -> None:
    d = build_dispatcher(core, frozenset({"entities.read"}))
    for method in ("labels.list", "labels.create", "labels.assign", "categories.list", "categories.assign"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, {})
        assert ei.value.code == "method_not_allowed"
