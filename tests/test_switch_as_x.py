"""A switch Home Assistant presents as a lock, siren or valve is that lock, siren or valve here (#643).

`switch_as_x` wraps a switch as another domain and leaves the switch with its own services, so
`switch.turn_on` on the source moves the lock the helper shows. Every row sets up real wrappers, so
the wrapper entities, the source's `hidden_by` and the copied expose settings are Home Assistant's.
Where a row needs the source to reach a reader at all, it un-hides and exposes it and asserts both,
so the refusal is the gate's and not the registry clause's.

Each refusal of something the model must not see is compared with `switch.ghost`'s, the id swapped
for a placeholder, so a lock's relay answers in exactly the words of an id that does not exist.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity, async_should_expose
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er, label_registry as lr
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai import labels as hl, subscriber as sub
from custom_components.hearth_ai.handlers import history, organise, registry, scripts
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

GHOST = "switch.ghost"


async def _source(hass: HomeAssistant, object_id: str) -> str:
    entity_id = f"switch.{object_id}"
    er.async_get(hass).async_get_or_create("switch", "hearth_test", object_id, suggested_object_id=object_id)
    hass.states.async_set(entity_id, "off")
    async_expose_entity(hass, "conversation", entity_id, True)
    return entity_id


async def _wrap(hass: HomeAssistant, source: str, target: str, *, by_uuid: bool = False, disabled: bool = False) -> MockConfigEntry:
    """A `switch_as_x` entry, set up by Home Assistant unless it is disabled."""
    value = er.async_get(hass).async_get(source).id if by_uuid else source
    entry = MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": value, "invert": False, "target_domain": target},
        title=f"{source} as {target}",
        version=1,
        minor_version=3,
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    if not disabled:
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _wrapper(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    (ent,) = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return ent.entity_id


def _bare(hass: HomeAssistant, entity_id: str) -> None:
    """Un-hide and expose `entity_id`, and assert both, so only the gate is left to refuse it."""
    reg = er.async_get(hass)
    reg.async_update_entity(entity_id, hidden_by=None)
    async_expose_entity(hass, "conversation", entity_id, True)
    ent = reg.async_get(entity_id)
    assert ent is not None and ent.hidden_by is None and ent.disabled_by is None, entity_id
    assert async_should_expose(hass, "conversation", entity_id), entity_id
    assert hass.states.get(entity_id) is not None, entity_id


async def _err(call: Any) -> tuple[str, str] | None:
    try:
        await call
    except RpcError as e:
        return (e.code, e.message)
    return None


def _sync_err(call: Any) -> tuple[str, str] | None:
    try:
        call()
    except RpcError as e:
        return (e.code, e.message)
    return None


def _mock_switch(hass: HomeAssistant, *services: str) -> list[Any]:
    """Every call to these `switch.*` services, in one list that keeps filling.

    `async_mock_service` hands back the list it appends to, so adding two of them together copies them,
    and an `assert not calls` on the copy holds whatever happens.
    """
    calls: list[Any] = []

    @callback
    def log(call: ServiceCall) -> None:
        calls.append(call)

    for service in services:
        hass.services.async_register("switch", service, log)
    return calls


def _calls(calls: list[Any]) -> list[tuple[str, list[str]]]:
    """Each call's service and ids; a wrapper sends one id as a string, `devices.call` sends a list."""
    return [(c.service, [c.data["entity_id"]] if isinstance(c.data["entity_id"], str) else c.data["entity_id"]) for c in calls]


def _as(answer: tuple[str, str] | None, entity_id: str) -> tuple[str, str] | None:
    return None if answer is None else (answer[0], answer[1].replace(entity_id, "<id>"))


