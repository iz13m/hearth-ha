"""Telling someone something (AgDR-0041)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.handlers import notify
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

CAPS = frozenset({"notify.send"})


async def test_targets_lists_both_machines_and_no_state(core: HomeAssistant) -> None:
    """The modern one is an entity, the older one a service, and a caller should not have to care."""
    async_mock_service(core, "notify", "mobile_app_alices_phone")
    async_mock_service(core, "notify", "persistent_notification")
    core.states.async_set("notify.bobs_phone", "2026-09-18T00:00:00+00:00", {"friendly_name": "Bob's phone"})
    d = build_dispatcher(core, CAPS)

    rows = await d.dispatch("notify.targets", {})
    by_target = {r["target"]: r for r in rows}
    assert by_target["notify.mobile_app_alices_phone"]["kind"] == "service"
    assert by_target["notify.bobs_phone"]["kind"] == "entity"
    assert by_target["notify.bobs_phone"]["name"] == "Bob's phone"
    # Not a person, so not offered as someone to tell.
    assert "notify.persistent_notification" not in by_target
    # A target's state is the time it last sent something — when someone was contacted — and a list
    # of who exists has no business carrying it.
    assert all(set(r) == {"target", "kind", "name", "available"} for r in rows)


async def test_sending_to_a_service_target(core: HomeAssistant) -> None:
    calls = async_mock_service(core, "notify", "mobile_app_alices_phone")
    d = build_dispatcher(core, CAPS)

    res = await d.dispatch(
        "notify.send",
        {"target": "notify.mobile_app_alices_phone", "message": "The washing is done", "title": "Hearth"},
    )
    assert res == {"target": "notify.mobile_app_alices_phone", "sent": True}
    assert len(calls) == 1
    assert calls[0].data == {"message": "The washing is done", "title": "Hearth"}


async def test_sending_to_an_entity_target(core: HomeAssistant) -> None:
    calls = async_mock_service(core, "notify", "send_message")
    core.states.async_set("notify.bobs_phone", "unknown", {"friendly_name": "Bob's phone"})
    d = build_dispatcher(core, CAPS)

    await d.dispatch("notify.send", {"target": "notify.bobs_phone", "message": "Back door left open"})
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "notify.bobs_phone"
    assert calls[0].data["message"] == "Back door left open"


async def test_an_unknown_target_is_refused_without_calling_anything(core: HomeAssistant) -> None:
    calls = async_mock_service(core, "notify", "send_message")
    d = build_dispatcher(core, CAPS)

    for target in ("notify.nobody", "light.hall", "notify", "shell_command.rm"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("notify.send", {"target": target, "message": "hello"})
        assert ei.value.code in ("not_found", "invalid_params")
    assert calls == []


async def test_a_run_of_notifications_is_stopped(core: HomeAssistant) -> None:
    """Ten in a minute is already more than anyone wants; a loop must not become a night of buzzing."""
    calls = async_mock_service(core, "notify", "mobile_app_alices_phone")
    d = build_dispatcher(core, CAPS)

    for i in range(notify.RATE_LIMIT):
        await d.dispatch("notify.send", {"target": "notify.mobile_app_alices_phone", "message": f"{i}"})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("notify.send", {"target": "notify.mobile_app_alices_phone", "message": "one more"})
    assert ei.value.code == "rate_limited"
    assert len(calls) == notify.RATE_LIMIT


async def test_an_empty_or_enormous_message_is_refused(core: HomeAssistant) -> None:
    async_mock_service(core, "notify", "mobile_app_alices_phone")
    d = build_dispatcher(core, CAPS)
    for message in ("", "   ", "x" * (notify.MAX_MESSAGE + 1)):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("notify.send", {"target": "notify.mobile_app_alices_phone", "message": message})
        assert ei.value.code == "invalid_params"


async def test_the_capability_is_required(core: HomeAssistant) -> None:
    d = build_dispatcher(core, frozenset({"entities.read", "devices.control"}))
    for method in ("notify.targets", "notify.send"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, {})
        assert ei.value.code == "method_not_allowed"
