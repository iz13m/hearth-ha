"""`states.history` — what one entity has been reading (AgDR-0036).

`recorder_mock` is requested **before** `core` everywhere, because the `core` fixture does not set
recording up and the recorder has to exist before the entities whose past is being asked about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.handlers import history
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SENSOR = "sensor.hall_temperature"


def _expose(hass: HomeAssistant, entity_id: str) -> None:
    """A sensor is not exposed to Assist by default, so nothing is readable until it is."""
    async_expose_entity(hass, "conversation", entity_id, True)


async def _record(hass: HomeAssistant, entity_id: str, values: list[str], **attrs) -> None:
    for value in values:
        hass.states.async_set(entity_id, value, {"unit_of_measurement": "°C", **attrs})
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def test_readings_come_back_in_order_and_carry_no_attributes(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["19.0", "20.5", "21.5"], friendly_name="Hall", entity_picture="/secret.png")

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 24})

    assert res["kind"] == "states"
    assert res["period"] is None
    assert res["unit"] == "°C"
    assert res["truncated"] is False and res["next_start"] is None
    assert [p["state"] for p in res["points"]][-3:] == ["19.0", "20.5", "21.5"]
    assert [p["t"] for p in res["points"]] == sorted(p["t"] for p in res["points"])
    # The security claim, asserted as a claim: a point is a time and a reading, and nothing else.
    # `entity_picture` is in the attribute denylist, but recorder rows never pass through it.
    for point in res["points"]:
        assert set(point) == {"t", "state"}


async def test_a_long_state_is_truncated(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["x" * 255])

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 1})
    longest = max(len(p["state"]) for p in res["points"])
    assert longest == history.MAX_STATE_STR + 1  # the ellipsis says it was cut


async def test_truncation_pages_from_the_oldest_reading(recorder_mock, core: HomeAssistant) -> None:
    """Paging walks the window once: every reading, in order, none twice, and the cursor always moves.

    The cursor used to be the last point's own time, and both recorder queries take their window from
    `start` inclusive — so the second page returned the first page again, for ever.
    """
    _expose(core, SENSOR)
    await _record(core, SENSOR, [str(n) for n in range(12)])

    d = build_dispatcher(core)
    first = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 1, "limit": 5})
    assert len(first["points"]) == 5
    assert first["truncated"] is True
    assert first["next_start"] > first["points"][-1]["t"]

    seen = list(first["points"])
    cursor = first["next_start"]
    for _ in range(10):  # a cursor that stalls fails here rather than hanging
        page = await d.dispatch("states.history", {"entity_id": SENSOR, "start": cursor, "limit": 5})
        assert all(p["t"] >= cursor for p in page["points"])
        seen += page["points"]
        if not page["truncated"]:
            break
        assert page["next_start"] > cursor
        cursor = page["next_start"]
    else:
        pytest.fail("paging never reached the end of the window")

    times = [p["t"] for p in seen]
    assert times == sorted(times)
    assert len(times) == len(set(times))
    assert [p["state"] for p in seen][-12:] == [str(n) for n in range(12)]


async def test_statistics_are_returned_as_buckets(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["20.0"])
    top = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async_import_statistics(
        core,
        {
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "name": "Hall",
            "source": "recorder",
            "statistic_id": SENSOR,
            "unit_class": "temperature",
            "unit_of_measurement": "°C",
        },
        [
            {"start": top - timedelta(hours=2), "min": 18.0, "max": 22.0, "mean": 20.0},
            {"start": top - timedelta(hours=1), "min": 19.0, "max": 23.0, "mean": 21.0},
        ],
    )
    await async_wait_recording_done(core)

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 6, "resolution": "hour"})
    assert res["kind"] == "statistics" and res["period"] == "hour"
    assert res["unit"] == "°C"
    assert [p["mean"] for p in res["points"]] == [20.0, 21.0]
    assert [p["min"] for p in res["points"]] == [18.0, 19.0]
    assert all("state" not in p for p in res["points"])


async def test_auto_picks_readings_for_a_short_window(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["19.0", "20.0"])

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 6})
    assert res["kind"] == "states"


async def test_auto_falls_back_to_readings_when_there_are_no_statistics(recorder_mock, core: HomeAssistant) -> None:
    """A binary_sensor has no statistics, and "how often did it fire last month" must still work."""
    _expose(core, "binary_sensor.back_door")
    await _record(core, "binary_sensor.back_door", ["on", "off"])

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": "binary_sensor.back_door", "hours": 720})
    assert res["kind"] == "states"


async def test_an_explicit_period_on_an_entity_without_statistics_says_so(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, "binary_sensor.back_door")
    await _record(core, "binary_sensor.back_door", ["on"])

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": "binary_sensor.back_door", "hours": 48, "resolution": "day"})
    assert res["kind"] == "statistics" and res["points"] == []
    assert "no hourly or daily statistics" in res["note"]


async def test_too_many_buckets_is_refused_before_the_database(recorder_mock, core: HomeAssistant, monkeypatch) -> None:
    """The caller could have done this arithmetic, so it gets one sentence instead of 8784 buckets."""
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["20.0"])
    monkeypatch.setattr(history, "_read", lambda *a, **k: pytest.fail("the database was queried"))

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 8784, "resolution": "5minute"})
    assert ei.value.code == "invalid_params"
    assert "coarser period" in ei.value.message


@pytest.mark.parametrize(
    "entity_id",
    [
        "lock.front_door",
        "alarm_control_panel.house",
        "camera.porch",
        "device_tracker.phone",
        "person.someone",
        "image.doorbell",
    ],
)
async def test_off_limits_domains_are_refused_without_a_query(
    recorder_mock, core: HomeAssistant, monkeypatch, entity_id: str
) -> None:
    """The union, not just HIDDEN_DOMAINS: an alarm panel's history is when the house was armed."""
    core.states.async_set(entity_id, "on")
    _expose(core, entity_id)
    await async_wait_recording_done(core)
    monkeypatch.setattr(history, "_read", lambda *a, **k: pytest.fail("the database was queried"))

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.history", {"entity_id": entity_id, "hours": 24})
    assert ei.value.code == "not_found"


