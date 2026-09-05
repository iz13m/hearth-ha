"""Adding and configuring integrations by driving Home Assistant's own config flows.

Gated by the opt-in `integrations.manage` capability. Hearth fills ordinary fields such as
host names and ports, but **never** a password, token, or API key: those are refused and the
half-finished flow is left for the person to complete in Home Assistant's own UI, where it
appears under Settings -> Devices & services.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_config_flows, async_get_integrations

from ..rpc import Dispatcher, RpcError
from .common import require_str

_LOGGER = logging.getLogger(__name__)

FLOW_TIMEOUT_S = 45
MAX_RESULTS = 25

# Integrations Hearth will not set up on the user's behalf: they grant host-level power, or
# they are Hearth itself.
DENIED_DOMAINS: frozenset[str] = frozenset(
    {"hearth_ai", "hassio", "backup", "homeassistant", "command_line", "shell_command", "python_script", "ffmpeg"}
)

# Field names that hold credentials even when the schema does not say so (older flows use a
# bare string for passwords). Matched case-insensitively against the field name.
_SECRET_NAME = re.compile(r"password|passwd|token|api_key|apikey|secret|credential|client_secret|pin|passcode|access_key", re.I)

# Serialising a flow's form: Home Assistant 2026+ uses probatio, older releases voluptuous-serialize.
try:  # pragma: no cover - depends on the HA version at runtime
    from probatio import to_field_list as _serialize_schema
except ImportError:  # pragma: no cover
    from voluptuous_serialize import convert as _serialize_schema


def _is_secret(field: dict[str, Any]) -> bool:
    name = str(field.get("name", ""))
    if _SECRET_NAME.search(name):
        return True
    selector = field.get("selector")
    if isinstance(selector, dict):
        text = selector.get("text")
        if isinstance(text, dict) and text.get("type") == "password":
            return True
    return False


def _flatten(field: dict[str, Any]) -> dict[str, Any]:
    """Home Assistant's serialisation -> the compact shape the hub's schema expects."""
    selector = field.get("selector") if isinstance(field.get("selector"), dict) else None
    kind = field.get("type")
    if not kind and selector:
        kind = next(iter(selector), "string")
    out: dict[str, Any] = {
        "name": str(field.get("name", "")),
        "type": str(kind or "string"),
        "required": bool(field.get("required", False)),
        "secret": _is_secret(field),
    }
    if (default := field.get("default")) is not None:
        out["default"] = default
    if (desc := field.get("description")) is not None:
        out["description"] = str(desc)
    options = field.get("options")
    if options is None and selector:
        for cfg in selector.values():
            if isinstance(cfg, dict) and "options" in cfg:
                options = cfg["options"]
                break
    if isinstance(options, list):
        out["options"] = [o if isinstance(o, (str, dict)) else str(o) for o in options][:50]
    return out


def _shape(hass: HomeAssistant, domain: str, result: dict[str, Any]) -> dict[str, Any]:
    """Turn a FlowResult into the wire shape, and remember which fields are secret."""
    out: dict[str, Any] = {"flow_id": str(result.get("flow_id", "")), "domain": domain, "type": str(result["type"].value)}
    rtype = result["type"]

    if rtype is FlowResultType.FORM:
        out["step_id"] = result.get("step_id")
        out["errors"] = {k: str(v) for k, v in (result.get("errors") or {}).items()}
        out["description_placeholders"] = {k: str(v) for k, v in (result.get("description_placeholders") or {}).items()} or None
        schema = result.get("data_schema")
        fields = []
        if schema is not None:
            try:
                fields = [_flatten(f) for f in _serialize_schema(schema, custom_serializer=cv.custom_serializer)]
            except Exception:  # noqa: BLE001 - a form we cannot describe is still a valid form
                _LOGGER.warning("could not serialise the %s form for step %s", domain, result.get("step_id"))
                fields = []
        out["fields"] = fields
        out["secret_fields"] = [f["name"] for f in fields if f["secret"]]
    elif rtype is FlowResultType.CREATE_ENTRY:
        out["title"] = result.get("title")
        entry = result.get("result")
        out["entry_id"] = getattr(entry, "entry_id", None)
    elif rtype is FlowResultType.ABORT:
        out["reason"] = result.get("reason")
    elif rtype is FlowResultType.MENU:
        out["step_id"] = result.get("step_id")
        options = result.get("menu_options") or []
        out["menu_options"] = list(options) if isinstance(options, (list, tuple)) else list(options.keys())
    return out


def _check_domain(domain: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", domain):
        raise RpcError("invalid_params", "domain must be a lowercase slug")
    if domain in DENIED_DOMAINS:
        raise RpcError("method_not_allowed", f"Hearth does not set up the {domain} integration")
    return domain


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
    domain = _check_domain(require_str(params, "domain"))
    if domain not in await async_get_config_flows(hass):
        raise RpcError("not_found", f"{domain} cannot be set up from the UI (it has no config flow)")
    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            result = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
    except TimeoutError as err:
        raise RpcError("timeout", f"{domain} did not respond within {FLOW_TIMEOUT_S}s") from err
    return _shape(hass, domain, result)


async def integrations_flow_step(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    flow_id = require_str(params, "flow_id")
    user_input = params.get("input") or {}
    if not isinstance(user_input, dict):
        raise RpcError("invalid_params", "input must be an object")
    flow = _flow(hass, flow_id)
    domain = str(flow["handler"])
    _check_domain(domain)

    # Refuse to relay credentials: the person finishes those steps in Home Assistant itself.
    refused = sorted(k for k in user_input if _SECRET_NAME.search(str(k)))
    schema = flow.get("data_schema")
    if schema is not None:
        try:
            for field in _serialize_schema(schema, custom_serializer=cv.custom_serializer):
                if _is_secret(field) and field.get("name") in user_input and field["name"] not in refused:
                    refused.append(str(field["name"]))
        except Exception:  # noqa: BLE001
            pass
    if refused:
        raise RpcError(
            "method_not_allowed",
            "Hearth will not send credentials on your behalf: "
            + ", ".join(sorted(refused))
            + ". The setup is waiting in Home Assistant under Settings > Devices & services; finish it there.",
        )

    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    except TimeoutError as err:
        raise RpcError("timeout", f"the {domain} setup step did not finish within {FLOW_TIMEOUT_S}s") from err
    except Exception as err:  # noqa: BLE001 - flows raise their own errors for bad input
        raise RpcError("ha_error", f"{type(err).__name__}: {err}") from err
    return _shape(hass, domain, result)


async def integrations_flow_abort(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    flow_id = require_str(params, "flow_id")
    _flow(hass, flow_id)
    hass.config_entries.flow.async_abort(flow_id)
    return {}


def register(d: Dispatcher) -> None:
    d.register("integrations.list", integrations_list)
    d.register("integrations.discovered", integrations_discovered)
    d.register("integrations.available", integrations_available)
    d.register("integrations.flow_start", integrations_flow_start)
    d.register("integrations.flow_step", integrations_flow_step)
    d.register("integrations.flow_abort", integrations_flow_abort)
