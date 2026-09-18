"""Operating the home: direct service calls, scene activation, running scripts.

Gated by the `devices.control` and `routines.run` capabilities, which are off until the
owner enables them. Every call is checked against the same policy the hub applies, so a
compromised hub still cannot reach a lock, alarm, camera, or the host.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from ..labels import is_hearth_scene
from ..policy import find_service_call_violations
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import _exposed
from .scenes import scene_run_violations
from .scripts import script_run_violations

# Service calls can legitimately take a while (thermostats, media players); scripts return
# as soon as they start.
CALL_TIMEOUT_S = 30
MAX_DATA_KEYS = 32
# How long to wait for the targeted entities to write their new state before reporting it.
#
# `blocking=True` waits for the *service handler* to return, not for the entity to push its new
# state into the state machine — plenty of integrations write asynchronously, after a device ack or
# on the next coordinator tick. Reading straight afterwards therefore returned the *pre-call* state
# often enough that the app's tile flipped back and people pressed twice.
#
# A command that genuinely changes nothing (turn_on on a light already on) fires no event and waits
# the whole window; that is the price of never reporting a state the device has not reached, and it
# is bounded well under the round trip the caller already paid for.
SETTLE_TIMEOUT_S = 1.0
# Keys that would widen a call beyond the entity_ids we validated.
TARGET_KEYS = frozenset({"entity_id", "device_id", "area_id", "floor_id", "label_id", "target"})


def _clean_data(data: Any) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RpcError("invalid_params", "data must be an object")
    out = {k: v for k, v in data.items() if k not in TARGET_KEYS}
    if len(out) > MAX_DATA_KEYS:
        raise RpcError("invalid_params", f"data has too many fields (max {MAX_DATA_KEYS})")
    return out


def _state_of(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.state if state else None


async def _settled(hass: HomeAssistant, entity_ids: list[str], call: Any) -> None:
    """
    Run `call` and wait (briefly) for every targeted entity to write a new state.

    The listener is registered *before* the service call so an integration that writes state
    synchronously cannot slip through the gap between the two.
    """
    waiting = set(entity_ids)
    done = asyncio.Event()

    def _seen(event: Event[EventStateChangedData]) -> None:
        waiting.discard(event.data["entity_id"])
        if not waiting:
            done.set()

    unsub = async_track_state_change_event(hass, entity_ids, _seen)
    try:
        await call
        async with asyncio.timeout(SETTLE_TIMEOUT_S):
            await done.wait()
    except TimeoutError:
        # Nothing reported in time. Whatever the state machine holds is the honest answer — for a
        # command that changed nothing it is already correct, and otherwise the next poll settles it.
        pass
    finally:
        unsub()


async def devices_call(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    domain = require_str(params, "domain")
    service = require_str(params, "service")
    raw_ids = params.get("entity_id")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(e, str) for e in raw_ids):
        raise RpcError("invalid_params", "entity_id must be a non-empty list of entity ids")
    if len(raw_ids) > 50:
        raise RpcError("invalid_params", "at most 50 entities per call")

    if violations := find_service_call_violations(domain, service, raw_ids):
        raise RpcError("method_not_allowed", "; ".join(violations))

    if not hass.services.has_service(domain, service):
        raise RpcError("not_found", f"no such service {domain}.{service}")

    for entity_id in raw_ids:
        if hass.states.get(entity_id) is None:
            raise RpcError("not_found", f"unknown entity {entity_id}")
        if not _exposed(hass, entity_id):
            raise RpcError("not_found", f"{entity_id} is not exposed to Assist")

    data = _clean_data(params.get("data"))
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await _settled(
                hass,
                raw_ids,
                hass.services.async_call(domain, service, {**data, "entity_id": raw_ids}, blocking=True),
            )
    except TimeoutError as err:
        raise RpcError("timeout", f"{domain}.{service} did not finish within {CALL_TIMEOUT_S}s") from err

    return {
        "called": f"{domain}.{service}",
        "entities": [{"entity_id": e, "state": _state_of(hass, e) or "unknown"} for e in raw_ids],
    }


async def _run_entity(hass: HomeAssistant, params: dict[str, Any], expected_domain: str) -> dict[str, Any]:
    entity_id = require_str(params, "entity_id")
    if not entity_id.startswith(f"{expected_domain}."):
        raise RpcError("invalid_params", f"entity_id must be a {expected_domain} entity")
    if hass.states.get(entity_id) is None:
        raise RpcError("not_found", f"unknown entity {entity_id}")
    # Exposure is the owner's consent, and it gates running a routine exactly as it gates a direct
    # service call. `devices_call` has always checked this; running a scene or script did not, which
    # made activation the one way to reach an entity the owner kept out of Assist.
    if not _exposed(hass, entity_id):
        raise RpcError("not_found", f"{entity_id} is not exposed to Assist")
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await hass.services.async_call(expected_domain, "turn_on", {"entity_id": entity_id}, blocking=True)
    except TimeoutError as err:
        raise RpcError("timeout", f"{entity_id} did not start within {CALL_TIMEOUT_S}s") from err
    return {"entity_id": entity_id, "state": _state_of(hass, entity_id)}


async def scenes_activate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """
    A scene applies whatever states it defines, so what it *contains* is checked here — not only
    when Hearth writes one. Otherwise a scene the owner wrote by hand could set a lock, and
    activating it would apply that, around the promise that locks are refused everywhere.
    """
    entity_id = require_str(params, "entity_id")
    if entity_id.startswith("scene.") and hass.states.get(entity_id) is not None:
        if violations := await scene_run_violations(hass, entity_id):
            raise RpcError("method_not_allowed", "; ".join(violations))
    return await _run_entity(hass, params, "scene")


async def scripts_run(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """
    Runs whatever the user wrote in that script — including actions Hearth itself may not perform.
    That is a deliberate, documented decision (AgDR-0005), unlike scenes: a script is a sequence the
    owner authored and exposed, and the options UI says so plainly. Exposure is still required.

    **Except a script Hearth presents as a scene.** A scene with steps is stored as a labelled
    script (AgDR-0044), and a scene is checked when it runs (AgDR-0012) — so storing one as a script
    must not be the way that check is lost. AgDR-0005's licence covers a script the owner reaches as
    a script, not one the app offers in the Scenes tab. The label is the owner's own, so putting it
    on a script *narrows* what that script may do, which is the safe direction for a label to work in.
    """
    entity_id = require_str(params, "entity_id")
    # Short-circuited on the label, so an ordinary script pays neither the file read nor the walk.
    if is_hearth_scene(hass, entity_id) and (violations := await script_run_violations(hass, entity_id)):
        raise RpcError("method_not_allowed", "; ".join(violations))
    return await _run_entity(hass, params, "script")


def register(d: Dispatcher) -> None:
    d.register("devices.call", devices_call)
    d.register("scenes.activate", scenes_activate)
    d.register("scripts.run", scripts_run)
