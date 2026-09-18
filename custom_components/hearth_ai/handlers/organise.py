"""Labels and categories: the organising layer above rooms (AgDR-0040).

Home Assistant has two ways to group things that are not areas, and a household wants both without
opening it:

* **Labels** are free tags that go on any entity — "holiday", "upstairs lights", "mum's room" — and
  are what a model should reach for when asked to act on a set of things that is not a room.
* **Categories** are the folders the automation, script and scene lists are sorted into. They are
  scoped: a category of automations is not a category of scripts.

Two rules make this safe to hand a model.

**Hearth's own labels are not an AI control.** `Hearth: tile`, `read only`, `setting`, `diagnostic`
and `hide` decide how the app presents a device (AgDR-0034), and the hub deliberately strips
`hearth_labels` from every non-app surface. A model that could rename, delete or apply one could
change what the household sees, so `role_of` is consulted on every write here and any hit is
refused. It resolves by stored id **and** by name, so renaming one out of the way does not work
either.

**Only things the model may already see can be labelled.** That is Assist exposure for a device, and
membership of `automation`/`script`/`scene` for the things categories are for — those are never
exposed by default and the model reaches them through its own lists. Anything in an off-limits
domain is refused outright, so labelling is not a way to confirm a lock exists.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import category_registry as cr, entity_registry as er, label_registry as lr
from homeassistant.core import HomeAssistant

from ..labels import ROLES, role_of
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import OFF_LIMITS, visible

# The three lists Home Assistant sorts into folders. There is no fourth.
SCOPES: frozenset[str] = frozenset({"automation", "script", "scene"})
# Domains a model already reaches through its own lists, which Assist does not expose by default.
OWN_LIST_DOMAINS: frozenset[str] = frozenset({"automation", "script", "scene"})
MAX_TARGETS = 100


def _check_not_hearths(hass: HomeAssistant, label_id: str) -> None:
    """Refuse to touch one of Hearth's own presentation labels (AgDR-0034)."""
    role = role_of(hass, label_id)
    if role is not None:
        raise RpcError(
            "method_not_allowed",
            f"'{ROLES[role]}' is one of Hearth's own labels and decides how the app shows a device; "
            "it is changed in Home Assistant by a person, never from here",
        )


def _label(hass: HomeAssistant, label_id: str) -> Any:
    label = lr.async_get(hass).async_get_label(label_id)
    if label is None:
        raise RpcError("not_found", f"no label {label_id}")
    return label


def _label_dto(hass: HomeAssistant, label: Any) -> dict[str, Any]:
    return {
        "label_id": label.label_id,
        "name": label.name,
        "icon": label.icon,
        "description": label.description,
        # True for Hearth's own five: listed so a model can see them and leave them alone.
        "hearth": role_of(hass, label.label_id) is not None,
    }


def _targets(hass: HomeAssistant, params: dict[str, Any]) -> list[str]:
    """The entities a write names, each one checked against what the model may already see."""
    raw = params.get("entity_ids")
    if not isinstance(raw, list) or not raw:
        raise RpcError("invalid_params", "entity_ids must be a non-empty list")
    if len(raw) > MAX_TARGETS:
        raise RpcError("invalid_params", f"at most {MAX_TARGETS} entities at a time")
    ent_reg = er.async_get(hass)
    out: list[str] = []
    for entity_id in raw:
        if not isinstance(entity_id, str):
            raise RpcError("invalid_params", "entity_ids must be strings")
        # `OFF_LIMITS` is the same union the policy's reference check uses, plus `image`: labelling
        # is not a way to confirm that a lock exists.
        domain = entity_id.split(".", 1)[0]
        if domain in OFF_LIMITS:
            raise RpcError("not_found", f"unknown entity {entity_id}")
        ent = ent_reg.async_get(entity_id)
        if ent is None:
            raise RpcError("not_found", f"{entity_id} is not in the entity registry, so it cannot be labelled")
        # Exposure is the boundary everywhere else, and it is the boundary here — except for the
        # three lists the model reaches through its own tools, which Assist never exposes.
        if domain not in OWN_LIST_DOMAINS and not visible(hass, entity_id, ent):
            raise RpcError("not_found", f"unknown entity {entity_id}")
        out.append(entity_id)
    return out


def _name(params: dict[str, Any], key: str = "name") -> str:
    name = require_str(params, key)
    if not name.strip() or len(name) > 255:
        raise RpcError("invalid_params", f"{key} must be 1..255 characters")
    return name


def _icon(params: dict[str, Any]) -> str | None:
    icon = params.get("icon")
    if icon is None:
        return None
    if not isinstance(icon, str) or len(icon) > 64:
        raise RpcError("invalid_params", "icon must be a string like mdi:home")
    return icon


# --------------------------------------------------------------------------- labels


async def labels_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    reg = lr.async_get(hass)
    return sorted((_label_dto(hass, label) for label in reg.async_list_labels()), key=lambda r: r["name"].lower())


async def labels_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    name = _name(params)
    reg = lr.async_get(hass)
    try:
        label = reg.async_create(name, icon=_icon(params), description=params.get("description") or None)
    except ValueError as err:
        raise RpcError("validation_failed", f"a label called {name} already exists") from err
    return _label_dto(hass, label)


async def labels_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    label_id = require_str(params, "label_id")
    _label(hass, label_id)
    _check_not_hearths(hass, label_id)
    changes: dict[str, Any] = {}
    if "name" in params:
        changes["name"] = _name(params)
    if "icon" in params:
        changes["icon"] = _icon(params)
    if "description" in params:
        changes["description"] = params.get("description") or None
    if not changes:
        raise RpcError("invalid_params", "give a name, an icon, or a description to change")
    try:
        label = lr.async_get(hass).async_update(label_id, **changes)
    except ValueError as err:
        raise RpcError("validation_failed", str(err)) from err
    return _label_dto(hass, label)


