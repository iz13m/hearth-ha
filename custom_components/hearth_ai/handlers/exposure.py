"""Choosing what Hearth may see (AgDR-0024).

Assist exposure is the boundary the *whole* tool surface is filtered on: `entities.list`,
`states.get`, `devices.call`, `scenes.activate` and `scripts.run` all consult it. Until now it could
only be changed in Home Assistant.

Home Assistant shares light, switch, cover, climate, fan, media_player and scene by default and does
not share scripts, helpers or most sensors, so this matters in both directions — and un-sharing is
the direction with no other route: it is the only way from the app to take a device back out of
everything Hearth can see, the assistant's inventory included.

Three rules make that safe to do from a phone:

1. **Listing a candidate is not reading it.** `entities.exposable` returns an entity id, a name, a
   room and nothing else — no state, no attributes. Choosing what to share must not itself be a way
   to read what you have not shared.
2. **The off-limits domains can never be *added*.** Locks, cameras, alarms, trackers and person
   entities cannot be shared, so no amount of exposing reaches something the policy would refuse to
   operate anyway.
3. **But they can always be *removed*.** The denylist is one-directional on purpose. An owner who
   exposed an alarm panel to Home Assistant's own Assist years ago has it visible to Hearth today,
   and refusing to un-share it would mean the one screen offering to take it back said no for the
   most sensitive entity in the house. A rule that blocks narrowing is worse than no rule.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
    async_should_expose,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..rpc import Dispatcher, RpcError
# `OFF_LIMITS` is shared with the history handler; it lives next to the domain lists it is built
# from. Consulted here only when *adding*. See rule 3 in the module docstring.
from .registry import ASSISTANT, OFF_LIMITS, _category, _device_name, _entity_area

MAX_IDS = 100
DEFAULT_LIMIT = 500

# The hub validates this too; repeated here because both sides enforce, always (invariant 1).
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _int_param(params: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise RpcError("invalid_params", f"{key} must be an integer between {lo} and {hi}")
    return value


def _str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 255:
        raise RpcError("invalid_params", f"{key} must be a string")
    return value


def _shareable(entity_id: str) -> bool:
    """Whether this entity may ever be *added*. Un-sharing ignores this by design."""
    return entity_id.split(".", 1)[0] not in OFF_LIMITS


def _refusal(hass: HomeAssistant, entity_id: str, ent_reg: er.EntityRegistry, *, expose: bool) -> str | None:
    """Why this entity may not be changed, or None when it may.

    Tolerates an entity with no registry entry, because the read path does: a template or MQTT light
    declared in YAML without a `unique_id` has a state and no entry, and Home Assistant exposes it by
    default. Refusing those here would have meant the devices Hearth most visibly *can* see were the
    ones this screen could neither list nor take back.
    """
    if not ENTITY_ID_RE.match(entity_id):
        return "not an entity id"
    domain = entity_id.split(".", 1)[0]
    if expose and domain in OFF_LIMITS:
        # Not "you lack permission": Hearth can never operate these, so sharing one would only
        # promise something the action policy refuses later.
        return f"Hearth never works with {domain} entities"
    ent = ent_reg.async_get(entity_id)
    # A registry lookup also resolves an entry *id*; make sure we got the entity we asked about.
    if ent is not None and ent.entity_id != entity_id:
        return "no such device"
    if ent is None and hass.states.get(entity_id) is None:
        return "no such device"
    if ent is not None and ent.disabled_by:
        return "disabled in Home Assistant"
    if ent is not None and ent.hidden_by:
        return "hidden in Home Assistant"
    return None


def _should_expose(hass: HomeAssistant, entity_id: str) -> bool:
    try:
        return async_should_expose(hass, ASSISTANT, entity_id)
    except Exception:  # noqa: BLE001 - exposure store missing => report as not shared
        return False


async def entities_exposable(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every device this screen can offer, and whether Hearth sees it today.

    Carries no state and no attributes on purpose. Paged on entity_id like `entities.list`, with the
    same `domain` and `query` filters — without them a home larger than the page ceiling would have
    devices that simply could not be reached from the app.

    An off-limits entity appears **only when it is already exposed**, so an alarm panel someone
    shared long ago can be found and taken back, while one that was never shared is never offered.
    """
    limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, 500)
    after = _str_param(params, "after")
    domain = _str_param(params, "domain")
    query = (_str_param(params, "query") or "").strip().lower()
    # One physical device's entities, for the note on its screen (AgDR-0035). Checked against the
    # registry entry, so an entity with no entry is never on any device.
    device_id = _str_param(params, "device_id")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    rows: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(), key=lambda s: s.entity_id):
        entity_id = state.entity_id
        if after is not None and entity_id <= after:
            continue
        if domain is not None and state.domain != domain:
            continue
        if _refusal(hass, entity_id, ent_reg, expose=False) is not None:
            continue
        shareable = _shareable(entity_id)
        exposed = _should_expose(hass, entity_id)
        # Rule 3: an off-limits entity is listed only so that it can be removed.
        if not shareable and not exposed:
            continue
        if query and query not in entity_id.lower() and query not in (state.name or "").lower():
            continue
        ent = ent_reg.async_get(entity_id)
        if device_id is not None and (ent is None or ent.device_id != device_id):
            continue
        rows.append(
            {
                "entity_id": entity_id,
                "name": state.name,
                "domain": state.domain,
                "area_id": _entity_area(ent, dev_reg),
                "device_name": _device_name(ent, dev_reg),
                "exposed": exposed,
                # False means "can be taken away but never given back" — the app draws it as one-way.
                "can_share": shareable,
                # Registry metadata, not state: which device, and what Home Assistant calls it there.
                # Lets the hub notice a sensor whose settings are shared and whose reading is not.
                "device_id": ent.device_id if ent else None,
                "entity_category": _category(ent),
            }
        )
        if len(rows) >= limit:
            break
    return rows


