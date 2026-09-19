"""Hearth labels: the owner decides in Home Assistant how the Hearth app presents a device (AgDR-0034).

Five ordinary Home Assistant labels, created here so they are simply there to pick in HA's own UI:
`Hearth: tile`, `Hearth: read only`, `Hearth: setting`, `Hearth: diagnostic` and `Hearth: hide`.

This side only **reports** which of them are on an entity and on its device. What they mean —
precedence, conflicts, that only `hide` counts on a device — is decided by the hub, so a change to
any rule is a hub deploy and never another release of this integration.

A label counts when its id is the one stored for a role, **or** its name is exactly the role's name.
The id is what survives an owner renaming a label; the name is what lets an owner who deleted one
and made their own by hand still be understood.

Labels are not a control on what an AI may see or do. They are never deleted or renamed from here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers.normalized_name_base_registry import normalize_name
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Role (as the hub knows it) -> label name (as the owner sees it). Order is the order they are created.
ROLES: dict[str, str] = {
    "tile": "Hearth: tile",
    "read_only": "Hearth: read only",
    "setting": "Hearth: setting",
    "diagnostic": "Hearth: diagnostic",
    "hide": "Hearth: hide",
}
ROUTINE_ROLES: dict[str, str] = {"scene": "Hearth: scene"}
"""Labels that say what a routine *is*, not how an entity is presented.

`Hearth: scene` marks a script the Scenes tab should show as a scene, which is what a scene with
steps becomes (AgDR-0044). Deliberately **not** in `ROLES`: `roles_of` feeds `hearth_labels`, which
the hub resolves into a presentation for a *device*, and a routine label has no meaning there. An
owner may also put this label on a script they wrote themselves, which is the whole reason it is an
ordinary Home Assistant label rather than a flag in Hearth's own storage.
"""

ALL_LABELS: dict[str, str] = {**ROLES, **ROUTINE_ROLES}

DESCRIPTIONS: dict[str, str] = {
    "tile": "Hearth app: show this as its own tile. A setting stays an admin's to change.",
    "read_only": "Hearth app: show this, but nobody can change it from the app.",
    "setting": "Hearth app: treat this as one of its device's settings, changed by admins only.",
    "diagnostic": "Hearth app: treat this as a reading about its device, never changed from the app.",
    "hide": "Hearth app: do not show this in the app. Assistants still see what Assist shares.",
    "scene": "Hearth app: show this script in Scenes. A scene with steps or a schedule is a script.",
}
ICON = "mdi:home-heart"

STORAGE_KEY = f"{DOMAIN}.labels"
STORAGE_VERSION = 1
# hass.data key for {role: label_id}, loaded once per Home Assistant run.
DATA_LABEL_IDS = f"{DOMAIN}_label_ids"

_NAMES: dict[str, str] = {normalize_name(name): role for role, name in ROLES.items()}

# One `registry.changed` for a burst: deleting a label touches every entity that had it.
DEBOUNCE_S = 1.0

# hass.data key for "tell the hub this home's shape changed". Set by the client while it is up, so
# anything local — a Hearth label, a panel save — can reach the same path without holding the entry.
DATA_NOTIFY = f"{DOMAIN}_notify_registry"


@callback
def async_notify_registry_changed(hass: HomeAssistant) -> None:
    """Best effort. A home whose socket is down needs no telling: the hub re-reads on reconnect."""
    if (notify := hass.data.get(DATA_NOTIFY)) is not None:
        notify()


def _stored(hass: HomeAssistant) -> dict[str, str]:
    return hass.data.get(DATA_LABEL_IDS, {})


async def async_ensure_labels(hass: HomeAssistant) -> None:
    """Make sure each Hearth label exists, remembering its id. Idempotent; never fails setup.

    Runs on every setup, and setup re-runs on every options save, so it has to be cheap and silent
    when nothing is missing. Keeps a stored id that still exists (the owner may have renamed it);
    otherwise adopts a label by name; otherwise creates one. A label the owner deleted comes back
    on the next reload — deleting one is not how to opt out, not using it is.
    """
    try:
        store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored = dict(await store.async_load() or {})
        reg = lr.async_get(hass)
        changed = False
        for role, name in ALL_LABELS.items():
            label_id = stored.get(role)
            if label_id and reg.async_get_label(label_id) is not None:
                continue
            label = reg.async_get_label_by_name(name)
            if label is None:
                try:
                    label = reg.async_create(name, icon=ICON, description=DESCRIPTIONS[role])
                except ValueError:
                    # Created meanwhile (a second entry, or the owner at the same moment): adopt it.
                    label = reg.async_get_label_by_name(name)
            if label is None:
                continue
            stored[role] = label.label_id
            changed = True
        if changed:
            await store.async_save(stored)
        hass.data[DATA_LABEL_IDS] = stored
    except Exception:  # noqa: BLE001 - labels are a convenience; the connection must still come up
        _LOGGER.exception("could not set up the Hearth labels")


@callback
def role_of(hass: HomeAssistant, label_id: str) -> str | None:
    """The Hearth **presentation** role a label id stands for, or None.

    A routine label is not one: it says what a script is, not how an entity is shown, and letting it
    through here would put `scene` in `hearth_labels` for the hub to resolve as a placement.
    """
    for role, stored_id in _stored(hass).items():
        if stored_id == label_id:
            return role if role in ROLES else None
    label = lr.async_get(hass).async_get_label(label_id)
    return _NAMES.get(label.normalized_name) if label is not None else None


@callback
def label_id_for(hass: HomeAssistant, role: str) -> str | None:
    """The stored id of one Hearth label, or the id of a label with that exact name."""
    if stored_id := _stored(hass).get(role):
        return stored_id
    name = ALL_LABELS.get(role)
    label = lr.async_get(hass).async_get_label_by_name(name) if name else None
    return label.label_id if label is not None else None


@callback
def is_hearth_scene(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether this routine carries `Hearth: scene`.

    Read from the entity registry, so a script the owner labelled by hand in Home Assistant counts
    exactly as one Hearth labelled itself.
    """
    label_id = label_id_for(hass, "scene")
    if not label_id:
        return False
    entry = er.async_get(hass).async_get(entity_id)
    return bool(entry and label_id in (entry.labels or set()))