async def _reads(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """What each reader that refuses by domain answers for `entity_id`, its id swapped out."""
    d = build_dispatcher(hass)
    domain = entity_id.split(".", 1)[0]
    service = {"switch": "turn_on", "cover": "close_cover", "valve": "close_valve"}.get(domain, "turn_on")
    # `devices.call` answers a missing service before it reads any id, which would hide the answer.
    assert hass.services.has_service(domain, service), f"{domain}.{service}"
    return {
        "states.get": _as(await _err(d.dispatch("states.get", {"entity_id": entity_id})), entity_id),
        "states.history": _as(_sync_err(lambda: history._entity(hass, entity_id)), entity_id),
        "labels": _as(_sync_err(lambda: organise._targets(hass, {"entity_ids": [entity_id]})), entity_id),
        "devices.call": _as(await _err(d.dispatch("devices.call", {"domain": domain, "service": service, "entity_id": [entity_id]})), entity_id),
    }


GHOST_WORDS = {k: ("not_found", "unknown entity <id>") for k in ("states.get", "states.history", "labels", "devices.call")}


async def _listed(hass: HomeAssistant) -> set[str]:
    return {e["entity_id"] for e in await build_dispatcher(hass).dispatch("entities.list", {})}


async def _pushed(hass: HomeAssistant, entity_ids: list[str]) -> set[str]:
    pushed: set[str] = set()

    async def send(batch: list[dict]) -> None:
        pushed.update(e["entity_id"] for e in batch)

    s = sub.StateSubscriber(hass, send)
    s.start()
    try:
        for entity_id in entity_ids:
            hass.states.async_set(entity_id, "on" if entity_id.startswith("switch.") else "open")
        await hass.async_block_till_done()
        await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
        await hass.async_block_till_done()
    finally:
        s.stop()
    return pushed


async def test_the_ghost_answers_every_reader_in_the_words_compared_against(core: HomeAssistant) -> None:
    """The reference every other row compares with: a ghost id is refused by all four readers."""
    _mock_switch(core, "turn_on")
    assert await _reads(core, GHOST) == GHOST_WORDS


# --------------------------------------------------------------------------- lock: hidden


async def test_a_lock_wrapped_switch_answers_like_a_ghost_everywhere(core: HomeAssistant) -> None:
    """Exposed and un-hidden, the relay under a lock is still refused by every reader, and never listed or pushed."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await _wrap(core, lamp, "light")
    _bare(core, relay)
    _bare(core, lamp)
    calls = _mock_switch(core, "turn_on")

    assert await _reads(core, relay) == GHOST_WORDS
    assert not calls
    # Control: a `light` wrapper gates nothing, so its switch is read and operated as before.
    assert await _reads(core, lamp) == dict.fromkeys(GHOST_WORDS)
    assert _calls(calls) == [("turn_on", [lamp])]

    listed = await _listed(core)
    assert relay not in listed and lamp in listed
    pushed = await _pushed(core, [relay, lamp])
    assert relay not in pushed and lamp in pushed


@pytest.mark.parametrize(("domain", "service"), [("switch", "turn_on"), ("switch", "toggle"), ("homeassistant", "turn_on")])
async def test_a_lock_wrapped_switch_is_refused_through_any_service(core: HomeAssistant, domain: str, service: str) -> None:
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    _bare(core, relay)
    calls = _mock_switch(core, "turn_on", "toggle")

    got = await _err(build_dispatcher(core).dispatch("devices.call", {"domain": domain, "service": service, "entity_id": [relay]}))
    assert _as(got, relay) == ("not_found", "unknown entity <id>")
    assert not calls


@pytest.mark.parametrize("form", ["uuid", "disabled"])
async def test_a_lock_wrapper_gates_by_registry_id_and_while_disabled(core: HomeAssistant, form: str) -> None:
    """The entry may name its source by registry id, and a disabled wrapper still has a relay under it."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock", by_uuid=form == "uuid", disabled=form == "disabled")
    _bare(core, relay)
    _mock_switch(core, "turn_on")

    assert await _reads(core, relay) == GHOST_WORDS
    assert relay not in await _listed(core)


# --------------------------------------------------------------------------- ruling 5: one gate per source


async def test_a_cover_beside_a_lock_is_the_lock(core: HomeAssistant) -> None:
    """Ruling 5: the cover drives the lock's relay, so it is hidden like the lock, listed nowhere and pushed nowhere.

    Pins the gate to the source, not the entry: computed per entry, the cover is an ungated cover.
    """
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    cover = _wrapper(core, await _wrap(core, relay, "cover"))
    _bare(core, cover)

    assert await _reads(core, cover) == GHOST_WORDS
    assert cover not in await _listed(core)
    assert cover not in await _pushed(core, [cover])


