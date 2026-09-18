"""Helpers: the things a household actually makes, without ever opening Home Assistant (AgDR-0039).

Two machines wear one name in Home Assistant's UI, and a household should not have to know which is
which:

* **Config-flow helpers** — `template`, `trend`, `threshold`, `derivative`, `group`, `utility_meter`
  and the rest. These are config entries, created by driving the same flow machinery
  `handlers/integrations.py` drives, and named here by their `entry_id`.
* **Storage-collection helpers** — `input_boolean`, `input_button`, `input_number`, `input_text`,
  `input_select`, `input_datetime`, `counter`, `timer`, `schedule`. These have no config flow at
  all. Each keeps a `DictStorageCollection` in a local variable inside its `async_setup` and
  publishes it only as admin-only websocket commands. See `_collection`.

Both answer with the same `fields` shape, serialised by the same code, so a model learns one
protocol for every helper type rather than one per machine.

Every write goes through `check_flow_input` first: a helper's form field is an authoring surface
(AgDR-0038), and a `template` switch is a stored action sequence like any script.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_config_flows, async_get_integrations

from ..rpc import Dispatcher, RpcError
from .common import require_str
from .flows import DENIED_DOMAINS, check_flow_input, forget_fields, serialize_field_map, shape
from .registry import OFF_LIMITS, _exposed

FLOW_TIMEOUT_S = 30

# The nine helper families Home Assistant keeps in a storage collection rather than a config entry.
# All nine are in `bootstrap.DEFAULT_INTEGRATIONS`, so they are set up on every Home Assistant that
# is not in recovery mode — there is no "is it loaded" case to handle beyond a clear refusal.
INPUT_DOMAINS: tuple[str, ...] = (
    "input_boolean",
    "input_button",
    "input_number",
    "input_text",
    "input_select",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
)

# Keys Home Assistant's websocket envelope adds; not part of the helper itself.
_ENVELOPE_FIELDS = frozenset({"id", "type"})
# How far down the decorator stack to look for the collection. `require_admin(async_response(...))`
# is two; the margin is for a future release adding another wrapper.
_MAX_UNWRAP = 6
# How many forms a helper may ask for in one create. `trend` and `utility_meter` use two.
_MAX_STEPS = 5


def _ws(hass: HomeAssistant, domain: str) -> Any:
    """The live `DictStorageCollectionWebsocket` behind `<domain>/create`.

    `input_boolean` and its eight siblings keep both their collection and their create/update schemas
    inside this one object, published only as admin-only websocket commands — the collection itself
    is a local variable in `async_setup` and never reaches `hass.data`. The only handle Home
    Assistant leaves is the registered command, whose handler is
    `require_admin(async_response(bound method))`; both decorators use `functools.wraps`, so
    `__wrapped__` leads back to the bound method and, through `__self__`, to this object. No private
    attribute is touched.

    We drive it directly rather than fabricating an `ActiveConnection` with an admin user: every
    other write in this integration (`ar.async_create`, `er.async_update_entity`) uses the
    integration's own authority, and impersonating a person to satisfy a decorator would be a worse
    answer, not a safer one. Authorisation happened upstream — the capability, the tool's
    `requiresAdmin`, and the caller's role.

    Refuses loudly if Home Assistant ever changes that stack, and the nine-domain test means such a
    release lands in CI rather than in someone's house.
    """
    registered = (hass.data.get("websocket_api") or {}).get(f"{domain}/create")
    if not registered:
        raise RpcError("not_found", f"{domain} is not set up in this Home Assistant")
    fn: Any = registered[0]
    for _ in range(_MAX_UNWRAP):
        owner = getattr(fn, "__self__", None)
        if owner is not None and hasattr(getattr(owner, "storage_collection", None), "async_create_item"):
            return owner
        fn = getattr(fn, "__wrapped__", None)
        if fn is None:
            break
    raise RpcError("ha_error", f"this Home Assistant version does not expose the {domain} store in a way Hearth can drive")


def _collection(hass: HomeAssistant, domain: str) -> Any:
    return _ws(hass, domain).storage_collection


def _item(hass: HomeAssistant, domain: str, item_id: str) -> dict[str, Any]:
    """One collection helper as stored, or `not_found`."""
    for item in _collection(hass, domain).async_items():
        if item["id"] == item_id:
            return dict(item)
    raise RpcError("not_found", f"no {domain} helper {item_id}")


def _merged(hass: HomeAssistant, domain: str, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """The item's current settings with `changes` laid over them.

    Home Assistant's collections **replace** on update — `_update_data` is
    `{id} | SCHEMA(update_data)` — so sending only the changed keys does not edit a helper, it
    rebuilds it from those keys alone and fails on whatever was required and missing. Its own UI
    never notices because it posts the whole form back; a model has no form to post. Merging here is
    also what keeps `rename` from wiping a number's range, and it steers around an upstream bug in
    `timer`, whose `_update_data` leaves a `timedelta` in storage when `duration` is absent.
    """
    current = _item(hass, domain, item_id)
    current.pop("id", None)
    return {**current, **changes}


def _input_fields(hass: HomeAssistant, domain: str) -> list[dict[str, Any]]:
    """The create form for a collection helper, from the schema Home Assistant registered with it.

    Serialised from the websocket wrapper's own `create_schema` rather than from the registered
    command schema, which wraps it in the envelope (`id`, `type`) a caller never sends. Same
    serialiser as the config-flow path, so `helpers.describe("input_number")` and
    `helpers.describe("threshold")` answer in one shape and Hearth hardcodes no field list of its
    own that could drift from Home Assistant's.
    """
    create_schema = getattr(_ws(hass, domain), "create_schema", None)
    if not create_schema:
        raise RpcError("ha_error", f"this Home Assistant version does not describe the {domain} form")
    fields = serialize_field_map(create_schema, f"{domain} create form")
    return [f for f in fields if f["name"] not in _ENVELOPE_FIELDS]


async def _flow_domains(hass: HomeAssistant) -> set[str]:
    """Helper-type config flows, and only those.

    `type_filter="helper"` is what tells a `template` from a Hue bridge. It is also the whole guard
    on `helpers.delete`: an entry whose domain is not in here is an integration, and Hearth does not
    remove integrations.
    """
    return {d for d in await async_get_config_flows(hass, "helper") if d not in DENIED_DOMAINS}


def _entry_dto(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    ent_reg = er.async_get(hass)
    ids = [e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)]
    # A `group` of locks, or a template lock, is a real thing an owner may have made by hand. It
    # stays listed so it can still be renamed or removed, but its entity ids never cross the wire:
    # naming one is the disclosure OFF_LIMITS exists to prevent.
    visible = [i for i in ids if i.split(".", 1)[0] not in OFF_LIMITS]
    return {
        "id": entry.entry_id,
        "domain": entry.domain,
        "kind": "flow",
        "name": entry.title,
        "entities": visible,
        "restricted": len(visible) != len(ids),
        "shared": _shared(hass, visible),
    }


def _shared(hass: HomeAssistant, entity_ids: list[str]) -> bool:
    """Whether Hearth can *read* what this helper produces, as opposed to having made it.

    Home Assistant shares a new entity with Assist only if its domain or device class is one it
    shares by default, and most helpers are neither: a `trend` binary_sensor has no device class,
    an `input_boolean` is not a shared domain. So the ordinary outcome of making a helper is an
    entity Hearth cannot see the state of — which, without being told, reads as a bug or a delay.

    It is not one, and it is not fixed by sharing it automatically: exposure is the household's
    setting, and a model that could widen it could widen its own reach mid-turn (AgDR-0024). Saying
    so is the honest answer, and it is what this flag is for. An automation may reference the entity
    either way; exposure filters what Hearth *lists*, not what Home Assistant can trigger on.
    """
    return bool(entity_ids) and all(_exposed(hass, entity_id) for entity_id in entity_ids)


def _item_dto(hass: HomeAssistant, domain: str, item: dict[str, Any]) -> dict[str, Any]:
    entity_id = f"{domain}.{item['id']}"
    return {
        "id": entity_id,
        "domain": domain,
        "kind": "input",
        "name": item.get("name") or item["id"],
        "entities": [entity_id],
        "restricted": False,
        "shared": _shared(hass, [entity_id]),
    }


def _split_input_id(helper_id: str) -> tuple[str, str] | None:
    """`input_boolean.guests_coming` -> (domain, item id), or None if this is not one of ours."""
    domain, _, item_id = helper_id.partition(".")
    return (domain, item_id) if item_id and domain in INPUT_DOMAINS else None


# --------------------------------------------------------------------------- reads


async def helpers_types(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every kind of helper this Home Assistant can make. No query and no cap: there are 27."""
    flow_domains = sorted(await _flow_domains(hass))
    configured: dict[str, int] = {}
    for entry in hass.config_entries.async_entries():
        configured[entry.domain] = configured.get(entry.domain, 0) + 1

    integrations = await async_get_integrations(hass, flow_domains)
    rows = [
        {
            "domain": d,
            "name": getattr(integrations.get(d), "name", d) if not isinstance(integrations.get(d), Exception) else d,
            "kind": "flow",
            "configured": configured.get(d, 0),
        }
        for d in flow_domains
    ]
    for domain in INPUT_DOMAINS:
        store = (hass.data.get("websocket_api") or {}).get(f"{domain}/create")
        if store is None:
            continue
        rows.append(
            {
                "domain": domain,
                "name": domain.replace("input_", "").replace("_", " ").title(),
                "kind": "input",
                "configured": len(hass.states.async_entity_ids(domain)),
            }
        )
    return sorted(rows, key=lambda r: r["domain"])