async def async_mark_hearth_scene(hass: HomeAssistant, entity_id: str, wanted: bool) -> None:
    """Put `Hearth: scene` on a routine, or take it off. Silent when the label does not exist yet."""
    label_id = label_id_for(hass, "scene")
    reg = er.async_get(hass)
    entry = reg.async_get(entity_id) if label_id else None
    if not label_id or entry is None:
        return
    labels = set(entry.labels or set())
    if wanted == (label_id in labels):
        return
    reg.async_update_entity(entity_id, labels=labels | {label_id} if wanted else labels - {label_id})


@callback
def roles_of(hass: HomeAssistant, label_ids: Iterable[str]) -> list[str]:
    """Hearth roles among these label ids, deduplicated, in a stable order."""
    found = {role for label_id in label_ids if (role := role_of(hass, label_id))}
    return [role for role in ROLES if role in found]


@callback
def hearth_labels(hass: HomeAssistant, ent: Any, dev_reg: dr.DeviceRegistry | None) -> dict[str, list[str]]:
    """The Hearth labels on an entity and on **its own** device, unresolved.

    A child device's parent's labels are not read: Home Assistant does not inherit labels, and the
    owner labelling a hub should not quietly hide everything plugged into it.
    """
    entity = roles_of(hass, getattr(ent, "labels", None) or ()) if ent is not None else []
    device: list[str] = []
    if ent is not None and getattr(ent, "device_id", None) and dev_reg is not None:
        dev = dev_reg.async_get(ent.device_id)
        if dev is not None:
            device = roles_of(hass, dev.labels or ())
    return {"entity": entity, "device": device}


class RegistryWatcher:
    """Tells the hub when a Hearth label changed, so the app follows within a second or two.

    Sends `registry.changed`, which carries nothing: the hub re-reads what it may already read. Only
    changes that touch a Hearth label count, so relabelling a device for an unrelated dashboard does
    not make every phone in the house re-read the home.
    """

    def __init__(self, hass: HomeAssistant, send: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._hass = hass
        self._send = send
        self._unsubs: list[Callable[[], None]] = []
        self._flush: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._told_old_hub = False

    def start(self) -> None:
        if self._unsubs:
            return
        bus = self._hass.bus
        self._unsubs = [
            bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_entity),
            bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_device),
            bus.async_listen(lr.EVENT_LABEL_REGISTRY_UPDATED, self._on_label),
        ]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._flush is not None:
            self._flush.cancel()
            self._flush = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _touches(self, label_ids: Iterable[str]) -> bool:
        # Any Hearth label, presentation or routine: a script gaining `Hearth: scene` changes what
        # the Scenes tab holds, which the app should see as soon as a presentation change.
        routine_ids = {label_id_for(self._hass, role) for role in ROUTINE_ROLES}
        return any(role_of(self._hass, label_id) or label_id in routine_ids for label_id in label_ids)

    @callback
    def _on_entity(self, event: Event[Any]) -> None:
        data = event.data
        if data.get("action") != "update" or "labels" not in data.get("changes", {}):
            return
        ent = er.async_get(self._hass).async_get(data["entity_id"])
        now = ent.labels if ent is not None else set()
        if self._touches(set(data["changes"]["labels"] or ()) | set(now)):
            self._schedule()

    @callback
    def _on_device(self, event: Event[Any]) -> None:
        data = event.data
        if data.get("action") != "update" or "labels" not in data.get("changes", {}):
            return
        dev = dr.async_get(self._hass).async_get(data["device_id"])
        now = dev.labels if dev is not None else set()
        if self._touches(set(data["changes"]["labels"] or ()) | set(now)):
            self._schedule()

    @callback
    def _on_label(self, event: Event[Any]) -> None:
        # A rename can make a label start or stop being a Hearth one; a delete of one of ours matters
        # too, and by then only its stored id says it was ours.
        if self._touches({event.data["label_id"]}):
            self._schedule()

    @callback
    def _schedule(self) -> None:
        if self._flush is None:
            self._flush = self._hass.loop.call_later(DEBOUNCE_S, self._drain)

    @callback
    def _drain(self) -> None:
        self._flush = None
        self._task = self._hass.loop.create_task(self._deliver())

    async def _deliver(self) -> None:
        try:
            await self._send()
        except Exception as err:  # noqa: BLE001 - best effort; the app's poll catches up anyway
            # An older hub answers `method_not_allowed`. Worth saying once, not on every label edit.
            if not self._told_old_hub:
                self._told_old_hub = True
                _LOGGER.debug("hub did not take registry.changed: %s", err)
