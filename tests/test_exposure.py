"""Choosing what Hearth may see (AgDR-0024).

Exposure is what the whole tool surface filters on, so these are mostly about what `entities.exposable`
refuses to say and what `entities.expose` refuses to do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity, async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai.handlers import exposure
from custom_components.hearth_ai.handlers.exposure import REFUSAL_CODES
from custom_components.hearth_ai.rpc import build_dispatcher

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def _register(core: HomeAssistant, domain: str, object_id: str, **kwargs) -> str:
    """A registry entry plus a state, which is what a real device looks like to both handlers."""
    reg = er.async_get(core)
    entry = reg.async_get_or_create(domain, "test", f"{domain}-{object_id}", suggested_object_id=object_id, **kwargs)
    core.states.async_set(entry.entity_id, "off")
    return entry.entity_id


async def test_lists_candidates_with_no_state_or_attributes(core: HomeAssistant) -> None:
    _register(core, "light", "hallway")
    rows = await build_dispatcher(core).dispatch("entities.exposable", {})
    row = next(r for r in rows if r["entity_id"] == "light.hallway")
    # Enough to recognise a device and choose. Nothing you could read a house from.
    assert set(row) == {"entity_id", "name", "domain", "area_id", "device_name", "exposed", "can_share", "device_id", "entity_category"}
    assert "state" not in row and "attributes" not in row


async def test_says_which_are_already_shared(core: HomeAssistant) -> None:
    """Home Assistant's own defaults decide the starting point, and they are not all-or-nothing.

    `DEFAULT_EXPOSED_DOMAINS` covers light, switch, cover, climate, fan, media_player and scene, so a
    new bulb is already shared. A script, a helper or a sensor without a recognised device class is
    not. That asymmetry is most of why this feature is worth having in both directions.
    """
    light = _register(core, "light", "hallway")
    sensor = _register(core, "sensor", "cupboard_humidity")
    rows = await build_dispatcher(core).dispatch("entities.exposable", {})
    by_id = {r["entity_id"]: r for r in rows}
    assert by_id[light]["exposed"] is True
    assert by_id[sensor]["exposed"] is False


async def test_never_offers_a_domain_hearth_cannot_work_with(core: HomeAssistant) -> None:
    """Including `alarm_control_panel`, which the read denylist happens not to name.

    `HIDDEN_DOMAINS` and the action policy's `DENIED_ENTITY_DOMAINS` are nearly the same list and
    differ in exactly the two places that matter here: `image` is only in the first, and
    `alarm_control_panel` only in the second. Offering an alarm panel would let an admin share it and
    then read whether the house is armed.
    """
    off_limits = {"lock", "camera", "device_tracker", "person", "alarm_control_panel", "image"}
    for domain in off_limits:
        _register(core, domain, "front")
    rows = await build_dispatcher(core).dispatch("entities.exposable", {})
    assert not {r["domain"] for r in rows} & off_limits


async def test_refuses_to_share_an_alarm_panel(core: HomeAssistant) -> None:
    alarm = _register(core, "alarm_control_panel", "house")
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": [alarm], "expose": True})
    assert out["changed"] == []
    assert async_should_expose(core, "conversation", alarm) is False


async def test_an_off_limits_entity_can_always_be_taken_back(core: HomeAssistant) -> None:
    """The denylist is one-directional, and this is the case that forced it.

    Someone exposed an alarm panel to Home Assistant's own Assist years ago, so Hearth reads its
    state today. A rule that refuses to *un*-share it would mean the one screen offering to take it
    back said no for the most sensitive entity in the house — strictly worse than having no rule.
    """
    alarm = _register(core, "alarm_control_panel", "house")
    async_expose_entity(core, "conversation", alarm, True)
    d = build_dispatcher(core)

    # It is listed, because it is exposed — and marked as something that can never be given back.
    row = next(r for r in await d.dispatch("entities.exposable", {}) if r["entity_id"] == alarm)
    assert row["exposed"] is True and row["can_share"] is False

    out = await d.dispatch("entities.expose", {"entity_ids": [alarm], "expose": False})
    assert out["changed"] == [alarm]
    assert async_should_expose(core, "conversation", alarm) is False
    # And once taken back it disappears, so it cannot be re-shared by mistake.
    assert alarm not in {r["entity_id"] for r in await d.dispatch("entities.exposable", {})}


async def test_handles_an_entity_with_no_registry_entry(core: HomeAssistant) -> None:
    """A YAML template or MQTT light without a `unique_id` has a state and no registry entry.

    `entities.list` returns those, and Home Assistant exposes `light` by default, so Hearth can
    already see and operate one. Refusing it here would have meant the devices Hearth most visibly
    *does* see were exactly the ones this screen could neither list nor take back.
    """
    core.states.async_set("light.yaml_garage", "on")
    d = build_dispatcher(core)
    assert "light.yaml_garage" in {e["entity_id"] for e in await d.dispatch("entities.list", {})}

    row = next(r for r in await d.dispatch("entities.exposable", {}) if r["entity_id"] == "light.yaml_garage")
    assert row["exposed"] is True

    out = await d.dispatch("entities.expose", {"entity_ids": ["light.yaml_garage"], "expose": False})
    assert out["changed"] == ["light.yaml_garage"]
    assert "light.yaml_garage" not in {e["entity_id"] for e in await d.dispatch("entities.list", {})}


async def test_filters_by_domain_and_query(core: HomeAssistant) -> None:
    """Without these a home past the page ceiling would have devices the app could never reach."""
    _register(core, "light", "kitchen_ceiling")
    _register(core, "switch", "kettle")
    d = build_dispatcher(core)

    lights = await d.dispatch("entities.exposable", {"domain": "light"})
    assert lights and {r["domain"] for r in lights} == {"light"}

    found = await d.dispatch("entities.exposable", {"query": "kettle"})
    assert [r["entity_id"] for r in found] == ["switch.kettle"]


async def test_refuses_a_malformed_entity_id(core: HomeAssistant) -> None:
    """Both sides validate, always. The hub's EntityId regex blocks this shape too.

    A registry lookup resolves an entry *id* as well as an entity_id, so a bare uuid would otherwise
    reach `async_expose_entity` having skipped the domain check entirely.
    """
    ent = er.async_get(core).async_get_or_create("lock", "test", "lock-front", suggested_object_id="front")
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": [ent.id, "NotAnId"], "expose": True})
    assert out["changed"] == []
    assert {(r["code"], r["reason"]) for r in out["refused"]} == {("not_entity", "not an entity id")}
    assert async_should_expose(core, "conversation", "lock.front") is False


async def test_refuses_to_share_a_domain_hearth_cannot_work_with(core: HomeAssistant) -> None:
    lock = _register(core, "lock", "front")
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": [lock], "expose": True})
    assert out["changed"] == []
    # The domain travels with the code so the app can say what kind of device it was (#452).
    assert out["refused"] == [
        {"entity_id": lock, "code": "off_limits", "reason": "Hearth never works with lock entities", "domain": "lock"}
    ]
    # And the refusal did not quietly happen anyway.
    assert async_should_expose(core, "conversation", lock) is False


async def test_shares_and_unshares(core: HomeAssistant) -> None:
    light = _register(core, "light", "hallway")
    d = build_dispatcher(core)

    out = await d.dispatch("entities.expose", {"entity_ids": [light], "expose": True})
    assert out == {"changed": [light], "refused": []}
    assert async_should_expose(core, "conversation", light) is True

    out = await d.dispatch("entities.expose", {"entity_ids": [light], "expose": False})
    assert out == {"changed": [light], "refused": []}
    assert async_should_expose(core, "conversation", light) is False


async def test_one_bad_id_does_not_lose_the_rest(core: HomeAssistant) -> None:
    good = _register(core, "light", "hallway")
    lock = _register(core, "lock", "front")
    out = await build_dispatcher(core).dispatch(
        "entities.expose", {"entity_ids": [good, lock, "light.does_not_exist"], "expose": True}
    )
    # Partial success: the one that could be shared was, and the others say why not.
    assert out["changed"] == [good]
    assert {r["entity_id"] for r in out["refused"]} == {lock, "light.does_not_exist"}
    assert async_should_expose(core, "conversation", good) is True


async def test_refuses_a_device_that_is_not_there(core: HomeAssistant) -> None:
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": ["light.does_not_exist"], "expose": True})
    assert out["refused"] == [{"entity_id": "light.does_not_exist", "code": "no_such_device", "reason": "no such device"}]
    # Only `off_limits` names a domain: every other code is about the device, not its kind.
    assert "domain" not in out["refused"][0]


@pytest.mark.parametrize(("message", "reason"), [("store is read-only", "store is read-only"), ("", "could not be changed")])
async def test_a_change_home_assistant_rejects_is_failed(
    core: HomeAssistant, monkeypatch: pytest.MonkeyPatch, message: str, reason: str
) -> None:
    """The one refusal `_refusal` cannot predict: Home Assistant itself raising on the write.

    `reason` is whatever Home Assistant said, which is exactly why the app renders the code instead.
    """
    good = _register(core, "light", "hallway")
    broken = _register(core, "light", "porch")
    real = exposure.async_expose_entity

    def expose(hass: HomeAssistant, assistant: str, entity_id: str, should_expose: bool) -> None:
        if entity_id == broken:
            raise RuntimeError(message)
        real(hass, assistant, entity_id, should_expose)

    monkeypatch.setattr(exposure, "async_expose_entity", expose)
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": [good, broken], "expose": False})
    assert out["changed"] == [good]
    assert out["refused"] == [{"entity_id": broken, "code": "failed", "reason": reason}]


def test_refusal_codes_match_shared() -> None:
    """The box's declared codes must equal the hub's enum (packages/shared `EXPOSURE_REFUSAL_CODES`).

    A code only the box knows reaches the app as no code at all — the hub's schema drops it to the
    generic line — so drift fails quietly in production and has to fail loudly here. This pins the
    declared list; the next test pins what the handler actually sends against it.
    """
    assert SCHEMA.is_file(), "run `pnpm --filter @hearth/shared export:jsonschema` first"
    refused = json.loads(SCHEMA.read_text())["methods"]["entities.expose"]["result"]["properties"]["refused"]
    assert refused["items"]["properties"]["code"]["enum"] == list(REFUSAL_CODES)


async def test_every_refusal_sends_a_declared_code(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """One of each refusal in one request: the codes sent are exactly `REFUSAL_CODES`.

    The codes are literals at each `return`, so renaming one there (and in its own test) would leave
    the declared list — and the mirror test above — still passing while the hub dropped the code.
    """
    broken = _register(core, "light", "porch")
    ids = [
        "NotAnId",
        _register(core, "lock", "front"),
        "light.does_not_exist",
        _register(core, "light", "spare", disabled_by=er.RegistryEntryDisabler.USER),
        _register(core, "light", "tucked", hidden_by=er.RegistryEntryHider.USER),
        broken,
    ]

    def expose(hass: HomeAssistant, assistant: str, entity_id: str, should_expose: bool) -> None:
        raise RuntimeError("store is read-only")

    monkeypatch.setattr(exposure, "async_expose_entity", expose)
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": ids, "expose": True})
    assert out["changed"] == []
    assert [r["code"] for r in out["refused"]] == list(REFUSAL_CODES)


async def test_refuses_a_disabled_or_hidden_entity(core: HomeAssistant) -> None:
    disabled = _register(core, "light", "spare", disabled_by=er.RegistryEntryDisabler.USER)
    hidden = _register(core, "light", "tucked", hidden_by=er.RegistryEntryHider.USER)
    d = build_dispatcher(core)
    out = await d.dispatch("entities.expose", {"entity_ids": [disabled, hidden], "expose": True})
    assert out["changed"] == []
    assert {r["entity_id"]: r["code"] for r in out["refused"]} == {disabled: "disabled", hidden: "hidden"}
    # And they are not offered in the first place.
    rows = await d.dispatch("entities.exposable", {})
    assert {disabled, hidden} & {r["entity_id"] for r in rows} == set()


async def test_paging_advances(core: HomeAssistant) -> None:
    for i in range(5):
        _register(core, "light", f"lamp{i}")
    d = build_dispatcher(core)
    first = await d.dispatch("entities.exposable", {"limit": 2})
    assert len(first) == 2
    second = await d.dispatch("entities.exposable", {"limit": 2, "after": first[-1]["entity_id"]})
    assert second and second[0]["entity_id"] > first[-1]["entity_id"]


async def test_exposure_is_what_the_rest_of_the_surface_filters_on(core: HomeAssistant) -> None:
    """The point of the whole feature, shown in both directions.

    Un-sharing is the half that matters most: it is the only way from the app to take a device back
    out of everything Hearth can see, the assistant's inventory included.
    """
    light = _register(core, "light", "hallway")
    d = build_dispatcher(core)
    assert light in {e["entity_id"] for e in await d.dispatch("entities.list", {})}

    await d.dispatch("entities.expose", {"entity_ids": [light], "expose": False})
    assert light not in {e["entity_id"] for e in await d.dispatch("entities.list", {})}

    await d.dispatch("entities.expose", {"entity_ids": [light], "expose": True})
    assert light in {e["entity_id"] for e in await d.dispatch("entities.list", {})}


async def test_says_which_device_and_what_it_is_there_and_filters_by_device(core: HomeAssistant) -> None:
    """AgDR-0035: enough to notice a sensor whose settings are shared and whose reading is not."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(core)
    devices = dr.async_get(core)
    sensor = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "presence")}, name="Presence Sensor")
    other = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "plug")}, name="Plug")
    reading = _register(core, "binary_sensor", "presence", device_id=sensor.id, config_entry=entry)
    setting = _register(core, "number", "fading_time", device_id=sensor.id, config_entry=entry, entity_category=EntityCategory.CONFIG)
    _register(core, "switch", "plug", device_id=other.id, config_entry=entry)
    loose = _register(core, "light", "loose")

    d = build_dispatcher(core)
    rows = {r["entity_id"]: r for r in await d.dispatch("entities.exposable", {})}
    assert (rows[reading]["device_id"], rows[reading]["entity_category"]) == (sensor.id, None)
    assert (rows[setting]["device_id"], rows[setting]["entity_category"]) == (sensor.id, "config")
    assert (rows[loose]["device_id"], rows[loose]["entity_category"]) == (None, None)
    assert "state" not in rows[reading] and "attributes" not in rows[reading]

    only = await d.dispatch("entities.exposable", {"device_id": sensor.id})
    assert sorted(r["entity_id"] for r in only) == sorted([reading, setting])
