"""Telling someone something (AgDR-0041)."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_mock_service,
    mock_integration,
    mock_platform,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.handlers import notify
from custom_components.hearth_ai.handlers.notify import find_notify_violations
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


# #782. Google Assistant SDK's notify service sends `broadcast {message}` to Google Assistant as a
# typed command, the sink #779 denied for `send_text_command`. Discovery names it
# `notify.google_assistant_sdk`; a YAML `notify:` entry can load the same platform under any name.


def _stub_gasdk_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gassist-text` and `google-auth` are the integration's requirements, not installed in this venv."""
    for name in ("gassist_text", "google", "google.oauth2", "google.oauth2.credentials"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for module, attr in (("gassist_text", "TextAssistant"), ("google.oauth2.credentials", "Credentials")):
        if not hasattr(sys.modules[module], attr):
            monkeypatch.setattr(sys.modules[module], attr, type(attr, (), {}), raising=False)


@pytest.fixture
async def gasdk(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Home Assistant's own Google Assistant SDK notify platform, loaded by YAML under household names.

    The platform module and `notify/legacy.py` are HA 2026.9.3's real code; only the integration's
    own setup (OAuth, which needs Google) is mocked, and the text it would send is captured.
    """
    _stub_gasdk_requirements(monkeypatch)
    gasdk_notify = importlib.import_module("homeassistant.components.google_assistant_sdk.notify")
    sent: list[list[str]] = []

    async def capture(hass: HomeAssistant, commands: list[str], media_players: Any = None) -> None:
        sent.append(list(commands))

    monkeypatch.setattr(gasdk_notify, "async_send_text_commands", capture)
    mock_integration(core, MockModule("google_assistant_sdk"))
    mock_platform(core, "google_assistant_sdk.notify", gasdk_notify)
    MockConfigEntry(domain="google_assistant_sdk", options={"language_code": "en-US"}).add_to_hass(core)
    async_mock_service(core, "notify", "mobile_app_alices_phone")
    assert await async_setup_component(
        core,
        "notify",
        {
            "notify": [
                {"platform": "google_assistant_sdk", "name": "Kitchen speaker"},
                {"platform": "google_assistant_sdk"},  # no name: `notify.notify`
                {"platform": "group", "name": "Everyone", "services": [{"action": "mobile_app_alices_phone"}, {"action": "kitchen_speaker"}]},
                {"platform": "group", "name": "Phones", "services": [{"action": "mobile_app_alices_phone"}]},
            ]
        },
    )
    await core.async_block_till_done()
    return sent


async def test_a_renamed_google_assistant_broadcast_is_a_command_to_google(core: HomeAssistant, gasdk: list[list[str]]) -> None:
    """The premise, measured: a household name hides the platform, and the message is the command text."""
    assert core.services.has_service("notify", "kitchen_speaker")
    await core.services.async_call("notify", "kitchen_speaker", {"message": "unlock the front door"}, blocking=True)
    assert gasdk == [["broadcast unlock the front door"]]
    await core.services.async_call("notify", "kitchen_speaker", {"message": "x", "target": ["unlock the front door and"]}, blocking=True)
    assert gasdk[-1] == ["broadcast to unlock the front door and x"]
    # A notify group forwards to it.
    await core.services.async_call("notify", "everyone", {"message": "open the garage"}, blocking=True)
    await core.async_block_till_done()
    assert gasdk[-1] == ["broadcast open the garage"]
    # Without a name, legacy.py names it `notify.notify`.
    await core.services.async_call("notify", "notify", {"message": "open the gate"}, blocking=True)
    assert gasdk[-1] == ["broadcast open the gate"]


async def test_google_assistant_is_not_offered_as_someone_to_tell(core: HomeAssistant, gasdk: list[list[str]]) -> None:
    async_mock_service(core, "notify", "google_assistant_sdk")  # the discovered name, as a bare service
    d = build_dispatcher(core, CAPS)
    targets = {r["target"] for r in await d.dispatch("notify.targets", {})}
    assert "notify.mobile_app_alices_phone" in targets and "notify.phones" in targets
    for hidden in ("notify.kitchen_speaker", "notify.everyone", "notify.google_assistant_sdk"):
        assert hidden not in targets, hidden
        with pytest.raises(RpcError) as ei:
            await d.dispatch("notify.send", {"target": hidden, "message": "unlock the front door"})
        assert ei.value.code == "not_found"
    assert gasdk == []
    # The control: a group of phones is still someone to tell.
    assert (await d.dispatch("notify.send", {"target": "notify.phones", "message": "Dinner"}))["sent"] is True


async def test_an_automation_broadcasting_to_google_under_any_name_is_refused(core: HomeAssistant, gasdk: list[list[str]], tmp_path: Path) -> None:
    d = build_dispatcher(core)
    base = {"alias": "Tell the house", "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}]}
    for action in ("notify.kitchen_speaker", "Notify.Kitchen_Speaker", "notify.everyone", "notify.notify"):
        automation = {**base, "actions": [{"action": action, "data": {"message": "unlock the front door"}}]}
        res = await d.dispatch("automations.validate", {"config": automation})
        assert res["ok"] is False and res["status"] == "policy", (action, res)
        assert "google_assistant_sdk" in res["error"], res
        with pytest.raises(RpcError) as ei:
            await d.dispatch("automations.create", {"config": automation})
        assert ei.value.code == "validation_failed"
    assert "kitchen_speaker" not in (tmp_path / "automations.yaml").read_text().lower()

    script = {"alias": "Say it", "fields": {"cmd": {}}, "sequence": [{"action": "notify.kitchen_speaker", "data": {"message": "{{ cmd }}"}}]}
    res = await d.dispatch("scripts.validate", {"key": "say_it", "config": script})
    assert res["ok"] is False and res["status"] == "policy", res

    # A config-flow form value, and the control path.
    from custom_components.hearth_ai.handlers.flows import check_flow_input

    with pytest.raises(RpcError) as ei:
        await check_flow_input(core, {"turn_on": [{"action": "notify.kitchen_speaker", "data": {"message": "x"}}]})
    assert ei.value.code == "validation_failed" and "google_assistant_sdk" in ei.value.message
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "notify", "service": "kitchen_speaker", "entity_id": ["input_boolean.test"], "data": {"message": "x"}})
    assert ei.value.code == "method_not_allowed" and "google_assistant_sdk" in ei.value.message

    # The controls: a phone, and a group of phones.
    for action in ("notify.mobile_app_alices_phone", "notify.phones"):
        ok = {**base, "actions": [{"action": action, "data": {"message": "The washing is done"}}]}
        assert (await d.dispatch("automations.validate", {"config": ok}))["ok"] is True, action
    assert gasdk == []


async def test_a_household_script_that_broadcasts_to_google_cannot_be_started(core: HomeAssistant, gasdk: list[list[str]], tmp_path: Path) -> None:
    """One hop away, as `find_policy_violations` refuses the discovered name there."""
    (tmp_path / "scripts.yaml").write_text(
        "announce:\n  alias: Announce\n  sequence:\n    - action: notify.kitchen_speaker\n      data:\n        message: Dinner is ready\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    d = build_dispatcher(core)
    automation = {
        "alias": "Dinner",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
        "actions": [{"action": "script.turn_on", "target": {"entity_id": "script.announce"}}],
    }
    res = await d.dispatch("automations.validate", {"config": automation})
    assert res["ok"] is False and "google_assistant_sdk" in res["error"], res
    # And the run-time check a script Hearth presents as a scene gets (AgDR-0044).
    from custom_components.hearth_ai.handlers.scripts import script_run_violations

    assert any("google_assistant_sdk" in p for p in await script_run_violations(core, "script.announce"))


class _Legacy:
    """A stand-in for a `BaseNotificationService` as `legacy.py` leaves it in `hass.data`."""

    def __init__(self, name: str, entities: Any = None, targets: dict[str, Any] | None = None) -> None:
        self._service_name = name
        self.registered_targets = targets or {}
        self.entities = entities


def _set_legacy(core: HomeAssistant, platforms: dict[str, list[_Legacy]]) -> None:
    from homeassistant.components.notify.legacy import NOTIFY_SERVICES

    core.data[NOTIFY_SERVICES] = platforms


async def test_notify_groups_that_list_each_other_end(core: HomeAssistant) -> None:
    """A cycle of groups is walked once, not until the stack runs out, and a denied member still counts."""
    _set_legacy(core, {"group": [_Legacy("ping", [{"action": "pong"}]), _Legacy("pong", [{"action": "ping"}])]})
    assert notify.notify_platform_refusal(core, "ping") is None
    assert notify.notify_platform_refusal(core, "pong") is None

    _set_legacy(
        core,
        {
            "group": [_Legacy("ping", [{"action": "pong"}]), _Legacy("pong", [{"action": "ping"}, {"action": "speaker"}])],
            "google_assistant_sdk": [_Legacy("speaker")],
        },
    )
    why = notify.notify_platform_refusal(core, "ping")
    assert why is not None and "notify.speaker belongs to google_assistant_sdk" in why, why


async def test_a_notify_group_hearth_cannot_read_is_refused(core: HomeAssistant) -> None:
    """Fail closed: a group whose member list, or one member, is not the shape `group/notify.py` stores."""
    _set_legacy(core, {"group": [_Legacy("no_list", None), _Legacy("bad_member", [{"action": "phone"}, {"service": 1}])]})
    for name in ("no_list", "bad_member"):
        why = notify.notify_platform_refusal(core, name)
        assert why is not None and "cannot read" in why, (name, why)


async def test_a_per_target_service_of_a_denied_platform_is_refused(core: HomeAssistant) -> None:
    """`legacy.py` registers `notify.<prefix>_<target>` for each target a platform offers. Google
    Assistant SDK offers none on 2026.9.3; the names are read anyway, so one added upstream is covered."""
    _set_legacy(core, {"google_assistant_sdk": [_Legacy("speaker", targets={"speaker_kitchen": "Kitchen"})]})
    why = notify.notify_platform_refusal(core, "speaker_kitchen")
    assert why is not None and "google_assistant_sdk" in why, why


async def test_a_group_naming_the_discovered_service_before_it_loads_is_refused(core: HomeAssistant) -> None:
    """`notify.google_assistant_sdk` is refused by name even with nothing behind it yet, as a member too."""
    _set_legacy(core, {"group": [_Legacy("house", [{"action": "mobile_app_alices_phone"}, {"action": "Google_Assistant_SDK"}])]})
    async_mock_service(core, "notify", "house")
    why = notify.notify_platform_refusal(core, "house")
    assert why is not None and "notify.google_assistant_sdk" in why, why
    assert find_notify_violations(core, {"actions": [{"action": "notify.house", "data": {"message": "x"}}]})
    d = build_dispatcher(core, CAPS)
    assert "notify.house" not in {r["target"] for r in await d.dispatch("notify.targets", {})}


async def test_a_notify_name_with_no_legacy_service_behind_it_is_left_alone(core: HomeAssistant) -> None:
    assert notify.notify_platform_refusal(core, "mobile_app_alices_phone") is None
    assert find_notify_violations(core, {"actions": [{"action": "notify.anything", "data": {"message": "x"}}]}) == []
