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

from ..rpc import Dispatcher, RpcError
from .common import require_str

DOMAIN_NOTIFY = "notify"
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
    for service in sorted(hass.services.async_services().get(DOMAIN_NOTIFY, {})):
        if service in NOT_A_PERSON:
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
                DOMAIN_NOTIFY, "send_message", {**data, "entity_id": target}, blocking=True
            )
        else:
            await hass.services.async_call(DOMAIN_NOTIFY, name, data, blocking=True)
    except HomeAssistantError as err:
        raise RpcError("ha_error", str(err) or "the notification could not be sent") from err
    return {"target": target, "sent": True}


def register(d: Dispatcher) -> None:
    d.register("notify.targets", notify_targets)
    d.register("notify.send", notify_send)
