"""Pushing exposed-entity state to the hub instead of being polled for it."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant

from custom_components.hearth_ai import subscriber as sub
from custom_components.hearth_ai.subscriber import StateSubscriber

TEST = "input_boolean.test"


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()
    await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
    await hass.async_block_till_done()


async def test_pushes_a_change_in_the_entities_list_shape(core: HomeAssistant) -> None:
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    s = StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(TEST, "on")
        await _settle(core)
    finally:
        s.stop()

    assert len(sent) == 1
    [entity] = sent[0]
    # The same shape `entities.list` returns, so the hub projects push and poll identically.
    assert set(entity) == {"entity_id", "name", "domain", "area_id", "device_id", "device_name", "entity_category", "hearth_labels", "state", "attributes"}
    assert entity["entity_id"] == TEST
    assert entity["state"] == "on"


async def test_debounces_a_burst_into_one_frame_with_the_latest_value(core: HomeAssistant) -> None:
    """Dimming a light emits a change per step; without this it would be a frame per step."""
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    s = StateSubscriber(core, send)
    s.start()
    try:
        for pct in (10, 20, 30, 40, 50):
            core.states.async_set(TEST, "on", {"brightness": pct})
        await _settle(core)
    finally:
        s.stop()

    assert len(sent) == 1, "a burst on one entity must collapse to a single frame"
    assert len(sent[0]) == 1
    assert sent[0][0]["attributes"]["brightness"] == 50, "the latest value must win"


async def test_never_pushes_an_entity_the_owner_did_not_expose(core: HomeAssistant) -> None:
    """Push must not become a way around the exposure filter that gates every read."""
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    async_expose_entity(core, "conversation", TEST, False)
    s = StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(TEST, "on")
        # A denied domain must never appear either, exposed or not.
        core.states.async_set("lock.front", "unlocked")
        await _settle(core)
    finally:
        s.stop()
        async_expose_entity(core, "conversation", TEST, True)

    assert sent == []


async def test_never_pushes_an_image_processing_entity_shared_before_it_was_hidden(core: HomeAssistant) -> None:
    """Exposed to Assist or not, a plate number read off a camera frame is never pushed (#566).

    `TEST` changes in the same window, so an empty push cannot be a subscriber that never ran.
    """
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    plate = "image_processing.driveway"
    core.states.async_set(plate, "unknown")
    async_expose_entity(core, "conversation", plate, True)
    s = StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(plate, "AB12CDE", {"plates": {"AB12CDE": 97.1}})
        core.states.async_set(TEST, "on")
        await _settle(core)
    finally:
        s.stop()

    assert [e["entity_id"] for batch in sent for e in batch] == [TEST]


async def test_stop_silences_it(core: HomeAssistant) -> None:
    """Nothing queues while the socket is down: the app's fallback poll is what recovers state."""
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    s = StateSubscriber(core, send)
    s.start()
    s.stop()
    core.states.async_set(TEST, "on")
    await _settle(core)
    assert sent == []


async def test_a_failing_send_does_not_break_the_subscription(core: HomeAssistant) -> None:
    """A push is best effort; a hub hiccup must not take the listener down with it."""
    calls = 0

    async def send(batch: list[dict]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("hub went away")

    s = StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(TEST, "on")
        await _settle(core)
        core.states.async_set(TEST, "off")
        await _settle(core)
    finally:
        s.stop()

    assert calls == 2


@pytest.mark.parametrize(("entity_id", "state"), [("alarm_control_panel.house", "armed_away"), ("siren.hall", "on")])
async def test_never_pushes_an_exposed_alarm_panel_or_siren(core: HomeAssistant, entity_id: str, state: str) -> None:
    """#603: exposing one in Home Assistant's own UI must not put it on the push, as it does not the poll.

    A fan exposed the same way changes in the same window and must be pushed: that is what shows the
    alarm is dropped for its domain, not because the fixture pushes nothing.
    """
    sent: list[list[dict]] = []

    async def send(batch: list[dict]) -> None:
        sent.append(batch)

    control = "fan.landing"
    for eid in (entity_id, control):
        async_expose_entity(core, "conversation", eid, True)
    s = StateSubscriber(core, send)
    s.start()
    try:
        core.states.async_set(entity_id, state)
        core.states.async_set(control, "on")
        await _settle(core)
    finally:
        s.stop()

    pushed = [e["entity_id"] for batch in sent for e in batch]
    assert control in pushed
    assert entity_id not in pushed
