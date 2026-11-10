"""The delivery guard: no companion-app command in a notification during a run Hearth started (#812).

R4 (AgDR-0076) refuses a companion-app command where a model *writes* one: an authored `notify.*`
action, `devices.call`, `notify.send`. It cannot see a household "Notify me" script whose message is
`{{ message }}`, started with `message: command_webview`: the household wrote the template, the
command arrives as data, and the phone obeys. The walkers read configs; only the call itself holds the
rendered value. So the boundary sits where every notification passes on its way to a phone:
`ServiceRegistry.async_call`.

**How it is wrapped.** `ServiceRegistry` has `__slots__`, so `hass.services.async_call = wrapper`
raises. The instance's class is swapped instead for `_GuardedServiceRegistry`, a subclass with no slots
of its own that overrides `async_call` and defers to it. Every caller reaches it: a script step
(`helpers/script.py`), a notify group forwarding to its members (`group/notify.py`, which passes no
context, so the group's own call is the one that is read), the websocket API, Hearth's own handlers.
`async_remove` restores the class. If the instance is not a plain `ServiceRegistry` (Home Assistant
changed it, or something else swapped it first) the guard is not installed, `active()` says so, and
the input paths fail closed instead (`handlers/inputs.py`).

**Which runs.** Hearth starts every model-reachable call (`devices.call`, `scripts.run`,
`scenes.activate`, `notify.send`) with a fresh `Context` from `hearth_context`. A script run keeps the
context it was started with, nested scripts included (`script/__init__.py` passes `service.context`
on), so its notify steps carry it. An automation a Hearth call triggered gets `Context(parent_id=...)`,
one level up, which is also read. Two levels up is not. The id is a ULID whose 80 random bits are four
random bytes (so ids in one millisecond differ) and an HMAC of the timestamp and those bytes under a
per-process key, so it is recognised without remembering it: nothing expires and no flood of calls can
evict it. A forged id only makes the guard stricter.

**What is refused**, on a `notify` call under such a context (`delivery_problems`), read from the
service data with `target` merged over it, as `ServiceRegistry.async_call` merges it after this check:
- `message` or `title` read by R4's matcher (`notify_message_problem`), trimmed and folded, against the
  Android and iOS command words merged (`NOTIFY_COMMAND_PREFIXES`);
- `data.command`, `data.url`, `data.clickAction`, a `uri` on any `data.actions` entry, and `data.push`
  carrying any `sound` or anything `critical`. `data.tag` with `clear_notification` is the message rule.

**Installed only over the method it was written against.** `async_install` compares the parameters of
`ServiceRegistry.async_call` with the ones this override forwards; if Home Assistant changes them the
guard is not installed, and `active()` is false, so the input paths fail closed rather than a call
reaching the phone through a parameter this does not read.

A refusal raises `ServiceValidationError`, which stops the run. It never names the value: it is logged
and fired on the bus as `hearth_ai_delivery_refused` with the service and the field names only.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import secrets
from typing import Any

from homeassistant.core import Context, HomeAssistant, ServiceRegistry, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.ulid import bytes_to_ulid, ulid_now, ulid_to_bytes, ulid_to_bytes_or_none

from .const import DOMAIN
from .policy import NOTIFY_COMMAND_RULE, notify_message_problem

_LOGGER = logging.getLogger(__name__)

EVENT_REFUSED = f"{DOMAIN}_delivery_refused"
DELIVERY_RULE = (
    "a notification sent during a run Hearth started may not carry a companion-app command, in its "
    f"message, its title or its data; {NOTIFY_COMMAND_RULE}"
)
NOTIFY_DOMAIN = "notify"
# Keys of a notification's `data` that make the phone act rather than show: Android's `command` and
# `clickAction`, iOS's `url`, and (below) an action button's `uri`.
COMMAND_DATA_KEYS: frozenset[str] = frozenset({"command", "url", "clickaction"})

_KEY = secrets.token_bytes(32)  # per process: a restart ends every run it marked
_INSTALLS = f"{DOMAIN}_delivery_installs"


def _mac(head: bytes) -> bytes:
    return hmac.new(_KEY, head, hashlib.sha256).digest()[:6]


def hearth_context() -> Context:
    """A fresh context for a call Hearth makes on a model's behalf, recognisable by its id alone.

    Sixteen bytes, as a ULID: six of timestamp, four random (two collided within a millisecond in a
    test of 2000), and six of HMAC over the first ten. A 48-bit MAC only has to make an ordinary
    context's id an unlikely match; a forged one would only make the guard stricter."""
    head = ulid_to_bytes(ulid_now())[:6] + secrets.token_bytes(4)
    return Context(id=bytes_to_ulid(head + _mac(head)))


def is_hearth_id(context_id: str | None) -> bool:
    raw = ulid_to_bytes_or_none(context_id) if isinstance(context_id, str) else None
    return raw is not None and hmac.compare_digest(raw[10:], _mac(raw[:10]))


