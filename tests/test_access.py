"""Doors (AgDR-0025).

The denial moved from the product to the model, so these come in two halves: that a person can now
work a lock, and that everything which kept the *model* away from one still does.
"""

from __future__ import annotations

import pytest

from homeassistant.components.lock import LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai.const import CAPABILITY_FOR_METHOD, OPT_IN_CAPABILITIES
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher


def _lock(core: HomeAssistant, object_id: str, *, state: str = "locked", features: int = 0, code_format: str | None = None) -> str:
    reg = er.async_get(core)
    entry = reg.async_get_or_create("lock", "test", f"lock-{object_id}", suggested_object_id=object_id)
    attrs: dict = {"supported_features": features}
    if code_format is not None:
        attrs["code_format"] = code_format
    core.states.async_set(entry.entity_id, state, attrs)
    return entry.entity_id


@pytest.fixture
def calls(core: HomeAssistant) -> list[tuple[str, dict]]:
    """Record lock service calls, and move the entity the way a real lock would."""
    seen: list[tuple[str, dict]] = []

    async def handle(call) -> None:  # noqa: ANN001
        seen.append((call.service, dict(call.data)))
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        core.states.async_set(entity_id, {"lock": "locked", "unlock": "unlocked", "open": "open"}[call.service])

    for service in ("lock", "unlock", "open"):
        core.services.async_register("lock", service, handle)
    return seen


async def test_lists_locks_with_what_each_can_do(core: HomeAssistant) -> None:
    _lock(core, "front", features=LockEntityFeature.OPEN, code_format=r"\d{4}")
    _lock(core, "back")
    rows = await build_dispatcher(core).dispatch("access.list", {})
    by_id = {r["entity_id"]: r for r in rows}

    assert by_id["lock.front"]["can_open"] is True
    assert by_id["lock.front"]["needs_code"] is True
    assert by_id["lock.back"]["can_open"] is False
    # Hearth never carries a code, so this is really "the owner must set a default code in HA".
    assert by_id["lock.back"]["needs_code"] is False


async def test_locks_and_unlocks(core: HomeAssistant, calls: list[tuple[str, dict]]) -> None:
    front = _lock(core, "front")
    d = build_dispatcher(core)

    out = await d.dispatch("access.operate", {"entity_id": front, "action": "unlock"})
    assert out == {"entity_id": front, "state": "unlocked"}
    assert calls[-1][0] == "unlock"

    out = await d.dispatch("access.operate", {"entity_id": front, "action": "lock"})
    assert out == {"entity_id": front, "state": "locked"}


async def test_waits_past_the_transitional_state(core: HomeAssistant) -> None:
    """A deadbolt reports `unlocking` before `unlocked`, and answering with the former reads as failure."""
    front = _lock(core, "front")

    async def slow(call) -> None:  # noqa: ANN001
        core.states.async_set(front, "unlocking")
        core.async_create_task(_finish())

    async def _finish() -> None:
        core.states.async_set(front, "unlocked")

    core.services.async_register("lock", "unlock", slow)
    out = await build_dispatcher(core).dispatch("access.operate", {"entity_id": front, "action": "unlock"})
    assert out["state"] == "unlocked"


async def test_does_not_wait_for_a_change_that_is_not_coming(core: HomeAssistant, calls: list[tuple[str, dict]]) -> None:
    """Locking an already-locked door fires no state change, so waiting would burn the whole timeout.

    Without the short-circuit this test would still pass — it would just take three seconds. The
    assertion that matters is the wall clock.
    """
    import time

    front = _lock(core, "front", state="locked")
    started = time.monotonic()
    out = await build_dispatcher(core).dispatch("access.operate", {"entity_id": front, "action": "lock"})
    assert out["state"] == "locked"
    assert time.monotonic() - started < 1.0


async def test_survives_a_nonsense_supported_features(core: HomeAssistant) -> None:
    """Integrations put odd things in attributes; a lock list must not blow up on one."""
    reg = er.async_get(core)
    entry = reg.async_get_or_create("lock", "test", "lock-odd", suggested_object_id="odd")
    core.states.async_set(entry.entity_id, "locked", {"supported_features": "not a number"})
    rows = await build_dispatcher(core).dispatch("access.list", {})
    assert next(r for r in rows if r["entity_id"] == entry.entity_id)["can_open"] is False