async def test_a_valve_beside_a_lock_does_not_close(core: HomeAssistant) -> None:
    """Ruling 5: #527's closing rule is about valves, and this relay is also a lock's."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    valve = _wrapper(core, await _wrap(core, relay, "valve"))
    _bare(core, valve)
    calls = _mock_switch(core, "turn_on", "turn_off")

    assert await _reads(core, valve) == GHOST_WORDS
    assert not calls


async def test_a_cover_beside_a_valve_is_refused_and_the_valve_still_closes(core: HomeAssistant) -> None:
    """Ruling 5 with ruling 1: the cover is the valve, refused both ways; `valve.close_valve` still closes it."""
    tap = await _source(core, "tap")
    valve = _wrapper(core, await _wrap(core, tap, "valve"))
    cover = _wrapper(core, await _wrap(core, tap, "cover"))
    _bare(core, cover)
    _bare(core, valve)
    switched = _mock_switch(core, "turn_on", "turn_off")
    d = build_dispatcher(core)

    for service in ("close_cover", "open_cover"):
        got = await _err(d.dispatch("devices.call", {"domain": "cover", "service": service, "entity_id": [cover]}))
        assert got is not None and got[0] == "method_not_allowed", (service, got)
    assert not switched

    assert await _err(d.dispatch("devices.call", {"domain": "valve", "service": "close_valve", "entity_id": [valve]})) is None
    assert _calls(switched) == [("turn_off", [tap])]


async def test_a_cover_alone_changes_nothing(core: HomeAssistant) -> None:
    """Control for ruling 5: a source with only an ungated wrapper behaves as it did."""
    relay = await _source(core, "relay")
    cover = _wrapper(core, await _wrap(core, relay, "cover"))
    _bare(core, cover)
    _bare(core, relay)
    calls = _mock_switch(core, "turn_on", "turn_off")

    assert await _reads(core, cover) == dict.fromkeys(GHOST_WORDS)
    assert await _reads(core, relay) == dict.fromkeys(GHOST_WORDS)
    assert _calls(calls) == [("turn_off", [relay]), ("turn_on", [relay])]


# --------------------------------------------------------------------------- siren: hidden like a siren; valve: refused, not hidden


@pytest.mark.parametrize(("domain", "service"), [("switch", "turn_on"), ("switch", "turn_off"), ("switch", "toggle"), ("homeassistant", "turn_on")])
@pytest.mark.parametrize("target", ["siren", "valve"])
async def test_a_siren_or_valve_wrapped_switch_is_refused_through_any_service(core: HomeAssistant, target: str, domain: str, service: str) -> None:
    """Keyed on the id, not on `switch.*`: `homeassistant.turn_on` reaches the relay too. Both directions on a valve (ruling 1).

    Since #605 a siren is in `OFF_LIMITS`, so a siren's relay is hidden like the siren and answers in a
    ghost's words; a valve is not, so its relay is visible and refused by the gate."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, target)
    _bare(core, relay)
    calls = _mock_switch(core, "turn_on", "turn_off", "toggle")

    got = await _err(build_dispatcher(core).dispatch("devices.call", {"domain": domain, "service": service, "entity_id": [relay]}))
    if target == "siren":
        assert _as(got, relay) == GHOST_WORDS["devices.call"], got
    else:
        assert got is not None and got[0] == "method_not_allowed", got
    assert not calls


async def test_a_siren_wrapped_switch_is_kept_out_of_history_and_labels(core: HomeAssistant) -> None:
    """The siren's own rule: off limits to history and labels (and, since #605, every other reader), so its relay is too. A valve's relay is not."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    await _wrap(core, relay, "siren")
    await _wrap(core, tap, "valve")
    _bare(core, relay)
    _bare(core, tap)
    _mock_switch(core, "turn_on")

    reads = await _reads(core, relay)
    assert (reads["states.history"], reads["labels"]) == (GHOST_WORDS["states.history"], GHOST_WORDS["labels"])
    reads = await _reads(core, tap)
    assert (reads["states.history"], reads["labels"]) == (None, None)


# --------------------------------------------------------------------------- what names a gated id


async def test_helpers_list_and_labels_delete_do_not_name_a_cover_beside_a_lock(core: HomeAssistant) -> None:
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    cover_entry = await _wrap(core, relay, "cover")
    cover = _wrapper(core, cover_entry)
    light = _wrapper(core, await _wrap(core, lamp, "light"))
    d = build_dispatcher(core)

    rows = {r["id"]: r for r in await d.dispatch("helpers.list", {}) if r.get("kind") == "flow"}
    assert (rows[cover_entry.entry_id]["entities"], rows[cover_entry.entry_id]["restricted"]) == ([], True)
    assert any(r["entities"] == [light] for r in rows.values())

    label = lr.async_get(core).async_create("Tagged")
    reg = er.async_get(core)
    for entity_id in (cover, relay, light):
        reg.async_update_entity(entity_id, labels={label.label_id})
    assert (await d.dispatch("labels.delete", {"label_id": label.label_id}))["removed_from"] == [light]


async def test_the_owner_cannot_share_a_gated_wrapper(core: HomeAssistant) -> None:
    """`entities.expose` refuses the relay and the cover beside its lock; `entities.exposable` offers neither."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    cover = _wrapper(core, await _wrap(core, relay, "cover"))
    light = _wrapper(core, await _wrap(core, lamp, "light"))
    for entity_id in (relay, cover, light):
        async_expose_entity(core, "conversation", entity_id, False)
    d = build_dispatcher(core)

    out = await d.dispatch("entities.expose", {"entity_ids": [relay, cover, light], "expose": True})
    assert out["changed"] == [light]
    assert {(r["entity_id"], r["code"], r.get("domain")) for r in out["refused"]} == {(relay, "off_limits", "lock"), (cover, "off_limits", "lock")}

    async_expose_entity(core, "conversation", light, False)
    offered = {r["entity_id"]: r for r in await d.dispatch("entities.exposable", {})}
    assert relay not in offered and cover not in offered and light in offered


# --------------------------------------------------------------------------- the wrapper's lifecycle


