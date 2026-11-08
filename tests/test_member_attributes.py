"""A group's members and what a scene sets never carry an off-limits id to a reader (#722).

Home Assistant publishes both as the entity's own `entity_id` attribute. `filter_attributes` passed
it through, so `states.get` on `group.noise`, `entities.list` and the state push named `lock.gate`
and `siren.hall` while every reader answered for those ids as for an unknown one. Off-limits ids are
dropped from the list and the rest kept, so a group of lights still says which lights. A switch a
`switch_as_x` helper presents as a lock is off-limits too (#643); one presented as a valve is not.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai import subscriber as sub
from custom_components.hearth_ai.handlers import registry
from custom_components.hearth_ai.rpc import build_dispatcher

LIGHT = "light.hall"
# One of each `OFF_LIMITS` kind: in both lists, only in `HIDDEN_DOMAINS`, only in the action policy's.
OFF = ["lock.gate", "siren.hall", "alarm_control_panel.house", "camera.porch", "person.me", "device_tracker.phone", "image.doorbell"]
RELAY = "switch.gate_relay"  # wrapped as a lock: off-limits by its effective domain
TAP = "switch.tap"  # wrapped as a valve: gated, but not off-limits, so it stays

GROUP = "group.noise"
SCENE = "scene.open_gate"
SINGLE = "scene.just_the_lock"  # a one-member attribute Home Assistant may publish as a string


async def _switch(hass: HomeAssistant, object_id: str) -> str:
    entity_id = f"switch.{object_id}"
    er.async_get(hass).async_get_or_create("switch", "hearth_test", object_id, suggested_object_id=object_id)
    hass.states.async_set(entity_id, "off")
    return entity_id


async def _wrap(hass: HomeAssistant, source: str, target: str) -> None:
    entry = MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": source, "invert": False, "target_domain": target},
        title=f"{source} as {target}",
        version=1,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _home(hass: HomeAssistant) -> None:
    await _wrap(hass, await _switch(hass, "gate_relay"), "lock")
    await _wrap(hass, await _switch(hass, "tap"), "valve")
    hass.states.async_set(LIGHT, "on")
    for eid in OFF:
        hass.states.async_set(eid, "on")
    reg = er.async_get(hass)
    for eid in (GROUP, SCENE, SINGLE):
        domain, object_id = eid.split(".", 1)
        reg.async_get_or_create(domain, "hearth_test", object_id, suggested_object_id=object_id)
        async_expose_entity(hass, "conversation", eid, True)
    _set(hass)
    await hass.async_block_till_done()
    # The premise: the relay is a lock to Hearth, the tap a valve, and the three carriers are readable.
    assert registry.effective_domain(hass, RELAY) == "lock"
    assert registry.effective_domain(hass, TAP) == "valve"
    assert all(registry.visible(hass, eid, reg.async_get(eid)) for eid in (GROUP, SCENE, SINGLE))


def _set(hass: HomeAssistant, state: str = "on") -> None:
    # A group of off-limits ids would itself be hidden if a relay were in it (its gate is the lock's),
    # so the relay rides in the scene, whose members do not lend it a gate.
    hass.states.async_set(GROUP, state, {"entity_id": [LIGHT, *OFF, TAP]})
    hass.states.async_set(SCENE, state, {"entity_id": (LIGHT, RELAY, "lock.gate", TAP, 42)})
    hass.states.async_set(SINGLE, state, {"entity_id": "lock.gate", "friendly_name": "Just the lock"})


WANT = {GROUP: [LIGHT, TAP], SCENE: [LIGHT, TAP], SINGLE: None}


def _members(attrs: dict[str, Any]) -> list[str] | None:
    return attrs.get("entity_id")


async def test_states_get_drops_off_limits_members(core: HomeAssistant) -> None:
    await _home(core)
    d = build_dispatcher(core)
    for eid, want in WANT.items():
        attrs = (await d.dispatch("states.get", {"entity_id": eid}))["attributes"]
        assert _members(attrs) == want, eid
    # The rest of the attributes are untouched.
    assert (await d.dispatch("states.get", {"entity_id": SINGLE}))["attributes"]["friendly_name"] == "Just the lock"


async def test_entities_list_drops_off_limits_members(core: HomeAssistant) -> None:
    await _home(core)
    listed = {e["entity_id"]: e for e in await build_dispatcher(core).dispatch("entities.list", {})}
    for eid, want in WANT.items():
        assert _members(listed[eid]["attributes"]) == want, eid


async def test_the_push_drops_off_limits_members(core: HomeAssistant) -> None:
    await _home(core)
    pushed: dict[str, dict[str, Any]] = {}

    async def send(batch: list[dict]) -> None:
        pushed.update({e["entity_id"]: e for e in batch})

    s = sub.StateSubscriber(core, send)
    s.start()
    try:
        _set(core, "off")
        await core.async_block_till_done()
        await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
        await core.async_block_till_done()
    finally:
        s.stop()
    for eid, want in WANT.items():
        assert _members(pushed[eid]["attributes"]) == want, eid


async def test_no_reader_names_an_off_limits_id_anywhere(core: HomeAssistant) -> None:
    """The whole answer, serialised, so a copy of the list kept under another key is caught too."""
    await _home(core)
    d = build_dispatcher(core)
    text = repr(await d.dispatch("entities.list", {})) + "".join([repr(await d.dispatch("states.get", {"entity_id": eid})) for eid in WANT])
    for eid in [*OFF, RELAY]:
        assert eid not in text, eid


async def test_a_literal_lock_is_dropped_whatever_gate_it_carries(core: HomeAssistant) -> None:
    """Ruling 9 hands a valve-wrapped group's gate down to its members, so a `lock.*` member of one
    has the effective domain `valve`, which is not off-limits. The literal domain is read too, as
    `visible()` does, so the lock is still dropped from the group's own `entity_id` attribute."""
    group = await _switch(core, "door_group")
    core.states.async_set(group, "off", {"entity_id": ["lock.gate", LIGHT]})
    core.states.async_set("lock.gate", "locked")
    core.states.async_set(LIGHT, "on")
    await _wrap(core, group, "valve")
    # The wrapper hides its source; un-hide and expose it, so the group is read and only its
    # attribute is left to answer for the lock.
    er.async_get(core).async_update_entity(group, hidden_by=None)
    async_expose_entity(core, "conversation", group, True)
    await core.async_block_till_done()
    assert registry.effective_domain(core, "lock.gate") == "valve"
    assert registry.visible(core, group, er.async_get(core).async_get(group))

    d = build_dispatcher(core)
    assert _members((await d.dispatch("states.get", {"entity_id": group}))["attributes"]) == [LIGHT]
    listed = {e["entity_id"]: e for e in await d.dispatch("entities.list", {})}
    assert _members(listed[group]["attributes"]) == [LIGHT]

    pushed: dict[str, dict[str, Any]] = {}

    async def send(batch: list[dict]) -> None:
        pushed.update({e["entity_id"]: e for e in batch})

    s = sub.StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(group, "on", {"entity_id": ["lock.gate", LIGHT]})
        await core.async_block_till_done()
        await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
        await core.async_block_till_done()
    finally:
        s.stop()
    assert _members(pushed[group]["attributes"]) == [LIGHT]
