"""Operating the home: direct service calls, scene activation, running scripts.

Gated by the `devices.control` and `routines.run` capabilities, which are off until the
owner enables them. Every call is checked against the same policy the hub applies, so a
compromised hub still cannot reach a lock, alarm, camera, or the host.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant

from ..policy import find_service_call_violations
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import _exposed

# Service calls can legitimately take a while (thermostats, media players); scripts return
# as soon as they start.
CALL_TIMEOUT_S = 30
MAX_DATA_KEYS = 32
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
            await hass.services.async_call(domain, service, {**data, "entity_id": raw_ids}, blocking=True)
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
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await hass.services.async_call(expected_domain, "turn_on", {"entity_id": entity_id}, blocking=True)
    except TimeoutError as err:
        raise RpcError("timeout", f"{entity_id} did not start within {CALL_TIMEOUT_S}s") from err
    return {"entity_id": entity_id, "state": _state_of(hass, entity_id)}


async def scenes_activate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    return await _run_entity(hass, params, "scene")


async def scripts_run(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Runs whatever the user wrote in that script — including actions Hearth itself may not perform."""
    return await _run_entity(hass, params, "script")


def register(d: Dispatcher) -> None:
    d.register("devices.call", devices_call)
    d.register("scenes.activate", scenes_activate)
    d.register("scripts.run", scripts_run)