@pytest.mark.parametrize("which", ["lock", "cover"])
async def test_helpers_delete_refuses_a_wrapper_on_a_gated_switch(core: HomeAssistant, which: str) -> None:
    """Ruling 3. Deleting the lock wrapper would hand the relay back as a plain switch; the cover carries the lock's gate."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    entries = {"lock": await _wrap(core, relay, "lock"), "cover": await _wrap(core, relay, "cover")}
    plain = await _wrap(core, lamp, "cover")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("helpers.delete", {"id": entries[which].entry_id}))
    assert got is not None and got[0] == "method_not_allowed", got
    assert core.config_entries.async_get_entry(entries[which].entry_id) is not None
    # Control: a cover on an ungated switch is deleted as before.
    assert await _err(d.dispatch("helpers.delete", {"id": plain.entry_id})) is None
    assert core.config_entries.async_get_entry(plain.entry_id) is None


async def _create(hass: HomeAssistant, path: str, source: str) -> tuple[str, str] | None:
    d = build_dispatcher(hass)
    config = {"entity_id": source, "target_domain": "cover"}
    if path == "helpers.create":
        return await _err(d.dispatch("helpers.create", {"domain": "switch_as_x", "config": config}))
    started = await d.dispatch("integrations.flow_start", {"domain": "switch_as_x"})
    try:
        return await _err(d.dispatch("integrations.flow_step", {"flow_id": started["flow_id"], "input": config}))
    finally:
        await _err(d.dispatch("integrations.flow_abort", {"flow_id": started["flow_id"]}))


@pytest.mark.parametrize("path", ["helpers.create", "integrations.flow_step"])
async def test_a_wrapper_flow_refuses_a_source_the_model_cannot_see_in_a_ghosts_words(core: HomeAssistant, path: str) -> None:
    """Ruling 4. `helpers.list` shows a lock wrapper with no source id, so its relay answers like a ghost,
    and so does a hidden or unexposed switch. Nothing is created for any of them."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    _bare(core, relay)
    hidden = await _source(core, "hidden")
    er.async_get(core).async_update_entity(hidden, hidden_by=er.RegistryEntryHider.USER)
    unexposed = await _source(core, "unexposed")
    async_expose_entity(core, "conversation", unexposed, False)
    relay_uuid = er.async_get(core).async_get(relay).id
    before = len(core.config_entries.async_entries("switch_as_x"))

    for source in (GHOST, relay, relay_uuid, hidden, unexposed):
        assert _as(await _create(core, path, source), source) == ("not_found", "unknown entity <id>"), source
    assert len(core.config_entries.async_entries("switch_as_x")) == before


@pytest.mark.parametrize("path", ["helpers.create", "integrations.flow_step"])
async def test_a_wrapper_flow_refuses_a_siren_source_in_a_ghosts_words_and_wraps_a_plain_switch(core: HomeAssistant, path: str) -> None:
    """Since #605 a siren is in `OFF_LIMITS`, so a siren's relay is hidden like the siren: even un-hidden
    and exposed, a wrapper flow over it answers like a ghost and creates nothing."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "siren")
    _bare(core, relay)
    lamp = await _source(core, "lamp")
    before = len(core.config_entries.async_entries("switch_as_x"))

    got = await _create(core, path, relay)
    assert _as(got, relay) == ("not_found", "unknown entity <id>"), got
    assert len(core.config_entries.async_entries("switch_as_x")) == before
    # Control: a switch the model can see, and no gate holds, is wrapped as before.
    assert await _create(core, path, lamp) is None
    assert len(core.config_entries.async_entries("switch_as_x")) == before + 1


# --------------------------------------------------------------------------- groups over a gated switch


async def _group(hass: HomeAssistant, group_type: str, members: list[str], name: str) -> str:
    """A `group` helper, set up by Home Assistant, and its entity exposed."""
    entry = MockConfigEntry(
        domain="group",
        options={"group_type": group_type, "entities": members, "name": name, "hide_members": False, "all": False},
        title=name,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (ent,) = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    _bare(hass, ent.entity_id)
    assert hass.states.get(ent.entity_id).attributes.get("entity_id") == members
    return ent.entity_id


def _forwarded(hass: HomeAssistant) -> list[list[str]]:
    """The ids of every `switch.*` call Home Assistant itself makes, read off the bus."""
    seen: list[list[str]] = []

    @callback
    def log(event: Any) -> None:
        if event.data.get("domain") == "switch":
            ids = event.data.get("service_data", {}).get("entity_id")
            seen.append([ids] if isinstance(ids, str) else list(ids or []))

    hass.bus.async_listen("call_service", log)
    return seen


async def test_a_switch_group_over_a_lock_relay_is_the_lock(core: HomeAssistant) -> None:
    """A group forwards `switch.turn_on` to its members, so a group over the relay answers like a ghost
    and forwards nothing; a group of that group too. Control: a group over a plain switch is operated
    and forwards to its member."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    grp = await _group(core, "switch", [relay], "Door group")
    outer = await _group(core, "switch", [grp], "Outer group")
    plain = await _group(core, "switch", [lamp], "Lamp group")
    seen = _forwarded(core)

    assert await _reads(core, grp) == GHOST_WORDS
    assert await _reads(core, outer) == GHOST_WORDS
    assert not [ids for ids in seen if relay in ids]
    listed = await _listed(core)
    assert grp not in listed and outer not in listed and plain in listed

    assert await _reads(core, plain) == dict.fromkeys(GHOST_WORDS)
    await core.async_block_till_done()
    assert [lamp] in seen


