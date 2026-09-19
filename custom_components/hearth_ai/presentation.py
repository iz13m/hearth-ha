"""The arrangement an admin authored in the Hearth panel (AgDR-0046).

Home Assistant holds this, not the hub. The app may arrange what this home handed over and can never
reach past it, and that is only true if Home Assistant is what holds the arrangement — so the panel
writes here, the hub reads it over `presentation.get`, and nothing else can write it at all.

What it may say is deliberately narrow. It can move a thing, give several things one tile, and take
an edit away from the app. It cannot widen anything: the hub clamps every placement against what the
labels, Home Assistant's own `entity_category` and inference already decided, and the validation
below refuses an off-limits domain outright rather than leaving the hub to drop it silently.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .handlers.registry import OFF_LIMITS

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.presentation"
STORAGE_VERSION = 1
# hass.data key for the loaded profile, so a read costs nothing on the state path.
DATA_PROFILE = f"{DOMAIN}_presentation"

PLACEMENTS = frozenset({"tile", "read_only", "setting", "diagnostic", "hide"})
SLOTS = frozenset({"main", "reading", "setting"})
EDITABLE = frozenset({"name", "icon", "room", "order", "hide"})
ROUTINE_DOMAINS = frozenset({"scene", "script"})

MAX_TILES = 200
MAX_MEMBERS = 32
MAX_ACTIONS = 8
MAX_ENTITIES = 2000

EMPTY: dict[str, Any] = {"version": STORAGE_VERSION, "entities": [], "tiles": []}


class ProfileError(Exception):
    """The panel sent an arrangement this home cannot hold. The message is shown to the admin."""


def _domain(entity_id: str) -> str:
    return entity_id.partition(".")[0]


def _known(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether this home has such an entity at all.

    A registry entry **or** a live state: a YAML template or MQTT entity without a `unique_id` has a
    state and no registry entry, and `entities.list` returns it — so requiring a registry entry would
    refuse to arrange things the app is already showing.
    """
    return er.async_get(hass).async_get(entity_id) is not None or hass.states.get(entity_id) is not None


