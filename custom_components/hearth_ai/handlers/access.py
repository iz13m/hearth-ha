"""Doors and other openings (AgDR-0025).

Hearth's rule used to be that locks are refused *everywhere, for everyone*. That rule came from a
security review written when the only thing on the other end was an LLM authoring automations, and
the mobile app inherited it without anyone asking whether a person tapping unlock in an
authenticated app is the same risk as a model deciding to. It is not.

So the denial moved from the product to the model. What that means here:

- **The model still cannot touch a lock.** `lock` stays in `DENIED_ACTION_DOMAINS`, so `devices.call`
  refuses it and `find_policy_violations` still rejects a lock inside an authored automation, script
  or scene. That was the point of AgDR-0012 and none of it changes.
- **A person can**, through this module, and only when the owner switched on `access.control` — which
  is deliberately a *different* toggle from `devices.control`, so "operate my lights" never silently
  meant "open my front door".

This module is the whole of that widening, and it is gated by its own capability in `rpc.py`. That
gate is the load-bearing one: the integration cannot see *who* asked. `surface` is a hub-side idea
and is deliberately not on the wire, because a forgeable "this came from the app" flag would be a
flag that opens doors. So the separation has to be something this side can check alone, and a
capability the owner set in their own Home Assistant is exactly that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntityFeature
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import _device_name, _entity_area

DOMAIN = "lock"
DEFAULT_LIMIT = 500

# `lock.open` releases a latch; `lock.lock` / `lock.unlock` throw the bolt.
SERVICE_FOR = {"lock": "lock", "unlock": "unlock", "open": "open"}

# Where each action is trying to get to. Asking a locked door to lock fires no state change at all,
# so without this the settle wait would burn its whole timeout on a no-op and the tap would feel hung.
RESTING_STATE = {"lock": "locked", "unlock": "unlocked", "open": "open"}

# States that mean "still moving". A lock reports one of these before it reports where it ended up.
IN_FLIGHT = ("locking", "unlocking", "opening")

# How long to wait for the lock to report what it did. Longer than the device settle used for lights:
# a Z-Wave or Zigbee deadbolt writes `locking` first and `locked` a second or two later, and
# answering with the transitional state makes the app look like the tap failed.
SETTLE_TIMEOUT_S = 3.0


def _entity(hass: HomeAssistant, entity_id: str):
    """The lock this id names, or a refusal.

    **Assist exposure is deliberately not the gate here**, which is a departure from AgDR-0012 worth
    stating plainly. Exposure means "what the assistant may see", and a lock is the one thing the
    assistant may never see — `HIDDEN_DOMAINS` still hides it from `entities.list` whatever the owner
    does. So requiring it would be an empty ritual: exposing a lock would reveal it to nothing, and
    the only effect would be sending the owner to Home Assistant to satisfy a check — the exact trip
    this work exists to remove. The Hearth app cannot even offer it, because AgDR-0024's exposure
    screen refuses locks outright.

    The owner's consent is the `access.control` capability instead: a switch in their own Home
    Assistant, off by default, that says this and nothing else. That is more specific than exposure,
    not less.
    """
    if entity_id.split(".", 1)[0] != DOMAIN:
        raise RpcError("invalid_params", "not a lock")
    state = hass.states.get(entity_id)
    if state is None:
        raise RpcError("not_found", "no such lock")
    return state


def _features(state: Any) -> int:
    """`supported_features` as a number, whatever the integration actually put there."""
    try:
        return int(state.attributes.get("supported_features") or 0)
    except (TypeError, ValueError):
        return 0


def _dto(hass: HomeAssistant, state: Any, ent: Any, dev_reg: Any) -> dict[str, Any]:
    return {
        "entity_id": state.entity_id,
        "name": state.name,
        "area_id": _entity_area(ent, dev_reg),
        "device_name": _device_name(ent, dev_reg),
        "state": state.state,
        "can_open": bool(_features(state) & LockEntityFeature.OPEN),
        # `code_format` is a regex when the lock wants a PIN. Hearth never carries one, so this is
        # really "the owner must have set a default code in Home Assistant for this to work".
        "needs_code": state.attributes.get("code_format") is not None,
    }


async def access_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every lock in the home.

    Its own method rather than widening `entities.list`, because that one also feeds the model's
    inventory — making locks visible there would hand them to exactly the caller this whole design is
    keeping them from. See `_entity` for why Assist exposure is not consulted.
    """
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    limit = params.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise RpcError("invalid_params", "limit must be an integer between 1 and 500")
    after = params.get("after")
    if after is not None and not isinstance(after, str):
        raise RpcError("invalid_params", "after must be a string")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    rows: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(DOMAIN), key=lambda s: s.entity_id):
        if after is not None and state.entity_id <= after:
            continue
        rows.append(_dto(hass, state, ent_reg.async_get(state.entity_id), dev_reg))
        if len(rows) >= limit:
            break
    return rows


async def access_operate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Lock, unlock, or open a latch — and wait long enough to say what actually happened."""
    from homeassistant.helpers import event as ev

    entity_id = require_str(params, "entity_id")
    action = require_str(params, "action")
    if action not in SERVICE_FOR:
        raise RpcError("invalid_params", "action must be lock, unlock or open")
    state = _entity(hass, entity_id)

    if action == "open" and not _features(state) & LockEntityFeature.OPEN:
        raise RpcError("invalid_params", "this lock cannot open a latch")

    import asyncio

    settled = asyncio.get_running_loop().create_future()

    @ev.callback
    def _changed(event) -> None:  # noqa: ANN001
        new = event.data.get("new_state")
        # Wait for a resting state: `locking`/`unlocking` is the lock telling us it is still moving.
        if new is not None and new.state not in IN_FLIGHT and not settled.done():
            settled.set_result(new.state)

    unsub = ev.async_track_state_change_event(hass, [entity_id], _changed)
    try:
        try:
            await hass.services.async_call(
                DOMAIN, SERVICE_FOR[action], {ATTR_ENTITY_ID: entity_id}, blocking=True, context=None
            )
        except HomeAssistantError as err:
            # A lock that wants a code the owner never set lands here, and the message is the useful
            # part — it tells them to set a default code in Home Assistant.
            raise RpcError("ha_error", str(err) or "the lock refused") from err
        # Already where it was asked to go, and no change is coming: do not wait for one.
        now = hass.states.get(entity_id)
        if now is not None and now.state == RESTING_STATE[action]:
            return {"entity_id": entity_id, "state": now.state}
        try:
            final = await asyncio.wait_for(settled, SETTLE_TIMEOUT_S)
        except TimeoutError:
            # `blocking=True` only waits for the service handler, not for the bolt (#25). Report what
            # we can see rather than pretending: a lock that is still moving says so.
            current = hass.states.get(entity_id)
            final = current.state if current else None
    finally:
        unsub()
    return {"entity_id": entity_id, "state": final}


def register(d: Dispatcher) -> None:
    d.register("access.list", access_list)
    d.register("access.operate", access_operate)
