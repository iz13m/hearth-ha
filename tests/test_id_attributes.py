"""No attribute names an off-limits entity, at any depth, and a zone never says who is in it (#729).

#722 cleaned the `entity_id` attribute alone. Home Assistant publishes ids under other keys too:
`zone.*` lists the `person.*` ids inside it as `persons`; a `bayesian` binary sensor names the
`person.*` / `device_tracker.*` ids it reads in `occurred_observation_entities` and inside the dicts
of `observations`; `utility_meter`, `compensation` and friends name a `source`, `min_max` its
`min_entity_id`, `universal` its `active_child`, a `media_player` its `group_members`. Every string at
any depth that names an off-limits id is dropped, and allowed ids are kept, so those attributes still
mean something.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from types import MappingProxyType
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai import subscriber as sub
from custom_components.hearth_ai.handlers import registry
from custom_components.hearth_ai.rpc import build_dispatcher

ZONE = "zone.home"
BAYES = "binary_sensor.someone_home"
METER = "sensor.energy_daily"
MINMAX = "sensor.warmest"
UNIVERSAL = "media_player.living"
SPEAKER = "media_player.kitchen"
ODD = "sensor.odd"
RELAY = "switch.gate_relay"  # wrapped as a lock below: off-limits only by its effective domain
# The same, with an Arabic-Indic digit Home Assistant accepts in an id. Its wrapper is only
# registered, not set up: the gate map reads config entries, set up or not.
UNI_RELAY = "switch.relay٣"

OBSERVATIONS = [
    {"platform": "state", "entity_id": "person.me", "to_state": "home", "prob_given_true": 0.9},
    {"platform": "state", "entity_id": "device_tracker.phone", "to_state": "home", "prob_given_true": 0.8},
    {"platform": "state", "entity_id": "binary_sensor.motion", "to_state": "on", "prob_given_true": 0.7},
]


def _set(hass: HomeAssistant, state: str = "on") -> None:
    hass.states.async_set(ZONE, "2" if state == "on" else "1", {"persons": ["person.me", "person.you"], "radius": 100, "friendly_name": "Home"})
    hass.states.async_set(
        BAYES,
        state,
        {
            "occurred_observation_entities": ["person.me", "device_tracker.phone", "binary_sensor.motion"],
            "observations": OBSERVATIONS,
            "probability": 0.93,
        },
    )
    hass.states.async_set(METER, state, {"source": "sensor.energy", "unit_of_measurement": "kWh"})
    hass.states.async_set(
        MINMAX,
        state,
        # A tracker as the source of a sensor (as `compensation` may), and an allowed one beside it.
        {"min_entity_id": "sensor.bedroom", "max_entity_id": "device_tracker.phone", "last_entity_id": "Person.Me"},
    )
    hass.states.async_set(UNIVERSAL, state, {"active_child": "media_player.tv", "children": ["media_player.tv", "lock.gate"]})
    hass.states.async_set(
        SPEAKER,
        state,
        {
            # Contrived: a lock and a lock-wrapped relay among the players, plus a dict keyed by ids.
            "group_members": [SPEAKER, "lock.gate", RELAY, "media_player.den"],
            "by_member": {"lock.gate": "locked", "media_player.den": "playing", "nested": [{"x": "siren.hall"}]},
            "version": "2026.9",
            "source": "lock.gate",
        },
    )
    hass.states.async_set(
        ODD,
        state,
        {
            # Ids Home Assistant accepts with Unicode digits (its `valid_entity_id` uses `\d`), a
            # trailing space, and a light written the same way as the control.
            "entity_id": ["lock.front٣", "lock.１", "lock.front ", "light.１"],
            "a": "lock.front٣",
            "b": "lock.１",
            "c": "lock.front ",
            "c2": "lock.front-door",  # not id-shaped, but its domain is a lock's
            "uni_relay": UNI_RELAY,
            "d": "light.１",
            # Several ids in one string.
            "joined": "light.a,lock.front",
            "spaced": "light.a lock.front",
            "relay_joined": f"light.a, {RELAY}",
            "lights": "light.a,light.b",
            # Punctuation around an id, and what `plain` stringifies (#729).
            "bracketed": "[lock.front]",
            "reprd": "{'lock.front': 1}",
            "colon": "x:lock.front",
            "piped": "light.a|lock.front",
            "zwsp": "\u200block.front",
            "template": "{{ states('lock.front') }}",
            "chained": "a.lock.front",
            "proxy": MappingProxyType({"lock.front": 1}),
            "queue": deque(["light.a", "lock.front"]),
            # Controls: a word that only ends in an off-limits domain, and a proxy of allowed ids.
            "block": "block.front",
            "light_proxy": MappingProxyType({"light.a": 1}),
            # An attribute whose own key is an off-limits id.
            "lock.front": "keyed",
            # A set is walked as a list, not stringified whole.
            "members": {"light.a", "lock.gate"},
        },
    )


async def _home(hass: HomeAssistant) -> None:
    reg = er.async_get(hass)
    reg.async_get_or_create("switch", "hearth_test", "gate_relay", suggested_object_id="gate_relay")
    hass.states.async_set(RELAY, "off")
    entry = MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": RELAY, "invert": False, "target_domain": "lock"},
        title="relay as lock",
        version=1,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    MockConfigEntry(
        domain="switch_as_x",
        options={"entity_id": UNI_RELAY, "invert": False, "target_domain": "lock"},
        title="relay as lock",
        version=1,
        minor_version=3,
    ).add_to_hass(hass)
    for eid in (ZONE, BAYES, METER, MINMAX, UNIVERSAL, SPEAKER, ODD):
        domain, object_id = eid.split(".", 1)
        reg.async_get_or_create(domain, "hearth_test", object_id, suggested_object_id=object_id)
        async_expose_entity(hass, "conversation", eid, True)
    _set(hass)
    await hass.async_block_till_done()
    assert registry.effective_domain(hass, RELAY) == "lock"
    assert registry.effective_domain(hass, UNI_RELAY) == "lock"
    assert all(registry.visible(hass, eid, reg.async_get(eid)) for eid in (ZONE, BAYES, METER, MINMAX, UNIVERSAL, SPEAKER, ODD))


def _check(answers: dict[str, dict[str, Any]]) -> None:
    zone = answers[ZONE]
    assert "persons" not in zone
    assert zone["radius"] == 100 and zone["friendly_name"] == "Home"

    bayes = answers[BAYES]
    assert bayes["occurred_observation_entities"] == ["binary_sensor.motion"]
    # An observation of a person or a tracker goes whole: what is left would still describe them.
    assert bayes["observations"] == [OBSERVATIONS[2]]
    assert bayes["probability"] == 0.93

    # Controls: allowed ids are untouched.
    assert answers[METER]["source"] == "sensor.energy"
    minmax = answers[MINMAX]
    assert minmax["min_entity_id"] == "sensor.bedroom"
    assert "max_entity_id" not in minmax
    assert "last_entity_id" not in minmax  # the literal domain is read case-blind, as `visible` does
    assert answers[UNIVERSAL]["active_child"] == "media_player.tv"
    assert answers[UNIVERSAL]["children"] == ["media_player.tv"]

    speaker = answers[SPEAKER]
    assert speaker["group_members"] == [SPEAKER, "media_player.den"]
    assert speaker["by_member"] == {"media_player.den": "playing", "nested": []}
    assert speaker["version"] == "2026.9"
    assert "source" not in speaker

    odd = answers[ODD]
    assert odd["entity_id"] == ["light.１"]
    for key in ("a", "b", "c", "c2", "uni_relay", "joined", "spaced", "relay_joined", "lock.front", "bracketed", "reprd", "colon", "piped", "zwsp", "template", "chained", "proxy", "queue"):
        assert key not in odd, key
    assert odd["d"] == "light.１"
    assert odd["lights"] == "light.a,light.b"
    assert odd["block"] == "block.front"
    assert odd["light_proxy"] == str(MappingProxyType({"light.a": 1}))
    assert odd["members"] == ["light.a"]


async def test_states_get(core: HomeAssistant) -> None:
    await _home(core)
    d = build_dispatcher(core)
    _check({eid: (await d.dispatch("states.get", {"entity_id": eid}))["attributes"] for eid in (ZONE, BAYES, METER, MINMAX, UNIVERSAL, SPEAKER, ODD)})


async def test_entities_list(core: HomeAssistant) -> None:
    await _home(core)
    _check({e["entity_id"]: e["attributes"] for e in await build_dispatcher(core).dispatch("entities.list", {})})


async def test_the_push(core: HomeAssistant) -> None:
    await _home(core)
    pushed: dict[str, dict[str, Any]] = {}

    async def send(batch: list[dict]) -> None:
        pushed.update({e["entity_id"]: e["attributes"] for e in batch})

    s = sub.StateSubscriber(core, send)
    s.start()
    try:
        _set(core, "off")
        await core.async_block_till_done()
        await asyncio.sleep(sub.DEBOUNCE_S + 0.2)
        await core.async_block_till_done()
    finally:
        s.stop()
    _check(pushed)


async def test_no_reader_names_an_off_limits_id_anywhere(core: HomeAssistant) -> None:
    await _home(core)
    d = build_dispatcher(core)
    text = repr(await d.dispatch("entities.list", {})).lower()
    for eid in ("person.me", "person.you", "device_tracker.phone", "lock.gate", "siren.hall", "lock.front", "lock.１", RELAY):
        # Not as a substring: the `block.front` control ends in one.
        assert not re.search(rf"(?<!\w){re.escape(eid)}", text), eid


def test_strings_that_are_not_ids_are_left_alone() -> None:
    """Text with no id in it comes back as it was, and nothing is built for it. Text that mentions
    a lock between spaces is dropped on its literal domain, also without the gate map."""
    calls: list[str] = []

    class Hass:
        pass

    attrs = {"note": "ends in a dot.", "n": 1.5, "tags": ["a", "b"], "d": {"k": "v"}, "prose": "the lock.gate is shut"}
    orig = registry._gates
    registry._gates = lambda hass: calls.append("gates") or orig(hass)  # type: ignore[assignment]
    try:
        out = registry.filter_attributes(Hass(), attrs)  # type: ignore[arg-type]
    finally:
        registry._gates = orig  # type: ignore[assignment]
    assert out == {"note": "ends in a dot.", "n": 1.5, "tags": ["a", "b"], "d": {"k": "v"}}
    assert calls == []
