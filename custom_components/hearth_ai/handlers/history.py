"""What one entity has been reading, from Home Assistant's recorder (AgDR-0036).

Model-facing, unlike doors and cameras: an automation that says "if it has been below 5 degrees for
three hours" cannot be written from a snapshot, and a model asked to write one either guesses or
refuses. But the recorder is not simply `states.get` with older numbers — it holds *patterns*: when
the house is empty, when someone gets up, when the back door last moved. So it waits behind its own
capability, `entities.history`, default off, with its own switch in the options.

Three rules keep it honest.

- **The same door as `states.get`, only narrower.** Exposed to Assist, present, not disabled or
  hidden, and not in `OFF_LIMITS` — the union that adds `alarm_control_panel`, the domain whose
  *history* is a dated record of when the house was armed.
- **No attributes, ever.** `no_attributes=True` on every query. Recorder attribute rows never pass
  through `filter_attributes`, so a picture URL or a pair of coordinates would come back through a
  path the denylist does not watch, and none of it answers "what has this been reading".
- **Bounded, and honest about it.** One entity, a point ceiling, a window ceiling, and a query
  timeout inside the hub's own. Over the ceiling the answer is the *oldest* part of the window with
  `truncated` set and a `next_start` to resume from — never a thinned-out series, which would quietly
  answer a different question than the one asked.

Recording is optional in Home Assistant, so every recorder import happens inside a function
(`# noqa: PLC0415`), the way `vision.py` imports the camera component: a home that has switched
recording off must still load this integration, announce that the capability exists, and say so in
plain words when asked.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import OFF_LIMITS, _exposed

DEFAULT_POINTS = 500
MAX_POINTS = 2000
DEFAULT_HOURS = 24
MAX_HOURS = 8784  # 366 days
# A recorded state may be 255 characters; up to 2000 of them must not treble the frame. Same idea as
# MAX_ATTR_STR in registry.py, tighter because of how many there can be.
MAX_STATE_STR = 100
# A backstop, not a routine path: 2000 points x ~150 bytes is ~300 KB, well under this and far under
# the 1 MB frame, which the hub treats as a protocol violation and closes the socket over.
MAX_BYTES = 400_000
# Inside the hub's 15 s read timeout, as vision.py's is, so a slow database fails as "the recorder
# did not answer" rather than as an RPC timeout that looks like the home went offline.
QUERY_TIMEOUT_S = 10

PERIODS: tuple[str, ...] = ("5minute", "hour", "day", "week", "month")
PERIOD_SECONDS: dict[str, int] = {
    "5minute": 300,
    "hour": 3600,
    "day": 86_400,
    "week": 604_800,
    "month": 2_592_000,
}
# `state` and `last_reset` are deliberately not asked for: they are meter bookkeeping, and `change`
# already carries what a person means by "how much did it use that hour".
STAT_TYPES = {"min", "max", "mean", "sum", "change"}
NUMBER_KEYS = ("min", "max", "mean", "sum", "change")


def _entity(hass: HomeAssistant, entity_id: str) -> Any:
    """The entity this id names, or the same refusal `states.get` gives.

    One message for "hidden from you" and for "does not exist", because the difference is itself
    worth knowing: an id that answers differently is an id someone can probe for.
    """
    if entity_id.split(".", 1)[0] in OFF_LIMITS:
        raise RpcError("not_found", f"unknown entity {entity_id}")
    state = hass.states.get(entity_id)
    if state is None or not _exposed(hass, entity_id):
        raise RpcError("not_found", f"unknown entity {entity_id}")
    return state


def _parse(value: Any, key: str) -> datetime:
    parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise RpcError("invalid_params", f"{key} must be an ISO 8601 timestamp")
    return dt_util.as_utc(parsed)


def _window(params: dict[str, Any]) -> tuple[datetime, datetime]:
    """Resolve the window here, on the box, against the home's own clock."""
    hours = params.get("hours")
    start_s = params.get("start")
    if hours is not None and start_s is not None:
        raise RpcError("invalid_params", "give either hours or start, not both")

    end = _parse(params["end"], "end") if params.get("end") is not None else dt_util.utcnow()
    if start_s is not None:
        start = _parse(start_s, "start")
    else:
        if hours is None:
            hours = DEFAULT_HOURS
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= MAX_HOURS:
            raise RpcError("invalid_params", f"hours must be an integer 1..{MAX_HOURS}")
        start = end - timedelta(hours=hours)

    if start >= end:
        raise RpcError("invalid_params", "start must be before end")
    if end - start > timedelta(hours=MAX_HOURS):
        raise RpcError("invalid_params", f"the window may cover at most {MAX_HOURS // 24} days")
    return start, end


