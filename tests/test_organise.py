"""Labels and categories (AgDR-0040).

The two tests that matter most are the ones about Hearth's own labels and about what may be
labelled: everything else is CRUD over two Home Assistant registries.
"""

from __future__ import annotations

import pytest

from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr, entity_registry as er, label_registry as lr

from custom_components.hearth_ai.handlers import registry
from custom_components.hearth_ai.labels import ALL_LABELS, async_ensure_labels, async_mark_hearth_scene, is_hearth_scene
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
    # All six, `Hearth: scene` included (#617): the flag says what the guard below refuses.
    assert {row["name"] for row in ours} == set(ALL_LABELS.values())
    assert "Hearth: scene" in {row["name"] for row in ours}

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


async def test_hearth_scene_is_refused_like_the_presentation_labels(core: HomeAssistant) -> None:
    """`Hearth: scene` marks the scripts whose contents are checked each time they run (AgDR-0044).

    It is not a presentation role, so `role_of` does not know it, and the guard used to ask `role_of`.
    A model with `labels.manage` could then take the label off a scene, or delete it, and the run-time
    scan stopped applying to every scene it marked (#617). The ordinary label is the control: the same
    dispatcher, the same calls, and they still work.
    """
    await async_ensure_labels(core)
    marked = _entity(core, "script", "evening", expose=False)
    other = _entity(core, "script", "morning", expose=False)
    await async_mark_hearth_scene(core, marked, True)
    assert is_hearth_scene(core, marked)
    d = build_dispatcher(core, CAPS)
    scene = next(row for row in await d.dispatch("labels.list", {}) if row["name"] == "Hearth: scene")
    assert scene["hearth"] is True

    for method, params in [
        ("labels.update", {"label_id": scene["label_id"], "name": "Not a scene"}),
        ("labels.delete", {"label_id": scene["label_id"]}),
        ("labels.assign", {"label_id": scene["label_id"], "entity_ids": [marked], "mode": "remove"}),
        ("labels.assign", {"label_id": scene["label_id"], "entity_ids": [other]}),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed", method
        assert "Hearth: scene" in ei.value.message
    # ...and nothing happened.
    assert lr.async_get(core).async_get_label(scene["label_id"]).name == "Hearth: scene"
    assert is_hearth_scene(core, marked)
    assert not is_hearth_scene(core, other)

    # The control: an ordinary label goes through every one of those calls.
    made = await d.dispatch("labels.create", {"name": "Evenings"})
    assert made["hearth"] is False
    assert (await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [other]}))["changed"] == [other]
    assert (await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [other], "mode": "remove"}))["changed"] == [other]
    assert (await d.dispatch("labels.update", {"label_id": made["label_id"], "name": "Nights"}))["name"] == "Nights"
    assert (await d.dispatch("labels.delete", {"label_id": made["label_id"]}))["removed_from"] == []
    assert lr.async_get(core).async_get_label(made["label_id"]) is None


async def test_a_renamed_hearth_scene_label_is_still_refused(core: HomeAssistant) -> None:
    """Resolved by stored id, as a renamed presentation label is. The next test is the by-name half."""
    await async_ensure_labels(core)
    d = build_dispatcher(core, CAPS)
    scene = next(row for row in await d.dispatch("labels.list", {}) if row["name"] == "Hearth: scene")
    lr.async_get(core).async_update(scene["label_id"], name="Something else")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.delete", {"label_id": scene["label_id"]})
    assert ei.value.code == "method_not_allowed"


async def test_an_owner_made_hearth_scene_label_is_refused_by_name(core: HomeAssistant) -> None:
    """No stored ids at all (the store never ran), so only the name can say it is Hearth's."""
    label = lr.async_get(core).async_create("Hearth: scene")
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.delete", {"label_id": label.label_id})
    assert ei.value.code == "method_not_allowed"
    assert lr.async_get(core).async_get_label(label.label_id) is not None


@pytest.mark.parametrize("domain", ["lock", "camera", "device_tracker", "person", "image", "image_processing", "alarm_control_panel"])
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