def validate(hass: HomeAssistant, raw: Any) -> dict[str, Any]:
    """Check an arrangement and return it normalised, or raise `ProfileError`.

    Refusals are loud on purpose. A silent drop leaves an admin looking at a panel that says one
    thing and an app that does another, which is the failure this whole feature exists to end.
    """
    if not isinstance(raw, dict):
        raise ProfileError("the arrangement must be an object")

    entities_in = raw.get("entities") or []
    tiles_in = raw.get("tiles") or []
    if not isinstance(entities_in, list) or not isinstance(tiles_in, list):
        raise ProfileError("entities and tiles must be lists")
    if len(entities_in) > MAX_ENTITIES:
        raise ProfileError(f"at most {MAX_ENTITIES} entities can be arranged")
    if len(tiles_in) > MAX_TILES:
        raise ProfileError(f"at most {MAX_TILES} tiles can be arranged")

    entities: list[dict[str, Any]] = []
    for row in entities_in:
        if not isinstance(row, dict):
            raise ProfileError("each entity must be an object")
        entity_id = row.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise ProfileError("each entity needs an entity_id")
        _refuse_off_limits(entity_id)
        if not _known(hass, entity_id):
            raise ProfileError(f"{entity_id} is not in this Home Assistant")
        out: dict[str, Any] = {"entity_id": entity_id}
        placement = row.get("placement")
        if placement is not None:
            if placement not in PLACEMENTS:
                raise ProfileError(f"{placement} is not a placement")
            out["placement"] = placement
        editable = row.get("editable")
        if editable is not None:
            out["editable"] = _editable(editable)
        # A row saying nothing is not an error, it is a row to drop — the panel writes one the moment
        # a dialog is opened, and saving a file full of them is how a profile grows without meaning.
        if len(out) > 1:
            entities.append(out)

    tiles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    claimed: dict[str, str] = {}
    for row in tiles_in:
        if not isinstance(row, dict):
            raise ProfileError("each tile must be an object")
        tile_id = row.get("id")
        if not isinstance(tile_id, str) or not tile_id.strip():
            raise ProfileError("each tile needs an id")
        if tile_id in seen_ids:
            raise ProfileError(f"two tiles share the id {tile_id}")
        seen_ids.add(tile_id)

        members_in = row.get("members") or []
        if not isinstance(members_in, list) or not members_in:
            raise ProfileError(f"tile {tile_id} has no devices in it")
        if len(members_in) > MAX_MEMBERS:
            raise ProfileError(f"tile {tile_id} has more than {MAX_MEMBERS} devices in it")
        members: list[dict[str, Any]] = []
        for m in members_in:
            if not isinstance(m, dict):
                raise ProfileError(f"tile {tile_id} has a device that is not an object")
            entity_id = m.get("entity_id")
            slot = m.get("slot")
            if not isinstance(entity_id, str) or "." not in entity_id:
                raise ProfileError(f"tile {tile_id} has a device with no entity_id")
            if slot not in SLOTS:
                raise ProfileError(f"{slot} is not a place in a tile")
            _refuse_off_limits(entity_id)
            if _domain(entity_id) in ROUTINE_DOMAINS:
                raise ProfileError(f"{entity_id} is a scene or a script — add it as an action, not a device")
            if not _known(hass, entity_id):
                raise ProfileError(f"{entity_id} is not in this Home Assistant")
            # One entity, one thing. The hub resolves a clash first-wins and reports it, but an admin
            # looking at the panel should be stopped here rather than told afterwards.
            if (owner := claimed.get(entity_id)) is not None:
                raise ProfileError(f"{entity_id} is already in tile {owner}")
            claimed[entity_id] = tile_id
            members.append({"entity_id": entity_id, "slot": slot})

        primary = row.get("primary")
        if not isinstance(primary, str) or not any(m["entity_id"] == primary for m in members):
            raise ProfileError(f"tile {tile_id} needs one of its own devices as the main one")

        actions: list[dict[str, str]] = []
        for a in row.get("actions") or []:
            entity_id = a.get("entity_id") if isinstance(a, dict) else None
            if not isinstance(entity_id, str) or _domain(entity_id) not in ROUTINE_DOMAINS:
                raise ProfileError(f"tile {tile_id} has an action that is not a scene or a script")
            if not _known(hass, entity_id):
                raise ProfileError(f"{entity_id} is not in this Home Assistant")
            actions.append({"entity_id": entity_id})
        if len(actions) > MAX_ACTIONS:
            raise ProfileError(f"tile {tile_id} has more than {MAX_ACTIONS} actions")

        tile: dict[str, Any] = {"id": tile_id, "primary": primary, "members": members}
        if actions:
            tile["actions"] = actions
        for key in ("name", "icon", "area_id"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                tile[key] = value.strip()[:64]
        if (editable := row.get("editable")) is not None:
            tile["editable"] = _editable(editable)
        tiles.append(tile)

    return {"version": STORAGE_VERSION, "entities": entities, "tiles": tiles}


def _refuse_off_limits(entity_id: str) -> None:
    """A lock or a camera is reached through its own per-person grant, never through a tile.

    Not tidiness: `access.list` and `vision.list` are filtered by a grant held by one member, and a
    tile is one arrangement for the whole household. A lock inside a tile would launder that grant.
    """
    if _domain(entity_id) in OFF_LIMITS:
        raise ProfileError(f"{entity_id} is a kind of thing Hearth never arranges")


def _editable(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ProfileError("what the app may edit must be a list")
    unknown = [v for v in value if v not in EDITABLE]
    if unknown:
        raise ProfileError(f"{unknown[0]} is not something the app edits")
    # Stored in a fixed order so two saves of the same choice are the same bytes.
    return [f for f in ("name", "icon", "room", "order", "hide") if f in value]


def warnings(hass: HomeAssistant, profile: dict[str, Any]) -> list[str]:
    """Things worth telling an admin that are not reasons to refuse the save.

    Kept apart from `validate` on purpose: an arrangement that is merely unusual must still save, or
    the panel starts refusing homes whose wiring we did not imagine.
    """
    out: list[str] = []
    reg = er.async_get(hass)
    for tile in profile["tiles"]:
        for member in tile["members"]:
            entry = reg.async_get(member["entity_id"])
            if entry is None or entry.device_id is None:
                continue
            siblings = [e for e in er.async_entries_for_device(reg, entry.device_id) if e.entity_id != entry.entity_id]
            # One gang of a switch plate inside a tile leaves the other gangs drawn as lone switches.
            if len(siblings) >= 2 and all(s.domain == entry.domain for s in siblings):
                out.append(f"{member['entity_id']} is one of several on the same device; the others stay on their own")
    return out


async def async_load(hass: HomeAssistant) -> dict[str, Any]:
    """The stored arrangement, or an empty one. Never raises: a home must come up regardless."""
    if (cached := hass.data.get(DATA_PROFILE)) is not None:
        return cached
    try:
        store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        profile = await store.async_load() or dict(EMPTY)
    except Exception:  # noqa: BLE001 - an unreadable arrangement is no arrangement, not a dead home
        _LOGGER.exception("could not read the Hearth arrangement")
        profile = dict(EMPTY)
    hass.data[DATA_PROFILE] = profile
    return profile


async def async_save(hass: HomeAssistant, profile: dict[str, Any]) -> None:
    """Store a validated arrangement and make it the one every read sees."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(profile)
    hass.data[DATA_PROFILE] = profile


@callback
def cached(hass: HomeAssistant) -> dict[str, Any]:
    """What is loaded now, without awaiting. Empty before the first load, which is a valid answer."""
    return hass.data.get(DATA_PROFILE) or dict(EMPTY)
