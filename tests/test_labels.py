"""Hearth labels (AgDR-0034): created once, recognised by id or name, reported raw, watched for changes."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.access import access_list
from custom_components.hearth_ai.handlers.registry import entities_list
from custom_components.hearth_ai.handlers.vision import vision_list
from custom_components.hearth_ai.rpc import RpcError


def _ids(hass: HomeAssistant) -> dict[str, str]:
    return dict(hass.data[hl.DATA_LABEL_IDS])


def _hearth(hass: HomeAssistant) -> list[str]:
    return sorted(label.name for label in lr.async_get(hass).async_list_labels() if label.name.startswith("Hearth:"))


async def _reload(hass: HomeAssistant) -> None:
    """A new Home Assistant run: nothing in memory, only what the store kept."""
    hass.data.pop(hl.DATA_LABEL_IDS, None)
    await hl.async_ensure_labels(hass)


async def test_creates_the_five_labels(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    assert _hearth(core) == sorted(hl.ROLES.values())
    assert set(_ids(core)) == set(hl.ROLES)


async def test_is_idempotent_across_setups(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    first = _ids(core)
    await _reload(core)
    await hl.async_ensure_labels(core)
    assert _ids(core) == first
    assert len(_hearth(core)) == 5


async def test_adopts_a_label_the_owner_already_made(core: HomeAssistant) -> None:
    mine = lr.async_get(core).async_create("hearth: HIDE")  # same name as far as Home Assistant is concerned
    await hl.async_ensure_labels(core)
    assert _ids(core)["hide"] == mine.label_id
    # The owner's lowercase one does not start with "Hearth:", and no second one was made beside it.
    assert len(_hearth(core)) == 4
    assert len(lr.async_get(core).async_list_labels()) == 5


async def test_survives_a_rename(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    reg = lr.async_get(core)
    tile = _ids(core)["tile"]
    reg.async_update(tile, name="Own tile")
    await _reload(core)
    assert _ids(core)["tile"] == tile
    assert hl.role_of(core, tile) == "tile"
    assert reg.async_get_label_by_name("Hearth: tile") is None, "a renamed label is not recreated beside it"


async def test_recreates_a_deleted_label_on_the_next_setup(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    reg = lr.async_get(core)
    old = _ids(core)["setting"]
    reg.async_delete(old)
    assert reg.async_get_label(old) is None
    await _reload(core)
    # Home Assistant derives the id from the name, so it may well come back with the same one.
    assert reg.async_get_label(_ids(core)["setting"]) is not None
    assert reg.async_get_label_by_name("Hearth: setting") is not None


async def test_a_creation_race_adopts_the_winner(core: HomeAssistant) -> None:
    reg = lr.async_get(core)
    real_create = reg.async_create

    def racing(name: str, **kwargs):
        real_create(name, **kwargs)  # somebody else got there first
        raise ValueError("already in use")

    with patch.object(reg, "async_create", side_effect=racing):
        await hl.async_ensure_labels(core)
    assert set(_ids(core)) == set(hl.ROLES)
    assert len(_hearth(core)) == 5


async def test_never_fails_setup(core: HomeAssistant) -> None:
    with patch.object(hl, "Store", side_effect=RuntimeError("disk full")):
        await hl.async_ensure_labels(core)  # must not raise


# ------------------------------------------------------------------ what is reported


def _device_with_light(hass: HomeAssistant, uid: str, parent: dr.DeviceEntry | None = None) -> tuple[dr.DeviceEntry, er.RegistryEntry]:
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", uid)},
        name=uid,
        **({"via_device_id": parent.id} if parent else {}),
    )
    ent = er.async_get(hass).async_get_or_create("light", "test", uid, device_id=device.id, config_entry=entry, suggested_object_id=uid)
    hass.states.async_set(ent.entity_id, "on")
    async_expose_entity(hass, "conversation", ent.entity_id, True)
    return device, ent


async def _row(hass: HomeAssistant, entity_id: str) -> dict:
    rows = await entities_list(hass, {"limit": 500})
    return next(r for r in rows if r["entity_id"] == entity_id)


async def test_reports_entity_and_device_labels_separately(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    ids = _ids(core)
    other = lr.async_get(core).async_create("Kitchen stuff").label_id
    device, ent = _device_with_light(core, "lamp")
    er.async_get(core).async_update_entity(ent.entity_id, labels={ids["tile"], ids["read_only"], other})
    dr.async_get(core).async_update_device(device.id, labels={ids["hide"], other})

    row = await _row(core, ent.entity_id)
    # Raw, both roles, no precedence applied: that is the hub's to decide.
    assert row["hearth_labels"] == {"entity": ["tile", "read_only"], "device": ["hide"]}


async def test_a_child_device_does_not_inherit_its_parents_labels(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    parent, _ = _device_with_light(core, "hub")
    _, child = _device_with_light(core, "bulb", parent=parent)
    dr.async_get(core).async_update_device(parent.id, labels={_ids(core)["hide"]})
    assert (await _row(core, child.entity_id))["hearth_labels"] == {"entity": [], "device": []}


async def test_a_label_named_by_hand_counts(core: HomeAssistant) -> None:
    # No setup has run: the owner made the label themselves.
    label = lr.async_get(core).async_create("Hearth: setting")
    _, ent = _device_with_light(core, "strip")
    er.async_get(core).async_update_entity(ent.entity_id, labels={label.label_id})
    assert (await _row(core, ent.entity_id))["hearth_labels"]["entity"] == ["setting"]


async def test_doors_and_cameras_carry_them_too(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    hide = _ids(core)["hide"]
    ents = er.async_get(core)
    for domain in ("lock", "camera"):
        e = ents.async_get_or_create(domain, "test", f"{domain}-1", suggested_object_id="garage")
        ents.async_update_entity(e.entity_id, labels={hide})
        core.states.async_set(e.entity_id, "locked" if domain == "lock" else "idle")
    [lock] = await access_list(core, {})
    [camera] = await vision_list(core, {})
    assert lock["hearth_labels"] == {"entity": ["hide"], "device": []}
    assert camera["hearth_labels"] == {"entity": ["hide"], "device": []}


# ------------------------------------------------------------------ watching


class _Sink:
    def __init__(self) -> None:
        self.count = 0
        self.fail: Exception | None = None

    async def __call__(self) -> None:
        self.count += 1
        if self.fail:
            raise self.fail


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()
    await asyncio.sleep(hl.DEBOUNCE_S + 0.2)
    await hass.async_block_till_done()


async def test_watcher_fires_on_a_hearth_label_and_not_on_another(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    other = lr.async_get(core).async_create("Kitchen stuff").label_id
    _, ent = _device_with_light(core, "lamp")
    sink = _Sink()
    w = hl.RegistryWatcher(core, sink)
    w.start()
    try:
        er.async_get(core).async_update_entity(ent.entity_id, labels={other})
        await _settle(core)
        assert sink.count == 0, "an unrelated label must not make every phone re-read the home"

        er.async_get(core).async_update_entity(ent.entity_id, labels={other, _ids(core)["tile"]})
        await _settle(core)
        assert sink.count == 1

        # Taking it off counts too: the old set is what names it.
        er.async_get(core).async_update_entity(ent.entity_id, labels={other})
        await _settle(core)
        assert sink.count == 2
    finally:
        w.stop()


async def test_watcher_sees_device_labels(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    device, _ = _device_with_light(core, "lamp")
    sink = _Sink()
    w = hl.RegistryWatcher(core, sink)
    w.start()
    try:
        dr.async_get(core).async_update_device(device.id, labels={_ids(core)["hide"]})
        await _settle(core)
        assert sink.count == 1
    finally:
        w.stop()


async def test_deleting_a_label_on_many_entities_sends_one_frame(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    hide = _ids(core)["hide"]
    for i in range(6):
        _, ent = _device_with_light(core, f"lamp{i}")
        er.async_get(core).async_update_entity(ent.entity_id, labels={hide})
    await core.async_block_till_done()
    sink = _Sink()
    w = hl.RegistryWatcher(core, sink)
    w.start()
    try:
        lr.async_get(core).async_delete(hide)
        await _settle(core)
        assert sink.count == 1
    finally:
        w.stop()


async def test_watcher_swallows_an_older_hubs_refusal(core: HomeAssistant) -> None:
    await hl.async_ensure_labels(core)
    _, ent = _device_with_light(core, "lamp")
    sink = _Sink()
    sink.fail = RpcError("method_not_allowed", "unknown method registry.changed")
    w = hl.RegistryWatcher(core, sink)
    w.start()
    try:
        for role in ("tile", "setting"):
            er.async_get(core).async_update_entity(ent.entity_id, labels={_ids(core)[role]})
            await _settle(core)
        assert sink.count == 2
    finally:
        w.stop()
