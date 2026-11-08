"""Operating the home: direct service calls, scene activation, running scripts.

Gated by the `devices.control` and `routines.run` capabilities, which are off until the
owner enables them. Every call is checked against the same policy the hub applies, so a
compromised hub still cannot reach a lock, alarm, camera, or the host.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from ..labels import is_hearth_scene
from ..policy import TARGET_KEYS, VALVE_DOMAIN, find_service_call_violations
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .remotes import remote_call_violations
from .registry import OFF_LIMITS, GateMap, MembershipUnwalkable, _exposed, _gates, effective_domain, expand_members, off_limits, visible
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

    `_seen` is a `@callback` so Home Assistant runs it on the event loop. Without the decorator HA
    treats a plain function as an executor job and runs it on a worker thread, where
    `asyncio.Event.set()` is not safe (#649). Under asyncio's debug mode, which Home Assistant's test
    harness turns on, `set()` marks the waiter's future done and then raises from `call_soon`, so the
    waiting task is never scheduled and neither the settle timeout nor `CALL_TIMEOUT_S` brings it back.
    Outside debug mode nothing raises, but the loop is not woken and `waiting` is shared across threads.
    """
    waiting = set(entity_ids)
    done = asyncio.Event()

    @callback
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


async def devices_call(hass: HomeAssistant, params: dict[str, Any], capabilities: frozenset[str] | None = None) -> dict[str, Any]:
    """`capabilities` is the dispatcher's enabled set (None = everything), for `member_violations`."""
    domain = require_str(params, "domain")
    service = require_str(params, "service")
    raw_ids = params.get("entity_id")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(e, str) for e in raw_ids):
        raise RpcError("invalid_params", "entity_id must be a non-empty list of entity ids")
    if len(raw_ids) > 50:
        raise RpcError("invalid_params", "at most 50 entities per call")

    if violations := find_service_call_violations(domain, service, raw_ids, params.get("data")):
        raise RpcError("method_not_allowed", "; ".join(violations))

    if not hass.services.has_service(domain, service):
        raise RpcError("not_found", f"no such service {domain}.{service}")

    ent_reg = er.async_get(hass)
    gates = _gates(hass)
    for entity_id in raw_ids:
        if hass.states.get(entity_id) is None:
            raise RpcError("not_found", f"unknown entity {entity_id}")
        # The read rule, so the model cannot operate what `states.get` would not show it (#642): an
        # entity in `HIDDEN_DOMAINS`, one the owner did not expose, or one the registry hid or disabled
        # is refused in the words an unknown id gets, so the model cannot tell which ids exist but are
        # kept from it (#648).
        # `visible()` holds the exposure clause, so it is not checked again here (#259). One refused
        # id refuses the whole call, before anything is sent.
        if not visible(hass, entity_id, ent_reg.async_get(entity_id), gates):
            raise RpcError("not_found", f"unknown entity {entity_id}")
        # A switch a `switch_as_x` helper presents as a valve, or another wrapper on that switch,
        # still moves what the helper shows through its own services (#643). A lock or a siren never
        # gets here: both are in `OFF_LIMITS` (#605), so `visible()` above already refused it. Keyed
        # on the id, not on `domain`, because `homeassistant.turn_on` and `switch.toggle` reach the
        # same relay. Both directions are refused
        # on a valve: the helper's `invert` option decides which switch service closes it, so the
        # `valve.*` wrapper is the one way to close it, and it stays closable.
        gate = effective_domain(hass, entity_id, gates)
        if gate != entity_id.split(".", 1)[0].lower():
            raise RpcError("method_not_allowed", f"{entity_id}: this drives a {gate}; Hearth refuses it through any other domain's service")

    # A remote command is sent or taught only to a remote whose integration plays stored names alone (#568, F1).
    # After `visible()`, so an id the model may not see is still refused as unknown rather than named.
    if problems := remote_call_violations(hass, domain, service, raw_ids):
        raise RpcError("method_not_allowed", "; ".join(problems))

    for entity_id in raw_ids:
        if problems := await member_violations(hass, domain, entity_id, gates, capabilities):
            raise RpcError("method_not_allowed", "; ".join(problems))

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


