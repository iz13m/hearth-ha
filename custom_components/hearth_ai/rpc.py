"""RPC dispatcher: the ONLY path from the hub into this Home Assistant instance.

The allowlist below is a verbatim copy of `HUB_TO_HA_ALLOWLIST` in
packages/shared. tests/test_allowlist.py asserts the two never drift. Anything not
listed here is refused with `method_not_allowed` regardless of what the hub asks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CAPABILITY_FOR_METHOD

_LOGGER = logging.getLogger(__name__)

ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "areas.list",
        "entities.list",
        "states.get",
        "services.list",
        "automations.list",
        "automations.get",
        "automations.validate",
        "automations.create",
        "automations.update",
        "automations.delete",
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
            raise RpcError("method_not_allowed", f"{cap} is disabled in the Hearth AI options")
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError("method_not_allowed", f"method not implemented: {method}")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError("invalid_params", "params must be an object")
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
    from .handlers import automations, control, integrations, registry, scenes, scripts  # noqa: PLC0415

    d = Dispatcher(hass, capabilities)
    registry.register(d)
    control.register(d)
    integrations.register(d)
    automations.register(d)
    scenes.register(d)
    scripts.register(d)
    return d
