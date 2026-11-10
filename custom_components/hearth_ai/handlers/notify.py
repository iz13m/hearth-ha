"""Telling someone something (AgDR-0041).

An automation that notices the washing is done is only half an automation; the other half is saying
so. That half already half-worked: `notify` is in neither `DENIED_ACTION_DOMAINS` nor
`HIDDEN_DOMAINS`, so an authored automation could always call `notify.mobile_app_<phone>`, and
`services.list` listed those services. Two things were missing.

**Knowing who can be told.** Home Assistant has two notification machines: the older one registers a
*service* per target (`notify.mobile_app_alices_phone`), the newer one an *entity* per target
(`notify.send_message` aimed at `notify.alices_phone`). Entities are the modern shape and `notify` is
not in HA's `DEFAULT_EXPOSED_DOMAINS`, so they never reached `entities.list` and a model could not
name one. `notify.targets` lists both, with names and no state — it says who could be told, never
what anyone was told.

**Saying something now.** Writing an automation to send one message is a silly way to answer "let me
know when you have it". `notify.send` sends one, to one target the home already has.

Behind its own opt-in capability. A notification is not dangerous the way unlocking a door is, but it
reaches a person's phone at whatever hour the model chooses, and a mistake is someone woken up — so
it waits for a yes, and is bounded here rather than trusted to be sensible.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..delivery import hearth_context
from ..policy import DENIED_ACTIONS, _fold, notify_message_problem
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .remotes import _visit_services

DOMAIN_NOTIFY = "notify"

# Legacy notify platforms whose "notification" is a command to something that acts on devices (#782).
# Google Assistant SDK's `notify.py` (HA 2026.9.3) sends `broadcast {message}` (or `broadcast to
# {target} {message}`) through `async_send_text_commands`, the sink `send_text_command` reaches and
# #779 denied: both fields are free text, read by Google's NLU, which controls whatever is linked to
# the account. Discovery registers it as `notify.google_assistant_sdk`, which `DENIED_ACTIONS` names;
# but `notify:` in YAML loads any legacy platform under any `name` (`notify/legacy.py`:
# `service_name = slugify(conf_name or "notify")`), so `{platform: google_assistant_sdk, name:
# kitchen}` is `notify.kitchen`, and without a name it is `notify.notify`. Only the box knows which
# platform is behind a name, so it is refused here by platform.
DENIED_NOTIFY_PLATFORMS: frozenset[str] = frozenset({"google_assistant_sdk"})
# The legacy `group` notify platform forwards a message to each `notify.<action>` it lists.
_GROUP_PLATFORM = "group"
_PLATFORM_RULE = "it sends its message to Google Assistant as a spoken command, which Hearth does not relay"
# The legacy services that are not a person: sending to them is allowed but they are not offered as
# "who could be told", because a model asked to tell someone should not pick the log file.
NOT_A_PERSON = frozenset({"notify", "send_message", "persistent_notification"})

MAX_MESSAGE = 1000
MAX_TITLE = 100
# A person's phone, not a channel. Ten in a minute is already more than anyone wants; the cap is
# here so a loop in a model's plan cannot become a night of buzzing.
RATE_LIMIT = 10
RATE_WINDOW_S = 60.0
DATA_SENT = "hearth_ai_notify_sent"


def _check_rate(hass: HomeAssistant) -> None:
    now = time.monotonic()
    sent: list[float] = [t for t in hass.data.get(DATA_SENT, []) if now - t < RATE_WINDOW_S]
    if len(sent) >= RATE_LIMIT:
        raise RpcError(
            "rate_limited",
            f"that is {RATE_LIMIT} notifications in a minute; the rest of them would be noise, so Hearth stopped",
        )
    sent.append(now)
    hass.data[DATA_SENT] = sent


def _legacy_services(hass: HomeAssistant) -> dict[str, list[tuple[str, Any]]]:
    """Every legacy `notify.<name>` service, folded, to the `(platform, service object)` pairs behind it.

    Read from `hass.data[NOTIFY_SERVICES]`, which `notify/legacy.py` fills with each platform's
    `BaseNotificationService`; the object knows its own service name and per-target service names.
    Two platforms asking for the same name are both kept: HA registers the first, and refusing for
    either is the side to err on.
    """
    from homeassistant.components.notify.legacy import NOTIFY_SERVICES  # noqa: PLC0415 - only once notify loaded

    out: dict[str, list[tuple[str, Any]]] = {}
    for platform, services in (hass.data.get(NOTIFY_SERVICES) or {}).items():
        for svc in services or ():
            names = [getattr(svc, "_service_name", None), *(getattr(svc, "registered_targets", None) or {})]
            for name in names:
                if isinstance(name, str):
                    out.setdefault(_fold(name), []).append((str(platform), svc))
    return out


def notify_platform_refusal(
    hass: HomeAssistant, service: str, services: dict[str, list[tuple[str, Any]]] | None = None
) -> str | None:
    """Why `notify.<service>` may not be called by Hearth, or None when it may (#782).

    Refused when a denied platform is behind the name, directly or through a legacy notify group,
    followed recursively. A group whose member list cannot be read is refused: what it forwards to is
    unknown. A name with nothing in `hass.data` behind it (a notify entity's `send_message`, a mocked or
    not-yet-loaded service) is left to the other checks, except a name `DENIED_ACTIONS` holds, which is
    refused wherever it appears, a group member included, so a group naming the discovered service
    before the integration has loaded agrees with `notify_targets`.

    `services` is `_legacy_services(hass)` when the caller already has it (`notify_targets` asks this
    of every legacy service).
    """
    if services is None:
        services = _legacy_services(hass)
    seen: set[str] = set()

    def reaches(name: str) -> str | None:
        name = _fold(name)
        if name in seen:
            return None
        seen.add(name)
        if f"{DOMAIN_NOTIFY}.{name}" in DENIED_ACTIONS:
            return f"notify.{name} is a notify service Hearth refuses by name"
        for platform, svc in services.get(name, ()):
            if platform in DENIED_NOTIFY_PLATFORMS:
                return f"notify.{name} belongs to {platform}"
            if platform == _GROUP_PLATFORM:
                members = getattr(svc, "entities", None)
                if not isinstance(members, list):
                    return f"notify.{name} is a notify group whose members Hearth cannot read"
                for member in members:
                    action = member.get("action") if isinstance(member, dict) else None
                    if not isinstance(action, str):
                        return f"notify.{name} is a notify group whose members Hearth cannot read"
                    if why := reaches(action):
                        return f"notify.{name} forwards to {why}"
        return None

    why = reaches(service)
    return f"{why}; {_PLATFORM_RULE}" if why else None


def notify_call_violations(hass: HomeAssistant, domain: str, service: str) -> list[str]:
    """The control path's half: `devices.call` with a `notify.*` service (#782)."""
    if _fold(domain) != DOMAIN_NOTIFY:
        return []
    why = notify_platform_refusal(hass, service)
    return [f"{domain}.{service}: {why}"] if why else []


def find_notify_violations(hass: HomeAssistant, config: Any, path: str = "config") -> list[str]:
    """Every `notify.*` action in a config whose service a denied platform is behind (#782).

    The authoring half, run where `find_remote_violations` runs: an authored automation or script, a
    labelled script when it runs, a config-flow form value, and a household script one hop away. A
    templated action name never gets here, `find_policy_violations` refuses it outright.
    """
    problems: list[str] = []

    def each(node: dict[str, Any], action: str, p: str) -> None:
        domain, _, service = _fold(action).partition(".")
        if domain == DOMAIN_NOTIFY and service and (why := notify_platform_refusal(hass, service)):
            problems.append(f"{p}: action {action}: {why}")

    _visit_services(config, path, each)
    return list(dict.fromkeys(problems))


async def notify_targets(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Who this home can tell something, of both machines.

    Carries names and nothing else. A notification target's *state* is the timestamp of the last
    message it sent, which is a record of when someone was contacted — not something a list of who
    exists needs to carry.
    """
    rows: list[dict[str, Any]] = []

    # Modern: one entity per target. Not exposed to Assist by default, so this is the only way a
    # model learns they exist — hence names only, and `notify` itself is not an off-limits domain.
    for state in hass.states.async_all(DOMAIN_NOTIFY):
        rows.append(
            {
                "target": state.entity_id,
                "kind": "entity",
                "name": state.name,
                "available": state.state != "unavailable",
            }
        )

    # Legacy: one service per target.
    legacy = _legacy_services(hass)
    for service in sorted(hass.services.async_services().get(DOMAIN_NOTIFY, {})):
        if service in NOT_A_PERSON:
            continue
        # Not a person either: a command to Google Assistant (#782). Left out here, so `notify.send`,
        # which only reaches what this lists, cannot reach it.
        if notify_platform_refusal(hass, service, legacy):
            continue
        rows.append(
            {
                "target": f"{DOMAIN_NOTIFY}.{service}",
                "kind": "service",
                "name": service.replace("_", " ").replace("mobile app ", "").strip().title(),
                "available": True,
            }
        )
    return sorted(rows, key=lambda r: r["target"])


async def notify_send(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Send one message to one target the home already has."""
    target = require_str(params, "target")
    message = require_str(params, "message")
    if not message.strip() or len(message) > MAX_MESSAGE:
        raise RpcError("invalid_params", f"message must be 1..{MAX_MESSAGE} characters")
    # A companion-app command, which a phone carries out rather than shows (AgDR-0076). Home Assistant
    # renders the message as a template, so it is read the way the walkers read one.
    if problem := notify_message_problem(message):
        raise RpcError("method_not_allowed", f"message {problem}")
    title = params.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE):
        raise RpcError("invalid_params", f"title must be a string of at most {MAX_TITLE} characters")

    domain, _, name = target.partition(".")
    if domain != DOMAIN_NOTIFY or not name:
        raise RpcError("invalid_params", "target must be one of the targets notify.targets listed")

    known = {row["target"]: row for row in await notify_targets(hass, {})}
    row = known.get(target)
    if row is None:
        raise RpcError("not_found", f"this home has no notification target {target}")
    _check_rate(hass)

    data: dict[str, Any] = {"message": message}
    if title:
        data["title"] = title
    try:
        if row["kind"] == "entity":
            await hass.services.async_call(
                DOMAIN_NOTIFY, "send_message", {**data, "entity_id": target}, blocking=True, context=hearth_context()
            )
        else:
            await hass.services.async_call(DOMAIN_NOTIFY, name, data, blocking=True, context=hearth_context())
    except HomeAssistantError as err:
        raise RpcError("ha_error", str(err) or "the notification could not be sent") from err
    return {"target": target, "sent": True}


def register(d: Dispatcher) -> None:
    d.register("notify.targets", notify_targets)
    d.register("notify.send", notify_send)