def _auto_period(seconds: float, limit: int) -> str | None:
    """The finest bucket that fits under the point cap, or None for individual readings.

    A day or less is asked about because someone wants the actual transitions; anything longer is
    asked about because someone wants the shape.
    """
    if seconds <= DEFAULT_HOURS * 3600:
        return None
    for period in ("hour", "day", "week", "month"):
        if math.ceil(seconds / PERIOD_SECONDS[period]) <= limit:
            return period
    return "month"


def _next_start(points: list[dict[str, Any]]) -> str | None:
    """Where the next page begins: just past the last point, never on it.

    A microsecond, because both recorder queries take their window from `start` **inclusive**. Handing
    back the last point's own time would return that point again — and, with the start-time state on
    top of it, a cursor that never moves.
    """
    if not points:
        return None
    last = dt_util.parse_datetime(points[-1]["t"])
    return (last + timedelta(microseconds=1)).isoformat() if last else None


def _unit(value: Any) -> str | None:
    """A unit the hub's schema will accept, or nothing. An attribute is whatever the integration put
    there, and a result that fails validation costs the caller the whole answer."""
    return value[:64] if isinstance(value, str) and value else None


def _state_points(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for state in rows:
        value = state.state
        if len(value) > MAX_STATE_STR:
            value = value[:MAX_STATE_STR] + "…"
        out.append({"t": (state.last_changed or state.last_updated).isoformat(), "state": value})
    return out


def _read(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    resolution: str,
    limit: int,
    resuming: bool,
) -> dict[str, Any]:
    """Everything that touches the database, in one job on the recorder's own thread.

    Metadata lookup and query together: the recorder has one executor and entering it twice for one
    question costs another queue wait behind whatever else the house is recording.
    """
    from homeassistant.components.recorder import history, statistics  # noqa: PLC0415
    from homeassistant.components.recorder.entity_options import is_entity_recorded  # noqa: PLC0415

    meta: dict[str, Any] | None = None
    if resolution != "states":
        # Its own metadata and nothing else: the id is an exposed entity of this home, so an external
        # statistic (whose id must contain ":", which `EntityId` rejects) cannot be named here.
        found = statistics.list_statistic_ids(hass, {entity_id})
        meta = found[0] if found else None

    if resolution == "states":
        period: str | None = None
    elif resolution == "auto":
        period = _auto_period((end - start).total_seconds(), limit)
    else:
        period = resolution

    if period is not None and meta is None:
        # Aggregates were asked for on something that has none — a binary_sensor, a text sensor.
        # `auto` quietly falls back to the readings themselves; an explicit period says so plainly,
        # because the caller asked for a specific thing and should hear that it does not exist.
        if resolution != "auto":
            return {
                "kind": "statistics",
                "period": period,
                "points": [],
                "more": False,
                "unit": None,
                "note": (
                    "Home Assistant keeps no hourly or daily statistics for this entity; ask again "
                    'with resolution "states" for its individual readings.'
                ),
            }
        period = None

    if period is None:
        if not is_entity_recorded(hass, entity_id):
            return {
                "kind": "states",
                "period": None,
                "points": [],
                "more": False,
                "unit": None,
                "note": "this entity is excluded from recording in this Home Assistant",
            }
        # `limit + 1` is how truncation is detected without asking twice.
        #
        # The start-time state — what the entity read *entering* the window — is worth having on a
        # fresh window, where it is the difference between "it was 19 all night" and no rows at all.
        # On a resumed page it is not: the recorder stamps it with the window start, so paging from
        # the last point's time would hand back a row at exactly that time forever. A page asked for
        # by `next_start` means "what happened after this", and that is what it gets.
        rows = history.state_changes_during_period(
            hass,
            start,
            end,
            entity_id,
            no_attributes=True,
            descending=False,
            limit=limit + 1,
            include_start_time_state=not resuming,
        ).get(entity_id, [])
        points = _state_points(rows)
        return {
            "kind": "states",
            "period": None,
            "points": points,
            "more": len(points) > limit,
            "unit": None,
            "note": None,
        }

    rows = statistics.statistics_during_period(hass, start, end, {entity_id}, period, None, STAT_TYPES)
    points: list[dict[str, Any]] = []
    for row in rows.get(entity_id, []):  # only the id that was asked for is ever read back
        point: dict[str, Any] = {"t": datetime.fromtimestamp(row["start"], UTC).isoformat()}
        for key in NUMBER_KEYS:
            value = row.get(key)
            if value is not None:
                point[key] = round(float(value), 4)  # trailing float noise is bytes, not information
        points.append(point)
    return {
        "kind": "statistics",
        "period": period,
        "points": points,
        "more": len(points) > limit,
        "unit": _unit(meta.get("display_unit_of_measurement")) if meta else None,
        "note": None,
    }


async def states_history(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """One entity's past, as individual readings or as statistics — see the module docstring."""
    entity_id = require_str(params, "entity_id")
    state = _entity(hass, entity_id)  # refuse before anything costs anything
    start, end = _window(params)

    resolution = params.get("resolution", "auto")
    if resolution not in ("auto", "states", *PERIODS):
        raise RpcError("invalid_params", "resolution must be auto, states, or a period")
    limit = params.get("limit", DEFAULT_POINTS)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_POINTS:
        raise RpcError("invalid_params", f"limit must be an integer 1..{MAX_POINTS}")

    # A predictable size is refused rather than truncated: the caller could have done this
    # arithmetic, and one sentence naming the numbers is a one-step fix, where a page of buckets
    # and a cursor is three. `auto` never lands here — it picks a period that fits.
    if resolution in PERIODS:
        buckets = math.ceil((end - start).total_seconds() / PERIOD_SECONDS[resolution])
        if buckets > limit:
            raise RpcError(
                "invalid_params",
                f"that window is {buckets} {resolution} buckets and the limit is {limit}; "
                "ask for a shorter window or a coarser period",
            )

    # Checked before the import, so a home that records nothing never pays for sqlalchemy either.
    if "recorder" not in hass.config.components:
        raise RpcError("ha_error", "this Home Assistant is not recording history, so there is no past to read")
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415

    instance = get_instance(hass)
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_S):
            # The recorder's own executor, as Home Assistant's history component does — not hass's,
            # whose pool serves everything else in the house.
            raw = await instance.async_add_executor_job(
                _read, hass, entity_id, start, end, resolution, limit, params.get("start") is not None
            )
    except TimeoutError as err:
        raise RpcError(
            "timeout",
            "the recorder did not answer in time; ask for a shorter window or a coarser period",
        ) from err
    except HomeAssistantError as err:
        raise RpcError("ha_error", str(err) or "the recorder could not answer") from err

    points: list[dict[str, Any]] = raw["points"]
    truncated = bool(raw["more"])
    if len(points) > limit:
        points = points[:limit]
    # Backstop only; the point and string ceilings above put this out of reach in practice.
    while points and len(json.dumps(points)) > MAX_BYTES:
        points = points[: max(1, int(len(points) * 0.8))]
        truncated = True

    note = raw["note"]
    if not note and not points:
        note = "nothing was recorded for this entity in that window"
    return {
        "entity_id": entity_id,
        "kind": raw["kind"],
        "period": raw["period"],
        "unit": raw["unit"] or _unit(state.attributes.get("unit_of_measurement")),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "points": points,
        "truncated": truncated,
        "next_start": _next_start(points) if truncated else None,
        "keep_days": getattr(instance, "keep_days", None),
        "note": note,
    }


def register(d: Dispatcher) -> None:
    d.register("states.history", states_history)