async def test_a_light_group_over_a_light_beside_a_lock_is_the_lock(core: HomeAssistant) -> None:
    """Ruling 5 through a group: the light wrapper carries the lock's gate, and so does a group over it."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    light = _wrapper(core, await _wrap(core, relay, "light"))
    grp = await _group(core, "light", [light], "Lights")
    _mock_switch(core, "turn_on")

    reads = await _reads(core, grp)
    assert reads == GHOST_WORDS


async def test_a_switch_group_over_a_valve_relay_is_refused_both_ways(core: HomeAssistant) -> None:
    tap = await _source(core, "tap")
    await _wrap(core, tap, "valve")
    grp = await _group(core, "switch", [tap], "Tap group")
    seen = _forwarded(core)
    d = build_dispatcher(core)

    for service in ("turn_on", "turn_off"):
        got = await _err(d.dispatch("devices.call", {"domain": "switch", "service": service, "entity_id": [grp]}))
        assert got is not None and got[0] == "method_not_allowed", (service, got)
    await core.async_block_till_done()
    assert not [ids for ids in seen if tap in ids]


async def test_a_group_takes_the_strictest_gate_of_its_members(core: HomeAssistant) -> None:
    """Over a lock relay and a valve relay, a group is the lock: hidden, not merely refused."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    await _wrap(core, tap, "valve")
    await _wrap(core, relay, "lock")
    grp = await _group(core, "switch", [tap, relay], "Both")
    _mock_switch(core, "turn_on")

    assert await _reads(core, grp) == GHOST_WORDS


async def test_a_valve_on_a_group_over_a_lock_relay_is_the_lock(core: HomeAssistant) -> None:
    """The strictest gate the walk reaches, not the first: the group's own wrapper is a valve, found
    before the lock under it, and the group is still the lock."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    grp = await _group(core, "switch", [relay], "Door group")
    await _wrap(core, grp, "valve")
    _bare(core, grp)
    _mock_switch(core, "turn_on")

    assert await _reads(core, grp) == GHOST_WORDS


async def test_visible_hides_a_lock_wrapper_whatever_gate_it_is_given(core: HomeAssistant) -> None:
    """Ruling 8: the literal domain is checked as well as the effective one, so a gate map that ranked
    a lock's relay as something weaker still could not surface the `lock.*` wrapper itself."""
    relay = await _source(core, "relay")
    lock = _wrapper(core, await _wrap(core, relay, "lock"))
    _bare(core, lock)

    class Weaker(registry.GateMap):
        def get(self, entity_id: str) -> str | None:
            return "siren"

    assert not registry.visible(core, lock, er.async_get(core).async_get(lock), Weaker(core))


async def test_a_cover_over_a_group_over_a_lock_relay_is_the_lock(core: HomeAssistant) -> None:
    """Ruling 8: membership and `switch_as_x` in one walk. The cover's source is a group, and the group
    is the lock only through its member, so a walk of either relation alone leaves the cover ungated."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    grp = await _group(core, "switch", [relay], "Door group")
    cover = _wrapper(core, await _wrap(core, grp, "cover"))
    _bare(core, cover)
    seen = _forwarded(core)

    assert await _reads(core, cover) == GHOST_WORDS
    assert cover not in await _listed(core)
    await core.async_block_till_done()
    assert not [ids for ids in seen if relay in ids or grp in ids]


async def test_a_changed_membership_counts_on_the_next_read(core: HomeAssistant) -> None:
    """Ruling 8: nothing caches the gates across calls. A group read while it holds only the lamp, then
    given the relay, is refused on the very next read; given back only the lamp, it is read again."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    core.states.async_set("group.outside", "off", {"entity_id": [lamp]})
    async_expose_entity(core, "conversation", "group.outside", True)
    d = build_dispatcher(core)

    async def read() -> tuple[str, str] | None:
        return _as(await _err(d.dispatch("states.get", {"entity_id": "group.outside"})), "group.outside")

    assert await read() is None and "group.outside" in await _listed(core)
    core.states.async_set("group.outside", "off", {"entity_id": [lamp, relay]})
    assert await read() == ("not_found", "unknown entity <id>") and "group.outside" not in await _listed(core)
    core.states.async_set("group.outside", "off", {"entity_id": [lamp]})
    assert await read() is None


