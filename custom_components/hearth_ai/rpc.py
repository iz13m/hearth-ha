"""RPC dispatcher: the ONLY path from the hub into this Home Assistant instance.

The allowlist below is a verbatim copy of `HUB_TO_HA_ALLOWLIST` in
packages/shared. tests/test_allowlist.py asserts the two never drift. Anything not
listed here is refused with `method_not_allowed` regardless of what the hub asks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CAPABILITY_FOR_METHOD

_LOGGER = logging.getLogger(__name__)

ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "areas.list",
        "floors.list",
        "areas.create",
        "areas.set_floor",
        "floors.create",
        "areas.update",
        "areas.delete",
        "floors.update",
        "floors.delete",
        "areas.assign",
        "entities.exposable",
        "entities.expose",
        "access.list",
        "access.operate",
        "vision.list",
        "vision.snapshot",
        "vision.webrtc_config",
        "vision.webrtc_offer",
        "vision.webrtc_candidate",
        "vision.webrtc_close",
        "entities.list",
        "states.get",
        "presentation.get",
        "states.history",
        "helpers.types",
        "helpers.describe",
        "helpers.list",
        "helpers.create",
        "helpers.update",
        "helpers.rename",
        "helpers.delete",
        "labels.list",
        "labels.create",
        "labels.update",
        "labels.delete",
        "labels.assign",
        "categories.list",
        "categories.create",
        "categories.update",
        "categories.delete",
        "categories.assign",
        "notify.targets",
        "notify.send",
        "services.list",
        "automations.list",
        "automations.get",
        "automations.validate",
        "automations.create",
        "automations.update",
        "automations.delete",
        "automations.set_enabled",
        "scenes.list",
        "scenes.get",
        "scenes.create",
        "scenes.update",
        "scenes.delete",
        "scripts.list",
        "scripts.get",
        "scripts.validate",
        "scripts.create",
        "scripts.update",
        "scripts.delete",
        "devices.call",
        "scenes.activate",
        "scripts.run",
        "integrations.list",
        "integrations.discovered",
        "integrations.available",
        "integrations.flow_start",
        "integrations.flow_step",
        "integrations.flow_abort",
    }
)

ERROR_CODES: frozenset[str] = frozenset(
    {
        "method_not_allowed",
        "invalid_params",
        "not_found",
        "validation_failed",
        "not_editable",
        "ha_error",
        "timeout",
        "offline",
        "unauthorized",
        "not_entitled",
        "rate_limited",
        "internal",
    }
)


# `EntityId` in packages/shared, which the hub validates every request against before sending it. Home
# Assistant's state lookup falls back to `entity_id.lower()` and its registry and exposure lookups do
# not, so an id in any other form finds a state with no registry entry and no exposure setting behind
# it, and every check made from those passes (#646). The box therefore refuses it too, here, before
# any handler looks it up. `fullmatch`: `$` would let a trailing newline through.
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")

# The one parameter of each method that the shared schema types as `EntityId`, a single id or a list
# of them. tests/test_allowlist.py derives this from schema/methods.json and asserts the two are equal,
# so a method that gains an entity-id parameter is red there until it is added here.
ENTITY_ID_PARAMS: dict[str, str] = {
    "access.operate": "entity_id",
    "areas.assign": "entity_id",
    "categories.assign": "entity_ids",
    "devices.call": "entity_id",
    "entities.expose": "entity_ids",
    "labels.assign": "entity_ids",
    "scenes.activate": "entity_id",
    "scripts.run": "entity_id",
    "states.get": "entity_id",
    "states.history": "entity_id",
    "vision.snapshot": "entity_id",
    "vision.webrtc_config": "entity_id",
    "vision.webrtc_offer": "entity_id",
}
# Methods whose handler makes the same check itself, per id, because refusing one id must not refuse
# the batch: `entities.expose` answers `not_entity` for that id, a code the hub declares
# (`EXPOSURE_REFUSAL_CODES`), and changes the rest. Its check runs before any lookup, as this one does.
CHECKED_PER_ID: frozenset[str] = frozenset({"entities.expose"})


class RpcError(Exception):
    """Error surfaced to the hub as an `err` frame."""

    def __init__(self, code: str, message: str, data: Any = None) -> None:
        if code not in ERROR_CODES:
            code = "internal"
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


Handler = Callable[[HomeAssistant, dict[str, Any]], Awaitable[Any]]


assert set(CAPABILITY_FOR_METHOD) == ALLOWED_METHODS, "every allowlisted method needs a capability"


class Dispatcher:
    """Routes allowlisted methods to handlers, gated by the user's capability toggles."""

    def __init__(self, hass: HomeAssistant, capabilities: frozenset[str] | None = None) -> None:
        self._hass = hass
        self._handlers: dict[str, Handler] = {}
        # None = everything (tests / legacy); otherwise exactly the user's enabled set.
        self._capabilities = capabilities

    @property
    def capabilities(self) -> frozenset[str] | None:
        return self._capabilities

    def register(self, method: str, handler: Handler) -> None:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"refusing to register non-allowlisted method {method}")
        self._handlers[method] = handler

    async def dispatch(self, method: str, params: Any) -> Any:
        if method not in ALLOWED_METHODS:
            raise RpcError("method_not_allowed", f"method not allowed: {method}")
        cap = CAPABILITY_FOR_METHOD[method]
        if self._capabilities is not None and cap not in self._capabilities:
            raise RpcError("method_not_allowed", f"{cap} is disabled in the Azka AI options")
        # Live video is strictly more than a still, and the list of cameras it draws from comes from
        # `vision.list`. "Live on, stills off" is a state nobody designed for, so it is refused here
        # rather than left to produce something stranger further in (AgDR-0045).
        if cap == "vision.live" and self._capabilities is not None and "vision.view" not in self._capabilities:
            raise RpcError("method_not_allowed", "vision.live needs vision.view, which is disabled in the Azka AI options")
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError("method_not_allowed", f"method not implemented: {method}")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError("invalid_params", "params must be an object")
        if (key := ENTITY_ID_PARAMS.get(method)) is not None and method not in CHECKED_PER_ID:
            value = params.get(key)
            # A non-string is left to the handler, which refuses it as `invalid_params` as before.
            for entity_id in value if isinstance(value, list) else [value]:
                if isinstance(entity_id, str) and not ENTITY_ID.fullmatch(entity_id):
                    raise RpcError("not_found", f"unknown entity {entity_id}")
        try:
            return await handler(self._hass, params)
        except RpcError:
            raise
        except HomeAssistantError as err:
            raise RpcError("ha_error", str(err)) from err
        except Exception as err:  # noqa: BLE001 - never leak tracebacks to the hub
            _LOGGER.exception("handler %s failed", method)
            raise RpcError("internal", f"{type(err).__name__}") from err


def build_dispatcher(hass: HomeAssistant, capabilities: frozenset[str] | None = None) -> Dispatcher:
    """Create the dispatcher with every handler registered (gating happens at dispatch)."""
    from .handlers import access, areas, automations, control, exposure, helpers, history, integrations, live, notify, organise, presentation, registry, scenes, scripts, vision  # noqa: PLC0415

    d = Dispatcher(hass, capabilities)
    registry.register(d)
    presentation.register(d)
    history.register(d)
    helpers.register(d)
    organise.register(d)
    notify.register(d)
    access.register(d)
    vision.register(d)
    live.register(d)
    areas.register(d)
    exposure.register(d)
    control.register(d)
    integrations.register(d)
    automations.register(d)
    scenes.register(d)
    scripts.register(d)
    return d