def started_by_hearth(context: Context | None) -> bool:
    """The context itself, or its parent one level up (an automation a Hearth call triggered)."""
    return context is not None and (is_hearth_id(context.id) or is_hearth_id(context.parent_id))


def delivery_problems(service_data: dict[str, Any]) -> list[str]:
    """The fields of one notification that carry a companion-app command. Names only, never values."""
    found: list[str] = []
    for field in ("message", "title"):
        if notify_message_problem(service_data.get(field)):
            found.append(field)
    data = service_data.get("data")
    if data is not None and not isinstance(data, dict):
        found.append("data")
    elif isinstance(data, dict):
        for key, value in data.items():
            folded = key.lower() if isinstance(key, str) else ""
            if folded in COMMAND_DATA_KEYS and value not in (None, "", [], {}):
                found.append(f"data.{key}")
            elif folded == "actions" and isinstance(value, list):
                found.extend(f"data.{key}[{i}].uri" for i, a in enumerate(value) if isinstance(a, dict) and a.get("uri") not in (None, ""))
            elif folded == "push" and _critical_or_sound(value):
                found.append(f"data.{key}")
    # `data.tag` matters only beside a `clear_notification` message, which the message rule refuses.
    return found


def _critical_or_sound(push: Any) -> bool:
    if isinstance(push, dict):
        return any(
            (isinstance(k, str) and k.lower() in ("sound", "critical")) or _critical_or_sound(v) for k, v in push.items()
        )
    if isinstance(push, list):
        return any(_critical_or_sound(v) for v in push)
    return isinstance(push, str) and push.strip().lower() == "critical"


class _GuardedServiceRegistry(ServiceRegistry):
    """`ServiceRegistry`, with every `notify` call under a Hearth context read first."""

    __slots__ = ()

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        context: Context | None = None,
        target: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> ServiceResponse:
        if isinstance(domain, str) and domain.lower() == NOTIFY_DOMAIN and started_by_hearth(context):
            # `target` is merged over the data after this, by `ServiceRegistry.async_call` itself.
            fields = delivery_problems({**(service_data or {}), **(target or {})})
            if fields:
                _refuse(self._hass, f"{domain}.{service}", fields, context)
        return await super().async_call(domain, service, service_data, blocking, context, target, return_response)


def _refuse(hass: HomeAssistant, action: str, fields: list[str], context: Context | None) -> None:
    # The audit: the action and the field names, never the value, which is the command itself.
    _LOGGER.warning("Refused %s during a run Hearth started: %s carries a companion-app command", action, ", ".join(fields))
    hass.bus.async_fire(EVENT_REFUSED, {"action": action, "fields": fields}, context=context)
    raise ServiceValidationError(f"{action}: {', '.join(fields)}: {DELIVERY_RULE}")


# What the override forwards: name, kind and default of each parameter, in order.
EXPECTED_PARAMETERS = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
    ("domain", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
    ("service", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
    ("service_data", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
    ("blocking", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
    ("context", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
    ("target", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
    ("return_response", inspect.Parameter.POSITIONAL_OR_KEYWORD, False),
)


def _parameters(func: Any) -> tuple[tuple[str, Any, Any], ...]:
    return tuple((p.name, p.kind, p.default) for p in inspect.signature(func).parameters.values())


def async_install(hass: HomeAssistant) -> bool:
    """Install the guard (counted, so a second entry does not remove the first's). Whether it is on."""
    services = hass.services
    if _parameters(ServiceRegistry.async_call) != EXPECTED_PARAMETERS:
        _LOGGER.warning("Home Assistant's service call changed shape; the notification delivery guard is not installed and inputs to a script that notifies are refused")
        return False
    if type(services) is ServiceRegistry:
        try:
            services.__class__ = _GuardedServiceRegistry
        except TypeError:
            _LOGGER.warning("Could not install the notification delivery guard; inputs to a script that notifies are refused")
            return False
    if type(services) is not _GuardedServiceRegistry:
        _LOGGER.warning("Could not install the notification delivery guard; inputs to a script that notifies are refused")
        return False
    hass.data[_INSTALLS] = hass.data.get(_INSTALLS, 0) + 1
    return True


def async_remove(hass: HomeAssistant) -> None:
    """Undo one `async_install`; the last one restores Home Assistant's own class."""
    count = hass.data.get(_INSTALLS, 0) - 1
    if count > 0:
        hass.data[_INSTALLS] = count
        return
    hass.data.pop(_INSTALLS, None)
    if type(hass.services) is _GuardedServiceRegistry:
        hass.services.__class__ = ServiceRegistry


def active(hass: HomeAssistant) -> bool:
    """Whether the guard stands between a Hearth run and a phone right now."""
    return type(hass.services) is _GuardedServiceRegistry
