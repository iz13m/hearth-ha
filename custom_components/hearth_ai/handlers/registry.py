"""Read-only catalog handlers: areas, entities, states, services."""

from __future__ import annotations

from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.service import async_get_all_descriptions

from ..rpc import Dispatcher, RpcError
from .common import plain, require_str

# Must match ATTRIBUTE_DENYLIST in packages/shared.
ATTRIBUTE_DENYLIST: frozenset[str] = frozenset(
    {
        "access_token",
        "token",
        "entity_picture",
        "latitude",
        "longitude",
        "gps_accuracy",
        "stream_source",
        "url",
        "video_url",
        "image_url",
    }
)
# Domains whose state/attributes are never exposed, even read-only.
HIDDEN_DOMAINS: frozenset[str] = frozenset({"camera", "device_tracker", "person", "image", "lock"})

MAX_ATTR_STR = 500
# The AI only sees what the user exposed to Assist (Settings > Voice assistants > Expose).
ASSISTANT = "conversation"


def _exposed(hass: HomeAssistant, entity_id: str) -> bool:
    try:
        return async_should_expose(hass, ASSISTANT, entity_id)
    except Exception:  # noqa: BLE001 - exposure store missing => fail closed
        return False


def filter_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if k in ATTRIBUTE_DENYLIST or k.endswith("_token") or k.endswith("_url"):
            continue
        v = plain(v)
        if isinstance(v, str) and len(v) > MAX_ATTR_STR:
            v = v[:MAX_ATTR_STR] + "…"
        out[k] = v
    return out


async def areas_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    reg = ar.async_get(hass)
    return [
        {"area_id": a.id, "name": a.name, "floor_id": a.floor_id}
        for a in sorted(reg.async_list_areas(), key=lambda a: a.name.lower())
    ]


def _entity_area(ent: er.RegistryEntry | None, dev_reg: dr.DeviceRegistry) -> str | None:
    if ent is None:
        return None
    if ent.area_id:
        return ent.area_id
    if ent.device_id and (dev := dev_reg.async_get(ent.device_id)):
        return dev.area_id
    return None


async def entities_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    domain = params.get("domain")
    area_id = params.get("area_id")
    query = (params.get("query") or "").lower()
    limit = params.get("limit", 200)
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise RpcError("invalid_params", "limit must be 1..500")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    out: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(), key=lambda s: s.entity_id):
        if state.domain in HIDDEN_DOMAINS:
            continue
        if domain and state.domain != domain:
            continue
        ent = ent_reg.async_get(state.entity_id)
        if ent is not None and (ent.hidden_by or ent.disabled_by):
            continue
        if not _exposed(hass, state.entity_id):
            continue
        entity_area = _entity_area(ent, dev_reg)
        if area_id and entity_area != area_id:
            continue
        name = state.name
        if query and query not in state.entity_id.lower() and query not in (name or "").lower():
            continue
        out.append(
            {
                "entity_id": state.entity_id,
                "name": name,
                "domain": state.domain,
                "area_id": entity_area,
                "device_id": ent.device_id if ent else None,
                "state": state.state,
                "attributes": filter_attributes(dict(state.attributes)),
            }
        )
        if len(out) >= limit:
            break
    return out


async def states_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = require_str(params, "entity_id")
    domain = entity_id.split(".", 1)[0]
    if domain in HIDDEN_DOMAINS:
        raise RpcError("not_found", "entity not available")
    state = hass.states.get(entity_id)
    if state is None or not _exposed(hass, entity_id):
        raise RpcError("not_found", f"unknown entity {entity_id}")
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": filter_attributes(dict(state.attributes)),
        "last_changed": state.last_changed.isoformat(),
    }


async def services_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Catalog only — lets the model write correct `action:` blocks. Nothing here calls a service."""
    domain = params.get("domain")
    descriptions = await async_get_all_descriptions(hass)
    out: list[dict[str, Any]] = []
    for dom, services in sorted(descriptions.items()):
        if domain and dom != domain:
            continue
        if dom in HIDDEN_DOMAINS:
            continue
        for name, desc in sorted(services.items()):
            fields = desc.get("fields", {}) if isinstance(desc, dict) else {}
            out.append(
                {
                    "domain": dom,
                    "service": name,
                    "description": desc.get("description") if isinstance(desc, dict) else None,
                    "fields": {
                        k: {
                            "description": (v or {}).get("description"),
                            "required": bool((v or {}).get("required", False)),
                            "example": plain((v or {}).get("example")),
                        }
                        for k, v in fields.items()
                    },
                }
            )
    return out


def register(d: Dispatcher) -> None:
    d.register("areas.list", areas_list)
    d.register("entities.list", entities_list)
    d.register("states.get", states_get)
    d.register("services.list", services_list)
