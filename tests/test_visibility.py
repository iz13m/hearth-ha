"""One visibility rule, called by every read of the home (#259).

`visible()` used to be one of three copies of the rule: `entities.list` repeated its clauses inline
and `states.get` applied two of them. Its docstring promised that push and poll cannot disagree, and
nothing made that true. These rows are what makes it true now:

- **What each reader answers**, over one corpus that has an entity only each clause refuses. A copy
  of the rule with one clause changed answers differently here.
- **That each reader asks `visible()`**, by swapping it for a stricter rule and checking every reader
  follows. A faithful inline copy agrees with `visible()` on every entity, so only this row sees it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any

import pytest

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai import subscriber as sub
from custom_components.hearth_ai.handlers import history, organise, registry
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SHOWN = "switch.shown"
HIDDEN = "switch.hidden"
DISABLED = "switch.disabled"
UNEXPOSED = "switch.unexposed"
CAMERA = "camera.porch"
# In `OFF_LIMITS` but not in `HIDDEN_DOMAINS` (#603), so a reader that fell back to `HIDDEN_DOMAINS`
# answers for it here. The camera is in both sets and cannot tell them apart.
ALARM = "alarm_control_panel.house"
CORPUS = (SHOWN, HIDDEN, DISABLED, UNEXPOSED, CAMERA, ALARM)

# The whole rule: only the control passes.
FULL = {SHOWN}
# `registry=False`: the hidden and the disabled switch pass too. This is what `states.get` and
# `states.history` do today, pinned so the refactor changes nothing; it is not the rule they should
# follow. AGENTS.md's invariant 1 says hidden and disabled entries are not readable as history, and
# #642 fixes that gap for history. When it is fixed, those readers' rows below move to `FULL`.
NO_REGISTRY = {SHOWN, HIDDEN, DISABLED}


async def _corpus(hass: HomeAssistant) -> None:
    """One entity each clause alone refuses, beside a control that passes all three.

    Every one is in the registry and has a state, and all but `UNEXPOSED` are exposed. Home
    Assistant drops a disabled entity's state, so `DISABLED`'s is set by hand: without it the
    disabled clause could be deleted from a reader and nothing here would notice.
    """
    reg = er.async_get(hass)
    flags: dict[str, dict[str, Any]] = {
        HIDDEN: {"hidden_by": er.RegistryEntryHider.USER},
        DISABLED: {"disabled_by": er.RegistryEntryDisabler.USER},
    }
    for entity_id in CORPUS:
        domain, object_id = entity_id.split(".", 1)
        reg.async_get_or_create(
            domain, "hearth_test", object_id, suggested_object_id=object_id, **flags.get(entity_id, {})
        )
    for entity_id in CORPUS:
        assert reg.async_get(entity_id) is not None, entity_id
        hass.states.async_set(entity_id, "on")
        async_expose_entity(hass, "conversation", entity_id, entity_id != UNEXPOSED)
    await hass.async_block_till_done()


async def _answers(hass: HomeAssistant) -> dict[str, set[str]]:
    """The corpus ids each reader answers for, one set per reader."""
    d = build_dispatcher(hass)

    async def get(entity_id: str) -> bool:
        try:
            await d.dispatch("states.get", {"entity_id": entity_id})
        except RpcError as e:
            assert e.code == "not_found"
            return False
        return True

    def refuses(read: Callable[[], Any]) -> bool:
        try:
            read()
        except RpcError as e:
            assert e.code == "not_found"
            return True
        return False

    pushed: set[str] = set()

    async def send(batch: list[dict]) -> None:
        pushed.update(e["entity_id"] for e in batch)

    s = sub.StateSubscriber(hass, send)
    s.start()
    try:
        for entity_id in CORPUS:
            hass.states.async_set(entity_id, "off")
        await hass.async_block_till_done()
        await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
        await hass.async_block_till_done()
    finally:
        s.stop()

    listed = {e["entity_id"] for e in await d.dispatch("entities.list", {})}
    return {
        "entities.list": listed & set(CORPUS),
        "push": pushed & set(CORPUS),
        "labels": {i for i in CORPUS if not refuses(lambda i=i: organise._targets(hass, {"entity_ids": [i]}))},
        "states.get": {i for i in CORPUS if await get(i)},
        "states.history": {i for i in CORPUS if not refuses(lambda i=i: history._entity(hass, i))},
    }


async def test_every_reader_answers_the_rule_it_names(core: HomeAssistant) -> None:
    """Red if a reader keeps its own copy of the rule and that copy differs in any one clause."""
    await _corpus(core)
    ent_reg = er.async_get(core)

    # The rule itself, pinned literally, so a change to `visible()` is red here and not only agreed with.
    assert {i for i in CORPUS if registry.visible(core, i, ent_reg.async_get(i))} == FULL
    assert {i for i in CORPUS if registry.visible(core, i, ent_reg.async_get(i), registry=False)} == NO_REGISTRY

    assert await _answers(core) == {
        "entities.list": FULL,
        "push": FULL,
        "labels": FULL,
        # Current behaviour, not the rule: see `NO_REGISTRY`.
        "states.get": NO_REGISTRY,
        "states.history": NO_REGISTRY,
    }


async def test_every_reader_asks_visible(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Red if a reader stops calling `visible()`, even with a copy that agrees with it today.

    `visible()` is swapped, in every module that imported it, for one that also refuses the control.
    A reader that asks it stops answering for the control; one that decides for itself does not.
    Scope: the five readers `_answers` calls. The swap reaches any module that imports `visible()`,
    but a new reader is only checked once it has a row in `_answers`, and one that never imports
    `visible()` is not found here at all.
    """
    await _corpus(core)
    real = registry.visible

    def stricter(hass: HomeAssistant, entity_id: str, ent: Any, **kwargs: Any) -> bool:
        return entity_id != SHOWN and real(hass, entity_id, ent, **kwargs)

    holders = [
        m
        for name, m in list(sys.modules.items())
        if name.startswith("custom_components.hearth_ai") and getattr(m, "visible", None) is real
    ]
    # The control for the discovery: it must find every module this file names, or the swap is partial.
    assert {m.__name__.rsplit(".", 1)[-1] for m in holders} >= {"registry", "subscriber", "organise", "history"}
    for m in holders:
        monkeypatch.setattr(m, "visible", stricter)

    assert await _answers(core) == {
        "entities.list": set(),
        "push": set(),
        "labels": set(),
        "states.get": NO_REGISTRY - {SHOWN},
        "states.history": NO_REGISTRY - {SHOWN},
    }


async def test_states_get_refuses_an_off_limits_domain_in_its_own_words(core: HomeAssistant) -> None:
    """`states.get` answers an off-limits domain `entity not available`, and anything else it refuses
    `unknown entity <id>`.

    `visible()` refuses the camera and the alarm panel too, so the early `OFF_LIMITS` check in
    `states.get` changes only the message. Without this row that check could be deleted, or narrowed
    to `HIDDEN_DOMAINS`, with the suite green. The unexposed switch is the control: it shows the other
    message is what the camera would get.
    """
    await _corpus(core)
    d = build_dispatcher(core)
    messages = {}
    for entity_id in (CAMERA, ALARM, UNEXPOSED):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("states.get", {"entity_id": entity_id})
        assert ei.value.code == "not_found"
        messages[entity_id] = ei.value.message

    assert messages == {
        CAMERA: "entity not available",
        ALARM: "entity not available",
        UNEXPOSED: f"unknown entity {UNEXPOSED}",
    }