async def test_an_entity_kept_from_the_model_is_refused_in_a_ghosts_words(core: HomeAssistant) -> None:
    """#651: an id the model may not see is refused exactly as one that does not exist.

    The registry reason used to come first, so a ghost was told it "is not in the entity registry"
    while a registered entity the owner kept back got `unknown entity`: the second answer meant
    "this exists". Each id below is compared with `sensor.ghost`, which has nothing at all, once the
    ids are masked:
    - `sensor.boiler_pressure` is registered and has a state, but is not exposed.
    - `light.ghost` has no state and no registry entry, but its domain is exposed by default, so
      `visible()` alone passes it.
    - `sensor.removed` is marked exposed in the store, with no state and no registry entry, as a
      YAML entity that has since been removed is.
    No refusal leaves a new key in the exposure store.
    """
    kept = _entity(core, "sensor", "boiler_pressure", expose=False)
    async_expose_entity(core, "conversation", "sensor.removed", True)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Plumbing"})
    store = core.data[DATA_EXPOSED_ENTITIES].entities
    before = set(store)

    async def refusal(entity_id: str) -> tuple[str, str]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [entity_id]})
        return ei.value.code, ei.value.message.replace(entity_id, "<id>")

    ghost = await refusal("sensor.ghost")
    assert ghost[0] == "not_found"
    assert {i: await refusal(i) for i in (kept, "light.ghost", "sensor.removed")} == {
        i: ghost for i in (kept, "light.ghost", "sensor.removed")
    }
    assert set(store) == before
    assert lr.async_get(core).async_get_label(made["label_id"]) is not None
    assert er.async_get(core).async_get(kept).labels == set()


async def test_a_visible_entity_with_no_registry_entry_gets_the_registry_reason(core: HomeAssistant) -> None:
    """#651's control: the registry reason is still given, to an entity the model can already see."""
    core.states.async_set("sensor.yaml_only", "1")
    async_expose_entity(core, "conversation", "sensor.yaml_only", True)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Garden"})

    with pytest.raises(RpcError) as ei:
        await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": ["sensor.yaml_only"]})
    assert (ei.value.code, ei.value.message) == (
        "not_found",
        "sensor.yaml_only is not in the entity registry, so it cannot be labelled",
    )


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
async def test_a_disabled_routine_is_refused_in_a_ghosts_words(core: HomeAssistant, domain: str) -> None:
    """#651: the three own lists are built from states, and a registry-disabled entry has none.

    So none of them names a disabled automation, script or scene. Labelling one, while a ghost in
    the same domain got the registry reason, told the model which disabled ids exist. It is refused
    in the ghost's words once the ids are masked, and gains no label. The control is the enabled
    automation with no exposure in the row below, which can still be labelled.
    """
    reg = er.async_get(core)
    disabled = reg.async_get_or_create(
        domain, "demo", "old", suggested_object_id="old", disabled_by=er.RegistryEntryDisabler.USER
    ).entity_id
    assert core.states.get(disabled) is None
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Archive"})

    async def refusal(entity_id: str) -> tuple[str, str]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [entity_id]})
        return ei.value.code, ei.value.message.replace(entity_id, "<id>")

    ghost = await refusal(f"{domain}.ghost")
    assert ghost[0] == "not_found"
    assert await refusal(disabled) == ghost
    assert reg.async_get(disabled).labels == set()


@pytest.mark.parametrize("domain", ["automation", "script", "scene"])
async def test_routines_can_be_labelled_though_assist_never_shares_them(core: HomeAssistant, domain: str) -> None:
    """The model reaches these through its own lists, so exposure is the wrong gate for them.

    Each has a state and is explicitly not exposed, which is asserted: Home Assistant exposes
    `scene` by default, so leaving exposure unset would let `visible()` pass a scene with no
    exemption at all. This is the positive control for every domain in `OWN_LIST_DOMAINS`, so
    narrowing the `visible()` exemption to fewer domains goes red.
    """
    routine = _entity(core, domain, "hall_at_sunset", expose=False)
    async_expose_entity(core, "conversation", routine, False)
    assert not registry._exposed(core, routine)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("labels.create", {"name": "Evening"})
    assert (await d.dispatch("labels.assign", {"label_id": made["label_id"], "entity_ids": [routine]}))["changed"] == [routine]


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


async def test_a_wrong_kind_later_in_the_list_files_nothing(core: HomeAssistant) -> None:
    """#653: a refused call changes nothing, wherever in the list the wrong kind sits.

    The kind was checked inside the write loop, so `[automation.a, script.b]` filed `automation.a`
    and then refused the call: the model was told it failed when half of it applied. The control is
    the same call without the script, which files the automation.
    """
    automation = _entity(core, "automation", "a", expose=False)
    script = _entity(core, "script", "b", expose=False)
    d = build_dispatcher(core, CAPS)
    made = await d.dispatch("categories.create", {"scope": "automation", "name": "Lighting"})

    def assign(*ids: str):
        params = {"scope": "automation", "category_id": made["category_id"], "entity_ids": list(ids)}
        return d.dispatch("categories.assign", params)

    with pytest.raises(RpcError) as ei:
        await assign(automation, script)
    assert (ei.value.code, ei.value.message) == (
        "invalid_params",
        f"{script} is not a automation, so it cannot go in a automation category",
    )
    assert not er.async_get(core).async_get(automation).categories

    assert (await assign(automation))["changed"] == [automation]
    assert er.async_get(core).async_get(automation).categories == {"automation": made["category_id"]}


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