async def test_an_old_style_group_over_a_scene_that_sets_the_relay_is_the_lock(core: HomeAssistant) -> None:
    """`homeassistant.turn_on` on an old-style group activates a scene in it, around the scene's own run
    check, so a scene is walked when it is a group's member. Asked about itself it is not: it stays
    listed and its own run check refuses it."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    core.states.async_set("scene.door", "unknown", {"entity_id": [relay]})
    core.states.async_set("group.evening", "off", {"entity_id": ["scene.door"]})
    for entity_id in ("scene.door", "group.evening"):
        async_expose_entity(core, "conversation", entity_id, True)
    scenes = async_mock_service(core, "scene", "turn_on")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("devices.call", {"domain": "homeassistant", "service": "turn_on", "entity_id": ["group.evening"]}))
    assert _as(got, "group.evening") == ("not_found", "unknown entity <id>")
    assert not scenes
    # Asked about itself, the scene is read as before; activating it is the run check's to refuse.
    assert (await d.dispatch("states.get", {"entity_id": "scene.door"}))["entity_id"] == "scene.door"
    assert "scene.door" in {r["entity_id"] for r in await d.dispatch("scenes.list", {})}


async def test_group_set_is_not_reachable_through_devices_call(core: HomeAssistant) -> None:
    """Sweep row for ruling 8: `group.set` takes an `object_id` and no `entity_id`, and `devices.call`
    always sends one, so Home Assistant's schema refuses the call and no membership changes. A group
    the model could widen this way would still be gated at read time, which is the backstop."""
    assert await async_setup_component(core, "group", {})
    lamp = await _source(core, "lamp")
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    core.states.async_set("group.outside", "off", {"entity_id": [lamp]})
    async_expose_entity(core, "conversation", "group.outside", True)
    d = build_dispatcher(core)
    assert core.services.has_service("group", "set")

    for service, data in [("set", {"object_id": "outside", "add_entities": [relay]}), ("remove", {"object_id": "outside"}), ("reload", {})]:
        got = await _err(d.dispatch("devices.call", {"domain": "group", "service": service, "entity_id": ["group.outside"], "data": data}))
        assert got is not None, service
        assert core.states.get("group.outside").attributes["entity_id"] == [lamp], service


# --------------------------------------------------------------------------- ruling 8: the order of the gates


async def test_a_lock_beside_a_siren_is_the_lock(core: HomeAssistant) -> None:
    """Lock before siren. Since #605 a siren's relay is hidden as well, so the readers answer the same
    either way; the owner's `entities.expose` still says which gate won, so the order is pinned there."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "siren")
    lock = _wrapper(core, await _wrap(core, relay, "lock"))
    _bare(core, relay)
    _mock_switch(core, "turn_on")

    assert await _reads(core, relay) == GHOST_WORDS
    assert lock not in await _listed(core) and relay not in await _listed(core)
    async_expose_entity(core, "conversation", relay, False)
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": [relay], "expose": True})
    assert [(r["entity_id"], r["code"], r.get("domain")) for r in out["refused"]] == [(relay, "off_limits", "lock")]


async def test_a_valve_beside_a_siren_does_not_close(core: HomeAssistant) -> None:
    """Siren before valve. With the two swapped the valve wrapper closes and the relay's history reads.
    Since #605 the siren gate hides the valve wrapper as well, so it is refused in a ghost's words."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "siren")
    valve = _wrapper(core, await _wrap(core, relay, "valve"))
    _bare(core, valve)
    _bare(core, relay)
    switched = _mock_switch(core, "turn_on", "turn_off")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("devices.call", {"domain": "valve", "service": "close_valve", "entity_id": [valve]}))
    assert _as(got, valve) == GHOST_WORDS["devices.call"], got
    assert not switched
    assert (await _reads(core, relay))["states.history"] == GHOST_WORDS["states.history"]


# --------------------------------------------------------------------------- ruling 7: run checks


async def test_an_owner_scene_that_sets_a_gated_relay_is_not_run(core: HomeAssistant) -> None:
    """The run check is the only one an owner's scene gets. A scene setting the lock relay, or a group
    over it, is refused naming neither the lock nor the relay, and listed with `can_run: false`. One
    setting a valve relay is refused whichever way it sets it. Control: a scene setting a plain switch runs."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await _wrap(core, tap, "valve")
    grp = await _group(core, "switch", [relay], "Door group")
    scenes = {"scene.door": [relay], "scene.door_group": [grp], "scene.water": [tap], "scene.lamp": [lamp]}
    for scene, members in scenes.items():
        core.states.async_set(scene, "unknown", {"entity_id": members})
        async_expose_entity(core, "conversation", scene, True)
    ran = async_mock_service(core, "scene", "turn_on")
    d = build_dispatcher(core)

    for scene in ("scene.door", "scene.door_group"):
        got = await _err(d.dispatch("scenes.activate", {"entity_id": scene}))
        assert got is not None and got[0] == "method_not_allowed", (scene, got)
        assert "lock" not in got[1] and relay not in got[1] and grp not in got[1], got
    got = await _err(d.dispatch("scenes.activate", {"entity_id": "scene.water"}))
    assert got is not None and got[0] == "method_not_allowed" and tap in got[1], got
    assert not ran

    can_run = {r["entity_id"]: r["can_run"] for r in await d.dispatch("scenes.list", {})}
    assert can_run == {"scene.door": False, "scene.door_group": False, "scene.water": False, "scene.lamp": True}
    assert await _err(d.dispatch("scenes.activate", {"entity_id": "scene.lamp"})) is None
    assert [c.data["entity_id"] for c in ran] == ["scene.lamp"]