async def helpers_describe(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """The form for one helper type, without leaving anything half-open.

    A config-flow helper is described by starting its flow, advancing past the menu when it has one,
    serialising the form and then **aborting**. Describing is a read, and a read must not leave a
    half-finished setup for a person to find under Settings > Devices & services.
    """
    domain = require_str(params, "domain")
    variant = params.get("variant")
    if variant is not None and not isinstance(variant, str):
        raise RpcError("invalid_params", "variant must be a string")

    if domain in INPUT_DOMAINS:
        return {"domain": domain, "kind": "input", "variants": [], "fields": _input_fields(hass, domain)}

    if domain not in await _flow_domains(hass):
        raise RpcError("not_found", f"{domain} is not a helper; use search_available_integrations for devices and services")

    result = await _init(hass, domain)
    flow_id = str(result["flow_id"])
    try:
        if result["type"] is FlowResultType.MENU:
            options = list(result.get("menu_options") or [])
            if variant is None:
                return {"domain": domain, "kind": "flow", "variants": options, "fields": []}
            if variant not in options:
                raise RpcError("invalid_params", f"{domain} has no variant {variant}: one of {', '.join(options)}")
            result = await _step(hass, flow_id, {"next_step_id": variant})
        if result["type"] is not FlowResultType.FORM:
            raise RpcError("ha_error", f"{domain} asked for something Hearth cannot describe ({result['type'].value})")
        shaped = shape(hass, domain, result)
        return {"domain": domain, "kind": "flow", "variants": [], "fields": shaped["fields"], "secret_fields": shaped["secret_fields"]}
    finally:
        _abandon(hass, flow_id)


async def helpers_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every helper this home has, of both kinds."""
    flow_domains = await _flow_domains(hass)
    rows = [_entry_dto(hass, e) for e in hass.config_entries.async_entries() if e.domain in flow_domains]
    for domain in INPUT_DOMAINS:
        with suppress(RpcError):
            store = _collection(hass, domain)
            rows += [_item_dto(hass, domain, item) for item in store.async_items()]
    return sorted(rows, key=lambda r: (r["domain"], r["name"] or ""))


# --------------------------------------------------------------------------- flow plumbing


async def _init(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            return await hass.config_entries.flow.async_init(domain, context={"source": "user"})
    except TimeoutError as err:
        raise RpcError("timeout", f"{domain} did not respond within {FLOW_TIMEOUT_S}s") from err


async def _step(hass: HomeAssistant, flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
    try:
        async with asyncio.timeout(FLOW_TIMEOUT_S):
            return await hass.config_entries.flow.async_configure(flow_id, data)
    except TimeoutError as err:
        raise RpcError("timeout", f"the helper form did not finish within {FLOW_TIMEOUT_S}s") from err
    except RpcError:
        raise
    except Exception as err:  # noqa: BLE001 - flows raise their own errors for bad input
        raise RpcError("validation_failed", f"{type(err).__name__}: {err}") from err


def _abandon(hass: HomeAssistant, flow_id: str) -> None:
    """Never leave a half-open flow for a person to find under Settings > Devices & services."""
    with suppress(Exception):
        hass.config_entries.flow.async_abort(flow_id)
    forget_fields(hass, flow_id)


# --------------------------------------------------------------------------- writes


async def helpers_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Make one helper, in one call, leaving no half-finished flow behind."""
    domain = require_str(params, "domain")
    config = params.get("config")
    if not isinstance(config, dict):
        raise RpcError("invalid_params", "config must be an object")
    check_flow_input(config)

    if domain in INPUT_DOMAINS:
        store = _collection(hass, domain)
        try:
            item = await store.async_create_item(dict(config))
        except Exception as err:  # noqa: BLE001 - vol.Invalid, ValueError: all mean "bad form"
            raise RpcError("validation_failed", f"{type(err).__name__}: {err}") from err
        return _item_dto(hass, domain, item)

    if domain not in await _flow_domains(hass):
        raise RpcError("not_found", f"{domain} is not a helper; use search_available_integrations for devices and services")

    result = await _init(hass, domain)
    flow_id = str(result["flow_id"])
    try:
        if result["type"] is FlowResultType.MENU:
            options = list(result.get("menu_options") or [])
            variant = params.get("variant")
            if variant not in options:
                raise RpcError("invalid_params", f"{domain} needs a variant: one of {', '.join(options)}")
            result = await _step(hass, flow_id, {"next_step_id": variant})
        if result["type"] is not FlowResultType.FORM:
            raise RpcError("ha_error", f"{domain} asked for something Hearth cannot answer ({result['type'].value})")

        # Some helpers ask twice: `trend` takes the entity on one form and its sensitivity settings
        # on the next. One `config` covers the lot — each step is given the keys it asked for, and a
        # step whose settings are all absent gets an empty submission, which is what Home Assistant's
        # own defaults are for. A model should not have to know how many forms a helper has.
        remaining = dict(config)
        for _ in range(_MAX_STEPS):
            shaped = shape(hass, domain, result)
            names = {f["name"] for f in shaped.get("fields") or []}
            step_input = {k: remaining.pop(k) for k in list(remaining) if k in names}
            step_id = result.get("step_id")
            result = await _step(hass, flow_id, step_input)
            if result["type"] is FlowResultType.CREATE_ENTRY:
                return _entry_dto(hass, result.get("result"))
            if result["type"] is not FlowResultType.FORM:
                raise RpcError("ha_error", f"{domain} asked for something Hearth cannot answer ({result['type'].value})")
            errors = {k: str(v) for k, v in (result.get("errors") or {}).items()}
            if errors or result.get("step_id") == step_id:
                # Rejected rather than advanced: hand the reason and the fields back rather than
                # leaving a half-finished setup for a person to find under Devices & services.
                detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "see fields"
                raise RpcError(
                    "validation_failed",
                    f"Home Assistant did not accept that: {detail}",
                    {"fields": shape(hass, domain, result).get("fields")},
                )
        raise RpcError("ha_error", f"{domain} asked more questions than Hearth can answer in one go")
    finally:
        # On success the flow is finished and aborting is a no-op; on any failure this is what keeps
        # a rejected form from becoming a half-open setup someone has to find and clean up.
        _abandon(hass, flow_id)


async def helpers_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Change a collection helper's settings — a timer's duration, a number's range.

    Config-entry helpers reconfigure through their own options flow, which is its own shape and its
    own decision; until then this says so plainly rather than pretending `delete` + `create` is the
    same thing. It is not: it changes the entity id and breaks every automation that named it.
    """
    helper_id = require_str(params, "id")
    config = params.get("config")
    if not isinstance(config, dict):
        raise RpcError("invalid_params", "config must be an object")
    check_flow_input(config)

    split = _split_input_id(helper_id)
    if split is None:
        raise RpcError(
            "method_not_allowed",
            "only toggles, numbers, timers and the like can be changed here; "
            "reconfiguring a template or threshold helper is not supported yet",
        )
    domain, item_id = split
    # A patch, not a replacement: what you leave out keeps its current value. See `_merged`.
    merged = _merged(hass, domain, item_id, dict(config))
    try:
        item = await _collection(hass, domain).async_update_item(item_id, merged)
    except Exception as err:  # noqa: BLE001 - a validation error from the collection's own schema
        raise RpcError("validation_failed", f"{type(err).__name__}: {err}") from err
    return _item_dto(hass, domain, item)


async def helpers_rename(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """The owner's name for a helper, for either kind. The entity id never changes."""
    helper_id = require_str(params, "id")
    name = require_str(params, "name")
    if not name.strip() or len(name) > 255:
        raise RpcError("invalid_params", "name must be 1..255 characters")

    split = _split_input_id(helper_id)
    if split is not None:
        domain, item_id = split
        merged = _merged(hass, domain, item_id, {"name": name})
        try:
            item = await _collection(hass, domain).async_update_item(item_id, merged)
        except Exception as err:  # noqa: BLE001 - ItemNotFound or a validation error
            raise RpcError("validation_failed", f"{type(err).__name__}: {err}") from err
        return _item_dto(hass, domain, item)

    entry = hass.config_entries.async_get_entry(helper_id)
    if entry is None or entry.domain not in await _flow_domains(hass):
        raise RpcError("not_found", "no helper with that id")
    hass.config_entries.async_update_entry(entry, title=name)
    return _entry_dto(hass, entry)


async def helpers_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Remove a helper, and say what went with it.

    Home Assistant's own rule, as in AgDR-0031: deleting never refuses and the counts say what
    happened. An automation that named these entities is now broken, and saying so is the tool
    description's job rather than a refusal's.
    """
    helper_id = require_str(params, "id")

    split = _split_input_id(helper_id)
    if split is not None:
        domain, item_id = split
        store = _collection(hass, domain)
        try:
            await store.async_delete_item(item_id)
        except Exception as err:  # noqa: BLE001 - ItemNotFound
            raise RpcError("not_found", f"no {domain} helper {item_id}") from err
        return {"removed_entities": [helper_id]}

    entry = hass.config_entries.async_get_entry(helper_id)
    if entry is None:
        raise RpcError("not_found", "no helper with that id")
    # The guard that keeps this off a Hue bridge: helper-type config flows only, ever. The
    # integrations surface has no delete at all, and this is not one by the back door.
    if entry.domain not in await _flow_domains(hass):
        raise RpcError("method_not_allowed", f"{entry.title} is an integration, not a helper; Hearth does not remove integrations")
    removed = _entry_dto(hass, entry)["entities"]
    if not await hass.config_entries.async_remove(entry.entry_id):
        raise RpcError("ha_error", "Home Assistant did not remove that helper")
    return {"removed_entities": removed}


def register(d: Dispatcher) -> None:
    d.register("helpers.types", helpers_types)
    d.register("helpers.describe", helpers_describe)
    d.register("helpers.list", helpers_list)
    d.register("helpers.create", helpers_create)
    d.register("helpers.update", helpers_update)
    d.register("helpers.rename", helpers_rename)
    d.register("helpers.delete", helpers_delete)
