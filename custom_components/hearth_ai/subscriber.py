"""
Push exposed-entity state to the hub as it changes, instead of being polled for it.

The app used to ask for every entity's state every 5 seconds, which meant a light switched at the
wall took up to 5 s to appear, every focused phone cost 12 round trips a minute over the home's one
WebSocket, and the audit log filled with reads. This sends the same data the other way round.

Three things keep it honest:

- **The same visibility rules as `entities.list`.** A pushed entity goes through `visible()`, so an
  entity the owner never exposed to Assist is no more visible here than it is there.
- **Debounced.** Dimming a light emits a state change per step; without a trailing-edge debounce
  that is a frame per step. One frame per `DEBOUNCE_S` at worst, carrying whatever changed.
- **Silent when disconnected.** Nothing queues up while the socket is down; the app's fallback poll
  is what recovers state after a reconnect, and a backlog of stale states would fight it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .handlers.registry import _entity_area, entity_dto, visible

_LOGGER = logging.getLogger(__name__)

# Trailing-edge debounce. Long enough that dragging a brightness slider produces a handful of frames
# rather than one per step; short enough that a wall switch still feels immediate.
DEBOUNCE_S = 0.4
# Matches the `entities.changed` schema. A burst larger than this is split across frames.
MAX_BATCH = 200


class StateSubscriber:
    """Watches every exposed entity and forwards changes to the hub."""

    def __init__(self, hass: HomeAssistant, send: Callable[[list[dict[str, Any]]], Coroutine[Any, Any, None]]) -> None:
        self._hass = hass
        self._send = send
        self._pending: dict[str, dict[str, Any]] = {}
        self._unsub: Callable[[], None] | None = None
        self._flush: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._unsub is not None:
            return
        # The bus, not `async_track_state_change_event`: that helper routes by a dict of entity ids
        # and has no "everything" form, so MATCH_ALL silently matches nothing. We want every entity,
        # because which ones are exposed changes without us being told.
        self._unsub = self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_change)
        _LOGGER.debug("state subscription started")

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._flush is not None:
            self._flush.cancel()
            self._flush = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._pending.clear()

    @callback
    def _on_change(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is None:
            # Removed entities are a structural change, not a state one; the next rooms fetch
            # notices. Pushing a half-entity here would only confuse the projection.
            return
        entity_id = event.data["entity_id"]
        ent = er.async_get(self._hass).async_get(entity_id)
        if not visible(self._hass, entity_id, ent):
            return
        area = _entity_area(ent, dr.async_get(self._hass))
        # Keyed by entity, so a burst on one entity collapses to its latest value.
        self._pending[entity_id] = entity_dto(state, ent, area)
        if self._flush is None:
            self._flush = self._hass.loop.call_later(DEBOUNCE_S, self._drain)

    @callback
    def _drain(self) -> None:
        self._flush = None
        if not self._pending:
            return
        batch = list(self._pending.values())
        self._pending.clear()
        self._task = self._hass.loop.create_task(self._deliver(batch))

    async def _deliver(self, batch: list[dict[str, Any]]) -> None:
        for i in range(0, len(batch), MAX_BATCH):
            chunk = batch[i : i + MAX_BATCH]
            try:
                await self._send(chunk)
            except Exception as err:  # noqa: BLE001 - a push is best-effort; the poll is the floor
                _LOGGER.debug("could not push %d state changes: %s", len(chunk), err)
                return