async def test_refuses_a_latch_the_lock_cannot_release(core: HomeAssistant, calls: list[tuple[str, dict]]) -> None:
    back = _lock(core, "back")  # no LockEntityFeature.OPEN
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("access.operate", {"entity_id": back, "action": "open"})
    assert err.value.code == "invalid_params"
    assert calls == []


async def test_refuses_something_that_is_not_a_lock(core: HomeAssistant) -> None:
    core.states.async_set("light.kitchen", "on")
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("access.operate", {"entity_id": "light.kitchen", "action": "unlock"})
    assert err.value.code == "invalid_params"


async def test_passes_the_locks_own_refusal_back(core: HomeAssistant) -> None:
    """A lock that wants a code the owner never set fails here, and the message is the useful part."""
    front = _lock(core, "front", code_format=r"\d{4}")

    async def demands_code(call) -> None:  # noqa: ANN001
        raise HomeAssistantError("Code required but none provided")

    core.services.async_register("lock", "unlock", demands_code)
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("access.operate", {"entity_id": front, "action": "unlock"})
    assert err.value.code == "ha_error"
    assert "Code" in err.value.message


async def test_the_capability_is_opt_in_and_its_own(core: HomeAssistant) -> None:
    """Enabling 'operate my lights' must never have meant 'open my front door'."""
    assert CAPABILITY_FOR_METHOD["access.operate"] == "access.control"
    assert CAPABILITY_FOR_METHOD["access.list"] == "access.control"
    assert "access.control" in OPT_IN_CAPABILITIES
    # The distinction that matters: a different toggle from device control.
    assert CAPABILITY_FOR_METHOD["devices.call"] != CAPABILITY_FOR_METHOD["access.operate"]


async def test_refused_outright_when_the_owner_has_not_enabled_it(core: HomeAssistant) -> None:
    """The integration's own gate, which is the load-bearing one.

    The hub marks these tools app-only, but the integration cannot see *who* asked — `surface` is
    deliberately not on the wire, because a forgeable "this came from the app" flag would be a flag
    that opens doors. So this side refuses on the owner's capability alone.
    """
    front = _lock(core, "front")
    # A dispatcher built with a capability set that does not include access.control.
    d = build_dispatcher(core, capabilities=frozenset({"entities.read", "devices.control"}))
    for method, params in (("access.list", {}), ("access.operate", {"entity_id": front, "action": "unlock"})):
        with pytest.raises(RpcError) as err:
            await d.dispatch(method, params)
        assert err.value.code == "method_not_allowed"


async def test_the_model_still_cannot_reach_a_lock_any_other_way(core: HomeAssistant, calls: list[tuple[str, dict]]) -> None:
    """Everything AgDR-0012 closed stays closed. This is the half that must not regress."""
    front = _lock(core, "front")
    d = build_dispatcher(core)

    # Not through devices.call...
    with pytest.raises(RpcError) as err:
        await d.dispatch("devices.call", {"domain": "lock", "service": "unlock", "entity_id": [front]})
    assert err.value.code == "method_not_allowed"

    # ...and not written into an automation.
    out = await d.dispatch(
        "automations.validate",
        {"config": {"alias": "x", "triggers": [], "actions": [{"action": "lock.unlock", "target": {"entity_id": front}}]}},
    )
    assert out["ok"] is False

    # ...nor a scene.
    with pytest.raises(RpcError):
        await d.dispatch("scenes.create", {"config": {"name": "n", "entities": {front: "unlocked"}}})

    assert calls == []


async def test_a_lock_is_still_invisible_to_the_catalogue(core: HomeAssistant) -> None:
    """`entities.list` feeds the model's inventory, so locks stay out of it however this is enabled."""
    front = _lock(core, "front")
    d = build_dispatcher(core)
    assert front not in {e["entity_id"] for e in await d.dispatch("entities.list", {})}
    with pytest.raises(RpcError):
        await d.dispatch("states.get", {"entity_id": front})
