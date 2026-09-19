"""The Hearth panel in the Home Assistant sidebar (AgDR-0046).

Everything here is local to Home Assistant. The panel never talks to the hub: it reads this home's
own registries and writes the arrangement to `helpers.storage`, and the hub reads that separately
over `presentation.get`. So it works with the hub unreachable, needs no credential in the browser,
and is admin-only because Home Assistant itself says so — `require_admin=True` means the sidebar item
does not exist for anyone else and the panel is refused if they ask for it by URL.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .const import DOMAIN
from .handlers.exposure import apply_exposure
from .handlers.registry import ASSISTANT, OFF_LIMITS
from .labels import async_notify_registry_changed, roles_of
from .presentation import ROUTINE_DOMAINS, ProfileError, async_load, async_save, validate, warnings

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "/hearth_ai_panel"
PANEL_PATH = "hearth"
PANEL_TITLE = "Hearth"
PANEL_ICON = "mdi:fireplace"
# The custom element the bundle defines. Versioned in the URL so a browser that cached yesterday's
# panel does not keep serving it after an update.
PANEL_ELEMENT = "hearth-ai-panel"

DATA_PANEL = f"{DOMAIN}_panel_registered"


async def async_register_panel(hass: HomeAssistant, version: str) -> None:
    """Put Hearth in the sidebar.

    Never fails setup, and never a hard dependency. `frontend` is in `after_dependencies`, not
    `dependencies`, because making it required would stop this integration loading at all on a Home
    Assistant that has no frontend package — and the sidebar is a convenience while the connection to
    the hub is the product. Same posture as `async_ensure_labels`.

    Idempotent: setup re-runs on every options save, and a second config entry must not register a
    second panel.
    """
    if hass.data.get(DATA_PANEL):
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415

        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_URL, str(Path(__file__).parent / "panel"), cache_headers=False)]
        )
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_PATH,
            # The whole access control. An admin of this Home Assistant owns the box and already
            # decides everything here; anyone else never sees that the panel exists.
            require_admin=True,
            update=True,
            config={
                "_panel_custom": {
                    "name": PANEL_ELEMENT,
                    "embed_iframe": False,
                    "trust_external": False,
                    # Versioned, or a browser keeps yesterday's panel after an update.
                    "module_url": f"{PANEL_URL}/panel.js?v={version}",
                }
            },
        )
        _register_commands(hass)
        hass.data[DATA_PANEL] = True
    except Exception:  # noqa: BLE001 - no sidebar is a smaller problem than no Hearth
        _LOGGER.exception("could not add Hearth to the sidebar")


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    """Take it out of the sidebar when the last entry unloads."""
    if not hass.data.pop(DATA_PANEL, None):
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415

        frontend.async_remove_panel(hass, PANEL_PATH)
    except Exception:  # noqa: BLE001 - a panel that will not go away must not fail an unload
        _LOGGER.debug("could not remove the Hearth panel", exc_info=True)


def _register_commands(hass: HomeAssistant) -> None:
    for command in (panel_state, panel_save, panel_expose):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/panel/state"})
@websocket_api.require_admin
@websocket_api.async_response
async def panel_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Everything the panel draws: this home's rooms and entities, and how they stand today."""
    connection.send_result(msg["id"], await _state(hass))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/panel/save", vol.Required("profile"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def panel_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Store an arrangement, and tell the hub at once that this home looks different."""
    try:
        profile = validate(hass, msg["profile"])
    except ProfileError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    await async_save(hass, profile)
    _publish(hass)
    connection.send_result(msg["id"], {"profile": profile, "warnings": warnings(hass, profile)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/expose",
        vol.Required("entity_ids"): [str],
        vol.Required("expose"): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def panel_expose(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Share entities with Assist, or stop — the same gate the hub's own method goes through.

    Partial success, like `entities.expose`: asking for eight and having one refused changes the
    seven and names the one that did not.
    """
    ent_reg = er.async_get(hass)
    changed: list[str] = []
    refused: list[dict[str, str]] = []
    for entity_id in msg["entity_ids"][:100]:
        reason = apply_exposure(hass, entity_id, msg["expose"], ent_reg)
        if reason is None:
            changed.append(entity_id)
        else:
            refused.append({"entity_id": entity_id, "reason": reason})
    if changed:
        _publish(hass)
    connection.send_result(msg["id"], {"changed": changed, "refused": refused})


def _publish(hass: HomeAssistant) -> None:
    """Tell the hub this home's shape changed.

    Deliberately the **existing** `registry.changed`, not a message of its own: that path already
    drops every cache at once, re-resolves open event streams and makes the apps reload their rooms.
    A new message type would have had to earn all of that again.
    """
    async_notify_registry_changed(hass)


async def _state(hass: HomeAssistant) -> dict[str, Any]:
    """This home as the panel needs to see it.

    Every entity the registry knows, not only the shared ones: choosing what to share is half of what
    the panel is for. Carries **no state and no attributes** — the same rule `entities.exposable`
    follows, for the same reason. Listing what a home has must never be a way to read what it says.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    areas = [{"area_id": a.id, "name": a.name, "floor_id": a.floor_id} for a in area_reg.async_list_areas()]
    entities: list[dict[str, Any]] = []
    for ent in ent_reg.entities.values():
        if ent.domain in OFF_LIMITS or ent.disabled_by is not None:
            continue
        device = dev_reg.async_get(ent.device_id) if ent.device_id else None
        entities.append(
            {
                "entity_id": ent.entity_id,
                "name": ent.name or ent.original_name,
                "domain": ent.domain,
                "area_id": ent.area_id or (device.area_id if device else None),
                "device_id": ent.device_id,
                "device_name": (device.name_by_user or device.name) if device else None,
                "entity_category": ent.entity_category.value if ent.entity_category else None,
                "hidden": ent.hidden_by is not None,
                "exposed": _exposed(hass, ent.entity_id),
                # What a Hearth label is already saying, so the panel can show it and offer to take
                # it over rather than quietly disagreeing with it.
                "labels": sorted(roles_of(hass, ent.labels)),
            }
        )

    # A registry entry is not the only kind of entity there is: a YAML template or an MQTT entity
    # without a `unique_id` has a state and no entry, and `entities.list` returns it — so the app can
    # already be showing something the panel would refuse to admit exists. Listed by its state, with
    # the same nothing-about-what-it-reads rule; `friendly_name` is a name, not a reading.
    known = {e["entity_id"] for e in entities}
    for state in hass.states.async_all():
        if state.entity_id in known or state.domain in OFF_LIMITS or state.domain in ROUTINE_DOMAINS:
            continue
        entities.append(
            {
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name"),
                "domain": state.domain,
                "area_id": None,
                "device_id": None,
                "device_name": None,
                "entity_category": None,
                "hidden": False,
                "exposed": _exposed(hass, state.entity_id),
                "labels": [],
            }
        )
    entities.sort(key=lambda e: e["entity_id"])

    # Scenes and scripts a tile can offer as a Run row. Names only: a scene's state is when it last
    # ran, which is not the panel's business.
    routines = sorted(
        (
            {"entity_id": state.entity_id, "name": state.attributes.get("friendly_name") or state.entity_id}
            for state in hass.states.async_all(["scene", "script"])
        ),
        key=lambda r: r["entity_id"],
    )

    return {"areas": areas, "entities": entities, "routines": routines, "profile": await async_load(hass)}


def _exposed(hass: HomeAssistant, entity_id: str) -> bool:
    try:
        return async_should_expose(hass, ASSISTANT, entity_id)
    except Exception:  # noqa: BLE001 - no exposure store means nothing is shared
        return False