async def test_a_scene_script_that_sets_the_lock_relay_is_not_run(core: HomeAssistant, tmp_path: Any) -> None:
    """Ruling 7: a script Hearth presents as a scene is checked when it runs, and the check was literal.
    Control: the same script on a plain switch runs."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await hl.async_ensure_labels(core)
    (tmp_path / "scripts.yaml").write_text(
        "door:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: " + relay + "\n"
        "lamp:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: " + lamp + "\n"
    )
    assert await async_setup_component(core, "script", {})
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    switched = _mock_switch(core, "turn_on")
    d = build_dispatcher(core)
    for script in ("script.door", "script.lamp"):
        async_expose_entity(core, "conversation", script, True)
        await hl.async_mark_hearth_scene(core, script, True)

    got = await _err(d.dispatch("scripts.run", {"entity_id": "script.door"}))
    assert got is not None and got[0] == "method_not_allowed", got
    assert "lock" not in got[1] and relay not in got[1], got
    assert not switched
    assert await _err(d.dispatch("scripts.run", {"entity_id": "script.lamp"})) is None
    assert _calls(switched) == [("turn_on", [lamp])]


SCENE_REFUSAL = "this scene sets something Hearth does not operate, so it cannot be activated"


@pytest.mark.parametrize("kind", ["scene", "script"])
async def test_a_routine_that_sets_a_siren_relay_is_refused_without_naming_it(core: HomeAssistant, tmp_path: Any, kind: str) -> None:
    """Since #605 a siren's relay answers every reader like an unknown id, so the run check must not
    name it either: the lock's generic sentence, with neither the relay nor "siren" in it. Control: a
    valve's relay, whose wrapper is still listed, is still named."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    await _wrap(core, relay, "siren")
    await _wrap(core, tap, "valve")
    d = build_dispatcher(core)
    if kind == "scene":
        for scene, member in (("scene.alarm", relay), ("scene.water", tap)):
            core.states.async_set(scene, "unknown", {"entity_id": [member]})
            async_expose_entity(core, "conversation", scene, True)
        ran = async_mock_service(core, "scene", "turn_on")
        method, siren_routine, valve_routine = "scenes.activate", "scene.alarm", "scene.water"
    else:
        await hl.async_ensure_labels(core)
        (tmp_path / "scripts.yaml").write_text(
            "alarm:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: " + relay + "\n"
            "water:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: " + tap + "\n"
        )
        assert await async_setup_component(core, "script", {})
        await core.services.async_call("script", "reload", blocking=True)
        await core.async_block_till_done()
        for script in ("script.alarm", "script.water"):
            async_expose_entity(core, "conversation", script, True)
            await hl.async_mark_hearth_scene(core, script, True)
        ran = _mock_switch(core, "turn_on")
        method, siren_routine, valve_routine = "scripts.run", "script.alarm", "script.water"

    got = await _err(d.dispatch(method, {"entity_id": siren_routine}))
    assert got is not None and got[0] == "method_not_allowed", got
    assert got[1] == SCENE_REFUSAL and relay not in got[1] and "siren" not in got[1], got
    got = await _err(d.dispatch(method, {"entity_id": valve_routine}))
    assert got is not None and got[0] == "method_not_allowed" and tap in got[1] and "valve" in got[1], got
    assert not ran


async def test_a_scenes_yaml_scene_with_no_member_attribute_is_checked_on_its_config(core: HomeAssistant, tmp_path: Any) -> None:
    """The config's own entity keys count too, for a scene whose state publishes no members."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    (tmp_path / "scenes.yaml").write_text("- id: door\n  name: Door\n  entities:\n    " + relay + ": 'on'\n")
    core.states.async_set("scene.door", "unknown", {"id": "door"})
    async_expose_entity(core, "conversation", "scene.door", True)
    ran = async_mock_service(core, "scene", "turn_on")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("scenes.activate", {"entity_id": "scene.door"}))
    assert got is not None and got[0] == "method_not_allowed" and relay not in got[1], got
    assert not ran


async def test_the_members_of_a_lock_wrapped_group_are_the_lock(core: HomeAssistant) -> None:
    """Ruling 9: a group wrapped as a lock is the lock, and each member drives part of it, so each
    answers like a ghost. Control: in a group that merely contains a gated relay, the porch switch
    beside it stays plain."""
    a = await _source(core, "a")
    b = await _source(core, "b")
    grp = await _group(core, "switch", [a, b], "Bolt group")
    await _wrap(core, grp, "lock")
    relay = await _source(core, "relay")
    porch = await _source(core, "porch")
    await _wrap(core, relay, "lock")
    await _group(core, "switch", [relay, porch], "All outside")
    for entity_id in (a, b, porch):
        _bare(core, entity_id)
    calls = _mock_switch(core, "turn_on")

    assert await _reads(core, a) == GHOST_WORDS
    assert await _reads(core, b) == GHOST_WORDS
    assert not calls
    assert await _reads(core, porch) == dict.fromkeys(GHOST_WORDS)
    assert _calls(calls) == [("turn_on", [porch])]


async def test_helpers_delete_refuses_a_group_a_lock_gate_rests_on(core: HomeAssistant) -> None:
    """Ruling 9's gate on the members is read off the lock-wrapped group's live `entity_id` attribute,
    so deleting the group's own entry would take it off them and `switch.turn_on` would reach the lock.
    Refused naming neither the lock nor a member; the group stays, and its member still answers like a
    ghost. Control: an unwrapped group over the lock's relay only reaches a gate, and is deleted."""
    a = await _source(core, "a")
    b = await _source(core, "b")
    grp = await _group(core, "switch", [a, b], "Bolt group")
    await _wrap(core, grp, "lock")
    relay = await _source(core, "relay")
    porch = await _source(core, "porch")
    await _wrap(core, relay, "lock")
    outside = await _group(core, "switch", [relay, porch], "All outside")
    _bare(core, a)
    calls = _mock_switch(core, "turn_on")
    reg = er.async_get(core)
    bolt_entry, outside_entry = reg.async_get(grp).config_entry_id, reg.async_get(outside).config_entry_id
    d = build_dispatcher(core)

    got = await _err(d.dispatch("helpers.delete", {"id": bolt_entry}))
    assert got is not None and got[0] == "method_not_allowed", got
    assert "lock" not in got[1] and a not in got[1] and b not in got[1] and grp not in got[1], got
    assert core.config_entries.async_get_entry(bolt_entry) is not None
    assert await _reads(core, a) == GHOST_WORDS
    assert not calls

    assert await _err(d.dispatch("helpers.delete", {"id": outside_entry})) is None
    assert core.config_entries.async_get_entry(outside_entry) is None