async def entities_expose(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Share these devices with Assist, or stop sharing them.

    Partial success, not all-or-nothing: asking for eight and having one refused changes the seven and
    names the one that did not, so the app can explain instead of failing the lot. That is also why
    every per-entity failure is caught — one entity whose exposure store is in a bad state must not
    discard the record of the ones that did change.
    """
    ids = params.get("entity_ids")
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        raise RpcError("invalid_params", f"entity_ids must be 1-{MAX_IDS} entity ids")
    expose = params.get("expose")
    if not isinstance(expose, bool):
        raise RpcError("invalid_params", "expose must be true or false")

    ent_reg = er.async_get(hass)
    changed: list[str] = []
    refused: list[dict[str, str]] = []
    for entity_id in ids:
        if not isinstance(entity_id, str):
            raise RpcError("invalid_params", "entity_ids must be strings")
        reason = apply_exposure(hass, entity_id, expose, ent_reg)
        if reason is not None:
            refused.append({"entity_id": entity_id, "reason": reason})
            continue
        changed.append(entity_id)
    return {"changed": changed, "refused": refused}


def apply_exposure(hass: HomeAssistant, entity_id: str, expose: bool, ent_reg: er.EntityRegistry) -> str | None:
    """Share one entity with Assist, or stop. Returns why it was refused, or None on success.

    The Hearth panel and the hub's `entities.expose` both go through here, so the two can never come
    to different answers about what is off limits — which is the one rule in this file that matters.
    """
    reason = _refusal(hass, entity_id, ent_reg, expose=expose)
    if reason is not None:
        return reason
    try:
        async_expose_entity(hass, ASSISTANT, entity_id, expose)
    except Exception as err:  # noqa: BLE001 - one bad entity must not lose the rest
        return str(err) or "could not be changed"
    return None


def register(d: Dispatcher) -> None:
    d.register("entities.exposable", entities_exposable)
    d.register("entities.expose", entities_expose)