async def test_unexposed_and_unknown_are_refused_the_same_way(recorder_mock, core: HomeAssistant) -> None:
    """Identical messages, so the refusal is not a way to find out which entities exist."""
    core.states.async_set(SENSOR, "20.0")
    await async_wait_recording_done(core)

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as unexposed:
        await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 24})
    with pytest.raises(RpcError) as unknown:
        await d.dispatch("states.history", {"entity_id": "sensor.no_such_thing", "hours": 24})
    assert unexposed.value.code == unknown.value.code == "not_found"
    assert unexposed.value.message.split()[:-1] == unknown.value.message.split()[:-1]


async def test_an_external_statistic_is_not_an_entity(recorder_mock, core: HomeAssistant) -> None:
    """External statistic ids contain ':' and belong to no entity, so they never reach a query."""
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.history", {"entity_id": "tibber:energy", "hours": 24})
    assert ei.value.code == "not_found"


async def test_a_window_that_recorded_nothing_explains_itself(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["20.0"])

    d = build_dispatcher(core)
    long_ago = (datetime.now(UTC) - timedelta(days=300)).isoformat()
    res = await d.dispatch(
        "states.history",
        {"entity_id": SENSOR, "start": long_ago, "end": (datetime.now(UTC) - timedelta(days=299)).isoformat()},
    )
    assert res["points"] == []
    assert res["note"]
    assert res["keep_days"] is not None  # says why a 300-day-old window is empty


async def test_bad_params_are_refused(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["20.0"])
    d = build_dispatcher(core)

    for params, expected in [
        ({"entity_id": SENSOR, "hours": 0}, "hours"),
        ({"entity_id": SENSOR, "hours": 9000}, "hours"),
        ({"entity_id": SENSOR, "hours": True}, "hours"),
        ({"entity_id": SENSOR, "limit": 0}, "limit"),
        ({"entity_id": SENSOR, "limit": 5000}, "limit"),
        ({"entity_id": SENSOR, "resolution": "yearly"}, "resolution"),
        ({"entity_id": SENSOR, "start": "not a date"}, "start"),
        ({"entity_id": SENSOR, "hours": 1, "start": datetime.now(UTC).isoformat()}, "either"),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("states.history", params)
        assert ei.value.code == "invalid_params"
        assert expected in ei.value.message

    # A window wider than the ceiling, given as dates rather than hours.
    with pytest.raises(RpcError) as ei:
        await d.dispatch(
            "states.history",
            {
                "entity_id": SENSOR,
                "start": (datetime.now(UTC) - timedelta(days=400)).isoformat(),
                "end": datetime.now(UTC).isoformat(),
            },
        )
    assert ei.value.code == "invalid_params" and "366 days" in ei.value.message


async def test_the_answer_stays_far_under_the_frame_cap(recorder_mock, core: HomeAssistant) -> None:
    """The 1 MB frame is a protocol violation that closes the socket, so this is proven, not reasoned."""
    _expose(core, SENSOR)
    await _record(core, SENSOR, [f"{'y' * 250}{n}" for n in range(60)])

    d = build_dispatcher(core)
    res = await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 1, "limit": 50})
    assert len(json.dumps(res)) < history.MAX_BYTES
    assert res["truncated"] is True


async def test_history_is_refused_when_the_capability_is_off(recorder_mock, core: HomeAssistant) -> None:
    _expose(core, SENSOR)
    await _record(core, SENSOR, ["20.0"])

    d = build_dispatcher(core, frozenset({"entities.read"}))
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 24})
    assert ei.value.code == "method_not_allowed"


async def test_a_home_that_records_nothing_says_so(core: HomeAssistant) -> None:
    """No `recorder_mock`: recording is optional in Home Assistant, and this must be an answer."""
    _expose(core, SENSOR)
    core.states.async_set(SENSOR, "20.0")
    await core.async_block_till_done()

    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.history", {"entity_id": SENSOR, "hours": 24})
    assert ei.value.code == "ha_error"
    assert "not recording history" in ei.value.message