# --------------------------------------------------------------------------- ruling 10: targets the literal scan cannot see

_FORMS = {
    "template_id": "    target:\n      entity_id: \"{{ 'switch.' ~ 'relay' }}\"\n",
    "data_template": "    data_template: \"{{ {'entity_id': 'switch.relay'} }}\"\n",
    "all": "    target:\n      entity_id: all\n",
    "area": "    target:\n      area_id: porch\n",
}


async def _run_scene_script(hass: HomeAssistant, tmp_path: Any, body: str) -> tuple[str, str] | None:
    """`scripts.run` on a Hearth-labelled, exposed script whose one step is `switch.turn_on` + `body`."""
    await hl.async_ensure_labels(hass)
    (tmp_path / "scripts.yaml").write_text("probe:\n  sequence:\n  - action: switch.turn_on\n" + body)
    assert await async_setup_component(hass, "script", {})
    await hass.services.async_call("script", "reload", blocking=True)
    await hass.async_block_till_done()
    async_expose_entity(hass, "conversation", "script.probe", True)
    await hl.async_mark_hearth_scene(hass, "script.probe", True)
    got = await _err(build_dispatcher(hass).dispatch("scripts.run", {"entity_id": "script.probe"}))
    await hass.async_block_till_done()
    return got


async def _relay_on_the_porch(hass: HomeAssistant) -> str:
    relay = await _source(hass, "relay")
    area = ar.async_get(hass).async_create("Porch")
    assert area.id == "porch"
    er.async_get(hass).async_update_entity(relay, area_id=area.id)
    return relay


@pytest.mark.parametrize("form", list(_FORMS))
async def test_a_scene_script_whose_target_is_not_literal_is_refused_in_a_gated_home(core: HomeAssistant, tmp_path: Any, form: str) -> None:
    """Ruling 10. A templated id, a whole-template `data_template` and `all` cannot be known before the
    step runs, so they are refused outright, in words that name neither the relay nor the lock. An area
    target is expanded the way Home Assistant expands it, and reaches the un-hidden relay."""
    relay = await _relay_on_the_porch(core)
    await _wrap(core, relay, "lock")
    _bare(core, relay)
    calls = _mock_switch(core, "turn_on")

    got = await _run_scene_script(core, tmp_path, _FORMS[form])
    expected = SCENE_REFUSAL if form == "area" else scripts.UNKNOWABLE_TARGET
    assert got == ("method_not_allowed", expected), got
    assert relay not in got[1] and "lock" not in got[1], got
    assert not calls


@pytest.mark.parametrize("form", list(_FORMS))
async def test_a_scene_script_whose_target_is_not_literal_runs_in_a_home_with_no_gate(core: HomeAssistant, tmp_path: Any, form: str) -> None:
    """Control for the row above, ruling 2's home-wide condition: the same four forms over the same
    relay, with no gated wrapper anywhere, run and reach it."""
    relay = await _relay_on_the_porch(core)
    calls = _mock_switch(core, "turn_on")

    assert await _run_scene_script(core, tmp_path, _FORMS[form]) is None
    assert len(calls) == 1, calls


async def test_a_scene_script_naming_a_plain_switch_runs_in_a_gated_home(core: HomeAssistant, tmp_path: Any) -> None:
    """Control: a named switch no gate holds still runs beside a lock wrapper."""
    relay = await _relay_on_the_porch(core)
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    calls = _mock_switch(core, "turn_on")

    assert await _run_scene_script(core, tmp_path, f"    target:\n      entity_id: {lamp}\n") is None
    assert _calls(calls) == [("turn_on", [lamp])]
