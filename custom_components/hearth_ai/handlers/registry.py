"""Read-only catalog handlers: areas, entities, states, services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.service import async_get_all_descriptions

from ..labels import hearth_labels
from ..policy import DENIED_ENTITY_DOMAINS
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
# Domains whose state/attributes are never exposed, even read-only. `image_processing` is here because
# its state is text read off a camera frame: a plate number, a recognised person's name, a QR code, or
# a count of people at the camera (#566).
HIDDEN_DOMAINS: frozenset[str] = frozenset({"camera", "device_tracker", "person", "image", "image_processing", "lock"})

# Never shareable, never listed, never read, never pushed. The union of two lists that are nearly but
# not quite the same: `HIDDEN_DOMAINS` has `image`; `DENIED_ENTITY_DOMAINS` is what the action policy
# refuses and has `alarm_control_panel` and `siren`, which `HIDDEN_DOMAINS` does not. Using only the
# first would let an admin share an alarm panel and read whether the house is armed — or, through
# `states.history`, when it was armed and when it was not, which is the same disclosure with dates on
# it. Sharing is not the only way in: an owner can expose an alarm panel in Home Assistant's own UI,
# which never asks Hearth, so `visible`, `entities_list`, `states_get` and `services_list` filter on
# this set too (#603). They filtered on `HIDDEN_DOMAINS` alone until then, so the model could read an
# exposed alarm panel's state while its history was refused.
#
# Lives here rather than in `exposure.py`, where it started, because every handler that reads or
# lists entities depends on it meaning exactly the same thing (AgDR-0036).
OFF_LIMITS: frozenset[str] = HIDDEN_DOMAINS | DENIED_ENTITY_DOMAINS

# Home Assistant's `switch_as_x` helper presents a switch as one of these, and the switch keeps its own
# services: `switch.turn_on` on the source moves the lock the helper shows (#643). These are the
# targets Hearth refuses or direction-gates; a `cover`, `fan` or `light` wrapper alone changes nothing.
SWITCH_AS_X = "switch_as_x"
# Strictest first, which decides a source wrapped as more than one of them (`_gates`): a lock is
# hidden as well as refused, and so is a siren (both are in `OFF_LIMITS`, #605); a valve is moved only
# by a `valve.*` service.
_GATED_BY_STRICTNESS: tuple[str, ...] = ("lock", "siren", "valve")
GATED_WRAPPER_DOMAINS: frozenset[str] = frozenset(_GATED_BY_STRICTNESS)

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


def _device_name(ent: Any, dev_reg: dr.DeviceRegistry | None) -> str | None:
    """The name of the physical device an entity belongs to, as the owner sees it in HA.

    `name_by_user` first: it is what the owner typed ("Switch by the door"), where `name` is usually
    the manufacturer's model string. This is metadata the owner already entered, read inside the
    existing entities.read capability, so it needs no capability of its own.
    """
    if ent is None or not ent.device_id or dev_reg is None:
        return None
    dev = dev_reg.async_get(ent.device_id)
    if dev is None:
        return None
    name = dev.name_by_user or dev.name
    if not isinstance(name, str) or not name:
        return None
    return name[:MAX_ATTR_STR]


def entity_dto(hass: HomeAssistant, state: Any, ent: Any, area_id: str | None, dev_reg: dr.DeviceRegistry | None) -> dict[str, Any]:
    """One entity in the shape `Entity` describes, so a pushed change and a polled one agree.

    Takes the device registry rather than a precomputed name so that both callers — the list
    handler and the push subscriber — get the device name without either being able to forget it.
    """
    return {
        "entity_id": state.entity_id,
        "name": state.name,
        "domain": state.domain,
        "area_id": area_id,
        "device_id": ent.device_id if ent else None,
        "device_name": _device_name(ent, dev_reg),
        # Home Assistant's own mark for what an entity *is* to its device: `config` for a setting
        # (a sensitivity, an indicator LED), `diagnostic` for a reading about the device itself (a
        # battery, a signal strength), None for the thing the device exists to do. The app folds the
        # first two into their device instead of drawing each as a tile of its own (AgDR-0033).
        "entity_category": _category(ent),
        # The Hearth labels on this entity and its device, as found. The hub decides what they mean
        # (AgDR-0034), so a rule change never needs a release of this integration.
        "hearth_labels": hearth_labels(hass, ent, dev_reg),
        "state": state.state,
        "attributes": filter_attributes(dict(state.attributes)),
    }


def _category(ent: Any) -> str | None:
    """`config`, `diagnostic` or None, whatever shape the registry holds it in."""
    cat = getattr(ent, "entity_category", None) if ent is not None else None
    if cat is None:
        return None
    value = getattr(cat, "value", cat)
    return value if value in ("config", "diagnostic") else None


class GateMap:
    """Every entity's `switch_as_x` gate (#643), worked out on demand and kept for one call.

    An entity's gate is the strictest, in `_GATED_BY_STRICTNESS` order, of every gated `switch_as_x`
    wrapper it reaches by following two relations as far as they go:

    - **A `switch_as_x` source and every wrapper on it are one thing** (ruling 5). A `cover` beside a
      `lock` on one relay drives the same relay, so it is the lock as far as Hearth is concerned; so
      is a `valve` beside one, which is why #527's "a valve may always close" does not reach it.
    - **A group is its members** (ruling 6). A `switch` group whose member is a lock's relay forwards
      `switch.turn_on` to that relay, and the model can make one with `helpers.create`. A group
      publishes its members as the `entity_id` state attribute, whatever its domain.

    Followed together, so a `cover` wrapper whose source is a group over the relay is the lock too
    (ruling 8), and a group of groups is covered. The walk only goes from an entity to what it
    drives, never back up: "all outside switches" over the relay is the lock, the porch switch beside
    it in that group is not. The one exception is a group that is *itself* wrapped as a lock, siren or
    valve: then each member drives part of what the wrapper stands for, so the gate flows down to
    them too (ruling 9). Only from a gated source, never from a group that merely contains one. A scene asked about *itself* is not walked (ruling 7): it stays listed,
    as one that sets a real lock does, and `find_scene_run_violations` refuses to activate it. A
    scene reached as a group's member is walked, because `homeassistant.turn_on` on an old-style
    group activates it.

    Reads every `switch_as_x` config entry, disabled ones included (`async_entries`' default): the
    relay underneath still drives the lock while its wrapper is switched off. `options["entity_id"]`
    holds either the source's entity_id or its registry id, and is resolved the way the helper's own
    setup resolves it; an entry whose source cannot be resolved is skipped rather than raised, as
    Home Assistant cannot set such a wrapper up either. Membership is read off the state machine one
    entity at a time, so nothing scans every state, not even the subscriber's `state_changed` callback.
    Nothing outlives the map, and a caller builds one per call, so a wrapper added or a member
    changed a moment ago already counts.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        ent_reg = er.async_get(hass)
        # Each source and each wrapper on it, mapped to the same set: the source and all its wrappers.
        self._same: dict[str, set[str]] = {}
        # Each source, mapped to the strictest gated target of any wrapper on it.
        self._own: dict[str, str] = {}
        for entry in hass.config_entries.async_entries(SWITCH_AS_X):
            if (source := _source_of(ent_reg, entry.options.get("entity_id"))) is None:
                continue
            same = self._same.setdefault(source, {source})
            if (target := entry.options.get("target_domain")) in GATED_WRAPPER_DOMAINS:
                self._own[source] = _stricter(self._own.get(source), target)
            for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
                same.add(ent.entity_id)
                self._same[ent.entity_id] = same
        for source, gate in list(self._own.items()):
            seen = {source}
            todo = [source]
            while todo:
                for member in self._members(todo.pop()):
                    if member not in seen:
                        seen.add(member)
                        todo.append(member)
                        self._own[member] = _stricter(self._own.get(member), gate)
        self._known: dict[str, str | None] = {}

    @property
    def empty(self) -> bool:
        """Whether the home has no gated wrapper at all, so no entity has a gate (ruling 2's condition)."""
        return not self._own

    def get(self, entity_id: str) -> str | None:
        """The entity's gate, or None."""
        if not self._own:
            # The common home: no gated wrapper, so nothing to walk and nothing worth caching. The
            # state subscriber asks this once per `state_changed`.
            return None
        key = entity_id.lower()
        if key not in self._known:
            self._known[key] = self._walk(key)
        return self._known[key]

    def __contains__(self, entity_id: str) -> bool:
        return self.get(entity_id) is not None

    def drives(self, entity_id: str) -> bool:
        """Whether a gate *rests on* this entity: a gated wrapper's source, or something a gated
        group above it hands its gate down to (ruling 9).

        Distinct from `get`, which also answers for anything that merely reaches a gated entity. An
        unwrapped "All outside" group over a lock's relay has a gate through `get`, but removing it
        takes no gate away from anything, so it is not a seed; a lock-wrapped group is, because its
        members' gates are read off its live `entity_id` attribute and go with it.
        """
        return entity_id.lower() in self._own

    def _walk(self, start: str) -> str | None:
        if not self._own:
            return None
        gate: str | None = None
        seen = {start}
        todo = [start]
        while todo:
            entity_id = todo.pop()
            gate = _stricter(gate, self._own.get(entity_id))
            reach = set(self._same.get(entity_id, ()))
            if not (entity_id == start and entity_id.startswith("scene.")):
                reach.update(self._members(entity_id))
            todo += [e for e in reach if e not in seen]
            seen |= reach
        return gate

    def _members(self, entity_id: str) -> list[str]:
        state = self._hass.states.get(entity_id)
        members = state.attributes.get("entity_id") if state is not None else None
        members = [members] if isinstance(members, str) else members
        return [m.lower() for m in members if isinstance(m, str)] if isinstance(members, (list, tuple)) else []


def _stricter(a: str | None, b: str | None) -> str | None:
    """The stricter of two gates, either of which may be None."""
    if a is None or b is None:
        return a or b
    return min(a, b, key=_GATED_BY_STRICTNESS.index)


def _gates(hass: HomeAssistant) -> GateMap:
    """A fresh `GateMap`. A caller asking about many entities builds one and passes it along."""
    return GateMap(hass)


def _source_of(ent_reg: er.EntityRegistry, value: Any) -> str | None:
    """A `switch_as_x` source as an entity_id, from either form its `entity_id` option takes, or None."""
    if not isinstance(value, str):
        return None
    try:
        return er.async_validate_entity_id(ent_reg, value.lower())
    except vol.Invalid:
        return None


def gated_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Whether this config entry is a `switch_as_x` wrapper on a gated source, of any target (#643)."""
    if entry.domain != SWITCH_AS_X:
        return False
    source = _source_of(er.async_get(hass), entry.options.get("entity_id"))
    return source is not None and source in _gates(hass)


def entry_carries_gate(hass: HomeAssistant, entry: Any, gates: GateMap | None = None) -> bool:
    """Whether a gate rests on any entity of this config entry (`GateMap.drives`), whatever its domain.

    A `group` helper that a `switch_as_x` helper wraps as a lock hands the lock's gate down to its
    members (ruling 9), read off the group's live `entity_id` attribute; removing the group's own
    entry would take that gate off every member, and `switch.turn_on` on one would reach the lock.
    """
    gates = _gates(hass) if gates is None else gates
    if gates.empty:
        return False
    return any(gates.drives(ent.entity_id) for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))


def wrapped_domain(hass: HomeAssistant, entity_id: str) -> str | None:
    """The gate `_gates` gives this entity, or None: the source switch or any wrapper on it (#643)."""
    return _gates(hass).get(entity_id.lower())


def effective_domain(hass: HomeAssistant, entity_id: str, gates: GateMap | None = None) -> str:
    """The domain Hearth's rules apply to: a gated wrapper's, else the id's own.

    A caller asking about many entities passes one `_gates(hass)` rather than have one built per id.
    """
    gate = (_gates(hass) if gates is None else gates).get(entity_id)
    return gate or entity_id.split(".", 1)[0]


def off_limits(hass: HomeAssistant, entity_id: str, gates: GateMap | None = None) -> bool:
    """Whether `OFF_LIMITS` covers this entity, on its effective domain (#643)."""
    return effective_domain(hass, entity_id, gates) in OFF_LIMITS


def visible(hass: HomeAssistant, entity_id: str, ent: Any, gates: GateMap | None = None) -> bool:
    """Whether the model may see this entity: the one rule the readers named below apply (#259).

    Not every list of the home's entities applies it. `scripts.list` and `scenes.list` list their own
    domain whether or not the owner exposed an entry, so that it can be found and edited, and say so
    with `can_run`.

    Three clauses: the domain is not in `OFF_LIMITS`, the registry has not hidden or disabled it,
    and the owner exposed it to Assist. The domain is the effective one, so a switch a `switch_as_x`
    helper presents as a lock, and every other wrapper on that switch, is hidden like the lock,
    exposed and un-hidden or not (#643). `entities.list` (poll), the state subscriber (push), the label
    targets in `organise.py`, `states.get` and `states.history` all call this, so push and poll cannot
    disagree: there is one copy of the rule. `devices.call` asks it too, so the model cannot operate
    what it cannot read (#642), and so does a `switch_as_x` flow about its source, so the model cannot
    wrap what it cannot read either (#643). `tests/test_visibility.py` goes red if one of these stops calling it;
    a new reader needs its own row there.

    `ent=None` skips the middle clause, so a caller must look the entry up and pass it. A caller
    asking about many entities passes one `gates` for all of them.

    The literal domain is checked as well as the effective one, so no gate, whatever the order of
    `_GATED_BY_STRICTNESS`, can surface a `lock.*` wrapper by giving it a weaker one.
    """
    if entity_id.split(".", 1)[0] in OFF_LIMITS or effective_domain(hass, entity_id, gates) in OFF_LIMITS:
        return False
    if ent is not None and (ent.hidden_by or ent.disabled_by):
        return False
    return _exposed(hass, entity_id)


async def areas_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Areas, each with its floor's storey number and name when it is on one.

    The floor fields ride on the area rather than in a sibling `floors` list so the result stays an
    array: a hub that predates them ignores two extra keys, where an object in place of the array
    would break it. A floor slug ("first_floor") says nothing about order; `level` does, and it is
    something the owner already set in Home Assistant.
    """
    reg = ar.async_get(hass)
    floors = fr.async_get(hass)
    return [area_dto(a, floors) for a in sorted(reg.async_list_areas(), key=lambda a: a.name.lower())]


def area_dto(area: Any, floors: fr.FloorRegistry) -> dict[str, Any]:
    """One area in the shape `Area` describes, shared by listing and the structure writes."""
    floor = floors.async_get_floor(area.floor_id) if area.floor_id else None
    return {
        "area_id": area.id,
        "name": area.name,
        "floor_id": area.floor_id,
        "floor_name": floor.name if floor else None,
        "floor_level": floor.level if floor and isinstance(floor.level, int) else None,
    }


def _entity_area(ent: er.RegistryEntry | None, dev_reg: dr.DeviceRegistry) -> str | None:
    if ent is None:
        return None
    if ent.area_id:
        return ent.area_id
    if ent.device_id and (dev := dev_reg.async_get(ent.device_id)):
        if dev.area_id is None and isinstance(dev, dr.ChildDeviceEntry):
            # A child device with no area of its own inherits its parent's, as Home Assistant's own
            # `async_get_effective_area_id` does. Reading `dev.area_id` alone reported such an entity
            # in no room while Home Assistant had it in one — a gap moving devices would expose.
            parent = dev_reg.async_get(dev.parent_device_id, include_child_devices=False)
            return parent.area_id if parent else None
        return dev.area_id
    return None


async def entities_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    domain = params.get("domain")
    area_id = params.get("area_id")
    query = (params.get("query") or "").lower()
    limit = params.get("limit", 200)
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise RpcError("invalid_params", "limit must be 1..500")
    after = params.get("after")
    if after is not None and (not isinstance(after, str) or len(after) > 255):
        raise RpcError("invalid_params", "after must be an entity_id")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    gates = _gates(hass)
    out: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(), key=lambda s: s.entity_id):
        # Keyset paging. Iteration is already sorted by entity_id, so "resume past this id" needs no
        # offset, no snapshot and no cursor state: a home that gains or loses an entity between pages
        # cannot make the caller skip or repeat one of the others. A page shorter than `limit` is the
        # last page, so the result shape stays as it was.
        if after is not None and state.entity_id <= after:
            continue
        if domain and state.domain != domain:
            continue
        ent = ent_reg.async_get(state.entity_id)
        if not visible(hass, state.entity_id, ent, gates):
            continue
        entity_area = _entity_area(ent, dev_reg)
        if area_id and entity_area != area_id:
            continue
        name = state.name
        if query and query not in state.entity_id.lower() and query not in (name or "").lower():
            continue
        out.append(entity_dto(hass, state, ent, entity_area, dev_reg))
        if len(out) >= limit:
            break
    return out


async def states_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    entity_id = require_str(params, "entity_id")
    if entity_id.split(".", 1)[0] in OFF_LIMITS:
        # Only the message differs here; `visible()` below refuses these domains too.
        raise RpcError("not_found", "entity not available")
    state = hass.states.get(entity_id)
    # The whole rule, registry clause included: #642 ruled that a hidden entity is refused even when
    # the owner exposed it to Assist. Home Assistant drops a disabled entity's state, so in practice
    # only `hidden_by` reaches the clause.
    if state is None or not visible(hass, entity_id, er.async_get(hass).async_get(entity_id)):
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
        if dom in OFF_LIMITS:
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