async def labels_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    label_id = require_str(params, "label_id")
    _label(hass, label_id)
    _check_not_hearths(hass, label_id)
    # Home Assistant takes the label off everything that had it; saying how many is the honest
    # answer to "what did that do", the same as deleting a room (AgDR-0031).
    ent_reg = er.async_get(hass)
    used_by = [e.entity_id for e in ent_reg.entities.values() if label_id in (e.labels or ())]
    lr.async_get(hass).async_delete(label_id)
    return {"removed_from": [e for e in used_by if e.split(".", 1)[0] not in OFF_LIMITS]}


async def labels_assign(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Put a label on entities, or take it off them."""
    label_id = require_str(params, "label_id")
    _label(hass, label_id)
    _check_not_hearths(hass, label_id)
    mode = params.get("mode", "add")
    if mode not in ("add", "remove"):
        raise RpcError("invalid_params", "mode must be add or remove")
    entity_ids = _targets(hass, params)

    ent_reg = er.async_get(hass)
    changed: list[str] = []
    for entity_id in entity_ids:
        ent = ent_reg.async_get(entity_id)
        labels = set(ent.labels or ())
        new = labels | {label_id} if mode == "add" else labels - {label_id}
        if new != labels:
            ent_reg.async_update_entity(entity_id, labels=new)
            changed.append(entity_id)
    return {"changed": changed}


# --------------------------------------------------------------------------- categories


def _scope(params: dict[str, Any]) -> str:
    scope = require_str(params, "scope")
    if scope not in SCOPES:
        raise RpcError("invalid_params", f"scope must be one of {', '.join(sorted(SCOPES))}")
    return scope


async def categories_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    scope = _scope(params)
    reg = cr.async_get(hass)
    return sorted(
        (
            {"category_id": c.category_id, "scope": scope, "name": c.name, "icon": c.icon}
            for c in reg.async_list_categories(scope=scope)
        ),
        key=lambda r: r["name"].lower(),
    )


async def categories_create(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    scope = _scope(params)
    name = _name(params)
    try:
        category = cr.async_get(hass).async_create(name=name, scope=scope, icon=_icon(params))
    except ValueError as err:
        raise RpcError("validation_failed", f"a {scope} category called {name} already exists") from err
    return {"category_id": category.category_id, "scope": scope, "name": category.name, "icon": category.icon}


async def categories_update(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    scope = _scope(params)
    category_id = require_str(params, "category_id")
    reg = cr.async_get(hass)
    if reg.async_get_category(scope=scope, category_id=category_id) is None:
        raise RpcError("not_found", f"no {scope} category {category_id}")
    changes: dict[str, Any] = {}
    if "name" in params:
        changes["name"] = _name(params)
    if "icon" in params:
        changes["icon"] = _icon(params)
    if not changes:
        raise RpcError("invalid_params", "give a name or an icon to change")
    try:
        category = reg.async_update(scope=scope, category_id=category_id, **changes)
    except ValueError as err:
        raise RpcError("validation_failed", str(err)) from err
    return {"category_id": category.category_id, "scope": scope, "name": category.name, "icon": category.icon}


async def categories_delete(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    scope = _scope(params)
    category_id = require_str(params, "category_id")
    reg = cr.async_get(hass)
    if reg.async_get_category(scope=scope, category_id=category_id) is None:
        raise RpcError("not_found", f"no {scope} category {category_id}")
    ent_reg = er.async_get(hass)
    emptied = [e.entity_id for e in ent_reg.entities.values() if (e.categories or {}).get(scope) == category_id]
    reg.async_delete(scope=scope, category_id=category_id)
    return {"removed_from": emptied}


async def categories_assign(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Put automations, scripts or scenes in a category, or take them out of one.

    `category_id: null` means "out of whatever folder it was in", which is how a model undoes a
    tidy-up without having to know where something started.
    """
    scope = _scope(params)
    category_id = params.get("category_id")
    if category_id is not None:
        if not isinstance(category_id, str):
            raise RpcError("invalid_params", "category_id must be a string or null")
        if cr.async_get(hass).async_get_category(scope=scope, category_id=category_id) is None:
            raise RpcError("not_found", f"no {scope} category {category_id}")
    entity_ids = _targets(hass, params)

    ent_reg = er.async_get(hass)
    changed: list[str] = []
    for entity_id in entity_ids:
        if entity_id.split(".", 1)[0] != scope:
            raise RpcError("invalid_params", f"{entity_id} is not a {scope}, so it cannot go in a {scope} category")
        ent = ent_reg.async_get(entity_id)
        categories = dict(ent.categories or {})
        if category_id is None:
            categories.pop(scope, None)
        else:
            categories[scope] = category_id
        if categories != (ent.categories or {}):
            ent_reg.async_update_entity(entity_id, categories=categories)
            changed.append(entity_id)
    return {"changed": changed}


def register(d: Dispatcher) -> None:
    d.register("labels.list", labels_list)
    d.register("labels.create", labels_create)
    d.register("labels.update", labels_update)
    d.register("labels.delete", labels_delete)
    d.register("labels.assign", labels_assign)
    d.register("categories.list", categories_list)
    d.register("categories.create", categories_create)
    d.register("categories.update", categories_update)
    d.register("categories.delete", categories_delete)
    d.register("categories.assign", categories_assign)