async def member_violations(
    hass: HomeAssistant, domain: str, entity_id: str, gates: GateMap, capabilities: frozenset[str] | None = None
) -> list[str]:
    """Why a service call on `entity_id` may not fan out to what it holds (#657). Empty means it may.

    A service called on an old-style `group.*`, or on one of Home Assistant's entity groups, acts on
    every member, recursively: `homeassistant.turn_on group.evening` calls `scene.turn_on` on a scene
    in it, `script.turn_on` on a script (which runs it) and `automation.turn_on` on an automation
    (which switches it on). None of that passes the checks the member would meet if it were named, so
    each member is put through them here, and one refusal refuses the whole call before anything is
    sent:

    - **Off-limits**, by literal or effective domain: a lock, siren or alarm panel in a group is
      refused like one named directly, without naming it (#648). `homeassistant.turn_on` reaches a
      `siren` this way; a lock has no `turn_on`, but the rule does not depend on which service names it.
    - **A valve** is moved only by a `valve.*` service, as when it is named (#527): `homeassistant.toggle`
      forwards to `valve.toggle`.
    - **A scene or a script** is refused outright unless `routines.run` is on. `devices.call` needs
      only `devices.control`, and running a routine is the other switch's to grant; AgDR-0005's
      licence for a script to do whatever its owner wrote is a licence for `scripts.run`, which
      `routines.run` gates. `capabilities` is None only where everything is enabled (tests, legacy).
    - **A scene** must be shared with Assist, not hidden or disabled in the registry (#644), and pass its own run check (`scene_run_violations`,
      AgDR-0012), exactly as `scenes.activate` asks.
    - **A script** must be shared with Assist and not hidden or disabled (#644), and one Hearth presents as a scene must pass its run
      check, exactly as `scripts.run` asks (AgDR-0005, AgDR-0044).
    - **An automation** is refused: switching one on or off is `automations.set_enabled`'s alone, an
      app-only path, and `automation.turn_on`/`turn_off`/`toggle` are refused by name (AgDR-0042).

    The check is made whatever service the call names, so `light.turn_on` on a mixed group that holds
    a scene or a valve is refused although that service would not reach them. That is deliberate:
    it fails closed, and one rule for every service cannot be talked round by picking the service.

    A scene's own `entity_id` attribute lists what it *sets*, which its run check judges, so the walk
    does not descend into it. A `switch_as_x` gate on any member is already the group's own
    (`GateMap` walks the same membership), so `devices_call` has refused that before this runs.
    """
    try:
        members = expand_members(hass, entity_id, stop=frozenset({"scene", "script", "automation"}))
    except MembershipUnwalkable as err:
        if err.cycle:
            # Home Assistant's own expansion would recurse until Python's limit on such a call.
            return [f"{entity_id}: this group reaches groups that hold each other, so what it reaches has no end"]
        return [f"{entity_id}: this group holds too many entities, or groups nested too deep, for Hearth to check what it reaches"]
    problems: list[str] = []
    for member in members:
        literal = member.split(".", 1)[0]
        # Both halves, as `visible()` has them: a gate replaces the effective domain, and ruling 9 hands
        # a valve-wrapped group's gate down to its members, so a literal `lock.*` member of such a group
        # has the effective domain `valve`, which `off_limits` alone would let through.
        if off_limits(hass, member, gates) or literal in OFF_LIMITS:
            problems.append(f"{entity_id}: this group holds something Hearth never lets a model reach")
        elif literal == VALVE_DOMAIN and domain.lower() != VALVE_DOMAIN:
            problems.append(f"{entity_id}: this group holds {member}, and a valve is moved only by its own services")
        elif literal == "automation":
            problems.append(f"{entity_id}: this group holds {member}, and a call on the group would switch that automation on or off")
        elif literal in ("scene", "script") and capabilities is not None and "routines.run" not in capabilities:
            problems.append(f"{entity_id}: this group holds {member}, and a call on the group would run it; running scenes and scripts is disabled in the Azka AI options")
        elif literal in ("scene", "script") and hass.states.get(member) is not None:
            if not _exposed(hass, member):
                problems.append(f"{entity_id}: this group holds {member}, which is not shared with Assist, and a call on the group would run it")
            elif not visible(hass, member, er.async_get(hass).async_get(member), gates):
                # Exposed, but hidden or disabled in the registry: `scenes.activate` and `scripts.run`
                # refuse it (#644), so a group holding it must not run it either.
                problems.append(f"{entity_id}: this group holds {member}, which Hearth cannot see, and a call on the group would run it")
            elif literal == "scene" and (found := await scene_run_violations(hass, member)):
                problems += [f"{entity_id}: this group holds {member}, which may not be activated: {v}" for v in found]
            elif literal == "script" and is_hearth_scene(hass, member) and (found := await script_run_violations(hass, member)):
                problems += [f"{entity_id}: this group holds {member}, which may not be run: {v}" for v in found]
    return list(dict.fromkeys(problems))


def _require_runnable(hass: HomeAssistant, params: dict[str, Any], expected_domain: str) -> str:
    """The routine's id, if the model may run it; otherwise the refusal an unknown id gets.

    Asked before anything reads what the routine contains, so a hidden or unshared scene whose
    contents break the policy answers like an unknown id, not with reasons that say it exists (#644).
    """
    entity_id = require_str(params, "entity_id")
    if not entity_id.startswith(f"{expected_domain}."):
        raise RpcError("invalid_params", f"entity_id must be a {expected_domain} entity")
    if hass.states.get(entity_id) is None:
        raise RpcError("not_found", f"unknown entity {entity_id}")
    # The read rule, as `devices_call` asks it (#642, #644): exposure is the owner's consent, and a
    # scene or script the registry hid or disabled is kept from the model even when exposed, so
    # running one is not the way round "hidden wins". Refused in the words an unknown id gets (#648).
    # `scenes.list` and `scripts.list` ask the same rule for `can_run`, so the app never offers a Run
    # button this refuses.
    if not visible(hass, entity_id, er.async_get(hass).async_get(entity_id)):
        raise RpcError("not_found", f"unknown entity {entity_id}")
    return entity_id


async def _run_entity(hass: HomeAssistant, params: dict[str, Any], expected_domain: str) -> dict[str, Any]:
    entity_id = _require_runnable(hass, params, expected_domain)
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
    entity_id = _require_runnable(hass, params, "scene")
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
    entity_id = _require_runnable(hass, params, "script")
    # Short-circuited on the label, so an ordinary script pays neither the file read nor the walk.
    if is_hearth_scene(hass, entity_id) and (violations := await script_run_violations(hass, entity_id)):
        raise RpcError("method_not_allowed", "; ".join(violations))
    return await _run_entity(hass, params, "script")


def register(d: Dispatcher) -> None:
    async def _devices_call(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
        # Bound to this dispatcher's capability set, read at call time, so a group cannot run a
        # routine where `routines.run` is off (#657).
        return await devices_call(hass, params, d.capabilities)

    d.register("devices.call", _devices_call)
    d.register("scenes.activate", scenes_activate)
    d.register("scripts.run", scripts_run)
