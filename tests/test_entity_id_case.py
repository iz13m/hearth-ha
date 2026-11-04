"""An entity id in any form but the canonical one is refused before anything looks it up (#646).

Home Assistant's state lookup falls back to `entity_id.lower()`; its registry and exposure lookups do
not. So `switch.Hidden` found `switch.hidden`'s state, no registry entry and no exposure setting, and
every check made from those passed: the owner's "not exposed", the registry's hidden and disabled,
and `_exposed()` fell through to Home Assistant's legacy default, writing a new key into the
exposed-entities store as it went. The hub's schema refuses these ids, so this is the box not
trusting the hub.

Each mixed-case id here is a case variant of a real corpus entity, with a state, a registry entry and
an exposure setting, so a refusal can only come from the case and not from the id being unknown.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.rpc import CHECKED_PER_ID, ENTITY_ID_PARAMS, Dispatcher, RpcError, build_dispatcher

from .test_visibility import SHOWN, _corpus

# The corpus's hidden, disabled and unexposed switches, spelled so only the case is wrong.
VARIANTS = ("switch.Hidden", "switch.Disabled", "switch.Unexposed")
# Other forms the canonical one excludes. The newline is the one `re.match` with `$` lets through.
MALFORMED = ("switch.Shown", "Switch.shown", "switch.shown\n", "switch.shown.x", "switch")


def _store(hass: HomeAssistant) -> set[str]:
    return set(hass.data[DATA_EXPOSED_ENTITIES].entities)


async def _refused(call) -> tuple[str, str]:
    with pytest.raises(RpcError) as e:
        await call
    return e.value.code, e.value.message


@pytest.mark.parametrize("variant", VARIANTS)
async def test_a_mixed_case_id_is_refused_by_every_reader_and_by_devices_call(core: HomeAssistant, variant: str) -> None:
    """The rows #646 names, through the dispatcher, with the unknown-id wording and no side effect.

    `devices.call` names the control first, so a check that reads only the first id is red here.
    """
    await _corpus(core)
    turned_on = async_mock_service(core, "switch", "turn_on")
    d = build_dispatcher(core)
    before = _store(core)
    unknown = ("not_found", f"unknown entity {variant}")

    assert await _refused(d.dispatch("states.get", {"entity_id": variant})) == unknown
    assert await _refused(d.dispatch("states.history", {"entity_id": variant})) == unknown
    call = {"domain": "switch", "service": "turn_on", "entity_id": [SHOWN, variant]}
    assert await _refused(d.dispatch("devices.call", call)) == unknown
    assert turned_on == []
    assert _store(core) == before

    # The control: the canonical id behaves as it did.
    assert (await d.dispatch("states.get", {"entity_id": SHOWN}))["entity_id"] == SHOWN
    await d.dispatch("devices.call", {"domain": "switch", "service": "turn_on", "entity_id": [SHOWN]})
    assert [c.data["entity_id"] for c in turned_on] == [[SHOWN]]


async def test_the_entity_ids_writers_refuse_a_mixed_case_id(core: HomeAssistant) -> None:
    """`entities.expose` and the label targets take their ids under `entity_ids`, after a good one.

    `entities.expose` refuses per id, with the `not_entity` code the hub declares, and changes the
    rest; the label targets refuse the call, as they refuse an unknown id.
    """
    await _corpus(core)
    d = build_dispatcher(core)
    label = (await d.dispatch("labels.create", {"name": "Holiday"}))["label_id"]
    before = _store(core)

    out = await d.dispatch("entities.expose", {"entity_ids": ["switch.unexposed", *VARIANTS], "expose": True})
    assert out["changed"] == ["switch.unexposed"]
    assert out["refused"] == [{"entity_id": v, "code": "not_entity", "reason": "not an entity id"} for v in VARIANTS]
    assign = {"label_id": label, "entity_ids": [SHOWN, "switch.Hidden"]}
    assert await _refused(d.dispatch("labels.assign", assign)) == ("not_found", "unknown entity switch.Hidden")
    assert _store(core) == before

    # The control: the canonical ids go through.
    assert (await d.dispatch("labels.assign", {"label_id": label, "entity_ids": [SHOWN]}))["changed"] == [SHOWN]


async def test_no_mixed_case_id_reaches_the_exposure_store_or_a_service(core: HomeAssistant) -> None:
    """The side effects alone, every call made whatever it answers, so no earlier assertion masks them.

    This is the row a missing check fails on even where a handler happens to refuse the id itself.
    """
    await _corpus(core)
    turned_on = async_mock_service(core, "switch", "turn_on")
    d = build_dispatcher(core)
    label = (await d.dispatch("labels.create", {"name": "Holiday"}))["label_id"]
    before = _store(core)

    for variant in VARIANTS:
        for method, params in (
            ("states.get", {"entity_id": variant}),
            ("states.history", {"entity_id": variant}),
            ("devices.call", {"domain": "switch", "service": "turn_on", "entity_id": [SHOWN, variant]}),
            ("entities.expose", {"entity_ids": [SHOWN, variant], "expose": True}),
            ("labels.assign", {"label_id": label, "entity_ids": [SHOWN, variant]}),
        ):
            try:
                await d.dispatch(method, params)
            except RpcError:
                pass

    assert _store(core) - before == set()
    assert turned_on == []


async def test_entities_expose_refuses_every_malformed_form_per_id(core: HomeAssistant) -> None:
    """The one method in `CHECKED_PER_ID`, against its real handler, for every form the spy row sends."""
    await _corpus(core)
    before = _store(core)
    out = await build_dispatcher(core).dispatch("entities.expose", {"entity_ids": list(MALFORMED), "expose": False})
    assert out["changed"] == []
    assert [(r["entity_id"], r["code"]) for r in out["refused"]] == [(m, "not_entity") for m in MALFORMED]
    assert _store(core) == before


def test_checked_per_id_is_only_entities_expose() -> None:
    """Pins the dispatcher check's one exception.

    A method in `CHECKED_PER_ID` skips the dispatcher check, and the parametrized row below is derived
    from `ENTITY_ID_PARAMS - CHECKED_PER_ID`, so a method added to the set drops out of that row and
    every row stays green. Growing the set needs that handler's own per-id check, run before any lookup,
    with its own rows, and a review.
    """
    assert CHECKED_PER_ID == {"entities.expose"}


@pytest.mark.parametrize("method", sorted(set(ENTITY_ID_PARAMS) - CHECKED_PER_ID))
async def test_every_entity_id_parameter_is_checked_before_its_handler(hass: HomeAssistant, method: str) -> None:
    """Each method `test_allowlist.py` derives from the schema, against a handler that records its calls.

    A list parameter carries the bad id second, and a bare string under a list key is checked too.
    A non-string is passed through: the handler refuses it as `invalid_params`, as it did before.
    """
    key = ENTITY_ID_PARAMS[method]
    seen: list[object] = []

    async def handler(hass: HomeAssistant, params: dict) -> None:
        seen.append(params.get(key))

    d = Dispatcher(hass)
    d.register(method, handler)
    for entity_id in MALFORMED:
        value = [SHOWN, entity_id] if key == "entity_ids" or method == "devices.call" else entity_id
        assert await _refused(d.dispatch(method, {key: value})) == ("not_found", f"unknown entity {entity_id}")
        if isinstance(value, list):
            assert await _refused(d.dispatch(method, {key: entity_id})) == ("not_found", f"unknown entity {entity_id}")
    assert seen == []

    good = [SHOWN] if key == "entity_ids" or method == "devices.call" else SHOWN
    await d.dispatch(method, {key: good})
    await d.dispatch(method, {key: 5})
    await d.dispatch(method, {})
    assert seen == [good, 5, None]
