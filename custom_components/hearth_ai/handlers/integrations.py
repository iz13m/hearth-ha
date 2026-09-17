"""Adding and configuring integrations by driving Home Assistant's own config flows.

Gated by the opt-in `integrations.manage` capability. Hearth fills ordinary fields such as
host names and ports, but **never** a password, token, or API key: those are refused and the
half-finished flow is left for the person to complete in Home Assistant's own UI, where it
appears under Settings -> Devices & services.

The flow machinery itself — serialising a form, remembering which of its fields were secret, and
policing what comes back — lives in `flows.py`, because `handlers/helpers.py` drives the same
machinery pointed at the helper half of `async_get_config_flows`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_config_flows, async_get_integrations

from ..rpc import Dispatcher, RpcError
from .common import require_str
from .flows import (
    DENIED_DOMAINS,
    check_domain,
    check_flow_input,
    check_secrets,
    forget_fields,
    shape,
)

FLOW_TIMEOUT_S = 45
MAX_RESULTS = 25


def _flow(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    for flow in hass.config_entries.flow.async_progress():
        if flow["flow_id"] == flow_id:
            return flow
    raise RpcError("not_found", "that setup flow is no longer in progress")


async def integrations_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "entry_id": e.entry_id,
            "domain": e.domain,
            "title": e.title,
            "state": e.state.value if isinstance(e.state, ConfigEntryState) else str(e.state),
            "source": e.source,
        }
        for e in sorted(hass.config_entries.async_entries(), key=lambda e: (e.domain, e.title))
    ]


async def integrations_discovered(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Setup flows Home Assistant already has waiting, including things it found on the network."""
    out = []
    for flow in hass.config_entries.flow.async_progress(include_uninitialized=True):
        context = flow.get("context") or {}
        out.append(
            {
                "flow_id": flow["flow_id"],
                "domain": flow["handler"],
                "name": (context.get("title_placeholders") or {}).get("name"),
                "source": str(context.get("source", "unknown")),
                "step_id": flow.get("step_id"),
            }
        )
    return out


async def integrations_available(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    query = require_str(params, "query").lower()
    domains = sorted(d for d in await async_get_config_flows(hass) if d not in DENIED_DOMAINS)
    configured = {e.domain for e in hass.config_entries.async_entries()}
    # Match on the domain first, then on the human name for the ones that survive.
    shortlist = [d for d in domains if query in d.replace("_", " ") or query in d][:MAX_RESULTS]
    if len(shortlist) < MAX_RESULTS:
        integrations = await async_get_integrations(hass, [d for d in domains if d not in shortlist])
        for domain, integration in integrations.items():
            if isinstance(integration, Exception):
                continue
            if query in integration.name.lower():
                shortlist.append(domain)
            if len(shortlist) >= MAX_RESULTS:
                break
    resolved = await async_get_integrations(hass, shortlist)
    return [
        {
            "domain": d,
            "name": resolved[d].name if not isinstance(resolved.get(d), Exception) else d,
            "already_configured": d in configured,
        }
        for d in shortlist
        if d in resolved
    ]


async def integrations_flow_start(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    domain = check_domain(require_str(params, "domain"))
    if domain not in await async_get_config_flows(hass):
        raise RpcError("not_found", f"{domain} cannot be set up from the UI (it has no config flow)")
    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            result = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
    except TimeoutError as err:
        raise RpcError("timeout", f"{domain} did not respond within {FLOW_TIMEOUT_S}s") from err
    return shape(hass, domain, result)


async def integrations_flow_step(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    flow_id = require_str(params, "flow_id")
    user_input = params.get("input") or {}
    if not isinstance(user_input, dict):
        raise RpcError("invalid_params", "input must be an object")
    flow = _flow(hass, flow_id)
    domain = str(flow["handler"])
    check_domain(domain)

    check_secrets(hass, flow_id, user_input)
    # A form field can be an action sequence — Home Assistant's `template` helper takes whole ones,
    # and really runs them — so it is policed like the authoring surface it is (AgDR-0038).
    check_flow_input(user_input)

    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    except TimeoutError as err:
        raise RpcError("timeout", f"the {domain} setup step did not finish within {FLOW_TIMEOUT_S}s") from err
    except Exception as err:  # noqa: BLE001 - flows raise their own errors for bad input
        raise RpcError("ha_error", f"{type(err).__name__}: {err}") from err
    return shape(hass, domain, result)


async def integrations_flow_abort(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    flow_id = require_str(params, "flow_id")
    _flow(hass, flow_id)
    hass.config_entries.flow.async_abort(flow_id)
    forget_fields(hass, flow_id)
    return {}


def register(d: Dispatcher) -> None:
    d.register("integrations.list", integrations_list)
    d.register("integrations.discovered", integrations_discovered)
    d.register("integrations.available", integrations_available)
    d.register("integrations.flow_start", integrations_flow_start)
    d.register("integrations.flow_step", integrations_flow_step)
    d.register("integrations.flow_abort", integrations_flow_abort)
