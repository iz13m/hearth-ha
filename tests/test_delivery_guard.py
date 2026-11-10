"""The delivery guard (#812): no companion-app command in a notification during a run Hearth started.

Run on the pinned Home Assistant with the real `script`, `automation` and notify `group` integrations,
so what is proven is how *their* steps call services, not how a mock would. The `core` fixture installs
the guard, as `async_setup_entry` does; the tests that need it gone remove it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant, ServiceRegistry, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.hearth_ai import delivery
from custom_components.hearth_ai.const import DOMAIN
from custom_components.hearth_ai.delivery import DELIVERY_RULE, EVENT_REFUSED, delivery_problems, hearth_context, started_by_hearth
from custom_components.hearth_ai.handlers.inputs import NOTIFY_STEP_RULE
from custom_components.hearth_ai.policy import NOTIFY_COMMAND_RULE
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

from .test_conversation import ENTRY_DATA

PHONE = "mobile_app_alices_phone"
# The household's own scripts. `notify_me` is the issue's example; the others are the shapes the issue
# lists: filters, two inputs joined, a phone command in `data`, and a relay one script further away.
HOUSEHOLD = f"""
notify_me:
  alias: Notify me
  sequence:
  - action: notify.{PHONE}
    data:
      message: "{{{{ message }}}}"
tidy:
  alias: Tidy
  sequence:
  - action: notify.{PHONE}
    data:
      message: "{{{{ message | trim | lower }}}}"
joined:
  alias: Joined
  sequence:
  - action: notify.{PHONE}
    data:
      message: "{{{{ a }}}}{{{{ b }}}}"
open_app:
  alias: Open app
  sequence:
  - action: notify.{PHONE}
    data:
      message: Look
      data:
        command: "{{{{ cmd }}}}"
ios:
  alias: iOS
  sequence:
  - action: notify.{PHONE}
    data:
      message: "{{{{ message }}}}"
      title: "{{{{ title }}}}"
relay:
  alias: Relay
  sequence:
  - action: script.notify_me
    data:
      message: "{{{{ message }}}}"
family:
  alias: Family
  sequence:
  - action: notify.family
    data:
      message: "{{{{ message }}}}"
pick:
  alias: Pick
  sequence:
  - action: notify.{PHONE}
    target: "{{{{ t }}}}"
    data:
      message: Hello
scrub:
  alias: Scrub
  sequence:
  - action: notify.{PHONE}
    data:
      message: "{{{{ message | replace('x', '') }}}}"
relay_join:
  alias: Relay join
  sequence:
  - action: script.notify_me
    data:
      message: "{{{{ a }}}}{{{{ b }}}}"
assigned:
  alias: Assigned
  sequence:
  - variables:
      m: "{{{{ a ~ b }}}}"
  - action: notify.{PHONE}
    data:
      message: "{{{{ m }}}}"
door:
  alias: Door
  sequence:
  - action: notify.{PHONE}
    data:
      message: "Front door: {{{{ message }}}}"
dim:
  alias: Dim
  sequence:
  - action: input_boolean.turn_on
    target:
      entity_id: input_boolean.test
  - delay: "{{{{ level }}}}"
"""


def _caller(key: str, target: str, inputs: dict[str, Any]) -> str:
    """A script a model could have written (before #812 refused it): start `target` with `inputs`."""
    lines = "".join(f"      {k}: {v!r}\n" for k, v in inputs.items())
    return f"{key}:\n  alias: {key}\n  sequence:\n  - action: {target}\n    data:\n{lines}"


@pytest.fixture
def background_errors(core: HomeAssistant) -> Iterator[list[BaseException]]:
    """What a script started by `script.turn_on` raised. It runs as its own task, so Home Assistant logs
    "Task exception was never retrieved" for any script that fails that way: that is the run stopping."""
    seen: list[BaseException] = []
    previous = core.loop.get_exception_handler()
    core.loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx.get("exception")))
    yield seen
    core.loop.set_exception_handler(previous)


@pytest.fixture
async def phone(core: HomeAssistant) -> list[Any]:
    sent = async_mock_service(core, "notify", PHONE)
    # A legacy notify group, which forwards to the phone with no context of its own.
    assert await async_setup_component(core, "notify", {"notify": [{"platform": "group", "name": "Family", "services": [{"action": PHONE}]}]})
    await core.async_block_till_done()
    return sent


def _refusals(hass: HomeAssistant) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    @callback
    def _on(event: Any) -> None:
        seen.append(dict(event.data))

    hass.bus.async_listen(EVENT_REFUSED, _on)
    return seen


async def _scripts(core: HomeAssistant, tmp_path: Path, extra: str = "") -> None:
    (tmp_path / "scripts.yaml").write_text(HOUSEHOLD + extra)
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    for state in core.states.async_all("script"):
        async_expose_entity(core, "conversation", state.entity_id, True)


async def _hearth_run(core: HomeAssistant, script: str, inputs: dict[str, Any]) -> ServiceValidationError | None:
    """Start a household script the way a Hearth run reaches it: under a Hearth context, waiting."""
    try:
        await core.services.async_call("script", script, inputs, blocking=True, context=hearth_context())
    except ServiceValidationError as err:
        return err
    finally:
        await core.async_block_till_done()
    return None


async def _owner_automation(core: HomeAssistant, tmp_path: Path, script: str, inputs: dict[str, Any], context: Context | None = None) -> None:
    """The owner's own automation, started by an event."""
    data = "".join(f"      {k}: {v!r}\n" for k, v in inputs.items())
    (tmp_path / "automations.yaml").write_text(
        "- id: '1'\n  alias: Owner\n  triggers:\n  - trigger: event\n    event_type: owner_go\n"
        f"  actions:\n  - action: script.{script}\n    data:\n{data}"
    )
    await core.services.async_call("automation", "reload", blocking=True)
    await core.async_block_till_done()
    core.bus.async_fire("owner_go", context=context)
    await core.async_block_till_done()


async def test_a_hearth_run_with_a_command_reaches_no_phone(core: HomeAssistant, tmp_path: Path, phone: list[Any]) -> None:
    """Step 1 of #812: the household's "Notify me" with `command_webview`, under a Hearth context."""
    refused = _refusals(core)
    await _scripts(core, tmp_path)

    err = await _hearth_run(core, "notify_me", {"message": "command_webview"})
    assert isinstance(err, ServiceValidationError)
    assert phone == []
    # The refusal names the field, never the value: the value is the command.
    assert str(err) == f"notify.{PHONE}: message: {DELIVERY_RULE}"
    assert refused == [{"action": f"notify.{PHONE}", "fields": ["message"]}]

    # The same message from the owner's untagged automation is still sent.
    await _owner_automation(core, tmp_path, "notify_me", {"message": "command_webview"})
    assert [c.data["message"] for c in phone] == ["command_webview"]


async def test_without_the_guard_the_phone_obeys(core: HomeAssistant, tmp_path: Path, phone: list[Any]) -> None:
    """The gap #812 closes, on the same run: nothing installed, the command is delivered."""
    delivery.async_remove(core)
    await _scripts(core, tmp_path)
    assert await _hearth_run(core, "notify_me", {"message": "command_webview"}) is None
    assert [c.data["message"] for c in phone] == ["command_webview"]
    assert delivery.async_install(core)  # for the fixture's teardown


@pytest.mark.parametrize(
    ("script", "inputs"),
    [
        ("notify_me", {"message": "request_location_update"}),
        # `| trim` and `| lower`: the household's filters make the command; the guard reads the result.
        ("tidy", {"message": "  COMMAND_WEBVIEW "}),
        # Two inputs joined: neither alone is a command.
        ("joined", {"a": "comm", "b": "and_webview"}),
        # A phone command in `data`, filled from an input.
        ("open_app", {"cmd": "com.example.app"}),
        # The iOS words, in the message and in the title.
        ("ios", {"message": "update_complications", "title": "Hi"}),
        ("ios", {"message": "Hi", "title": "update_widgets"}),
        # A nested script: the relay starts `notify_me` under the same context.
        ("relay", {"message": "command_screen_on"}),
        # A notify group, which forwards to the phone with no context: its own call is the one read.
        ("family", {"message": "clear_notification"}),
        # R4's exact command, `TTS`: the phone speaks the title aloud through do-not-disturb.
        ("ios", {"message": "TTS", "title": "Wake up"}),
    ],
)
async def test_every_shape_the_issue_lists_is_refused_on_delivery(
    core: HomeAssistant, tmp_path: Path, phone: list[Any], script: str, inputs: dict[str, Any]
) -> None:
    await _scripts(core, tmp_path)
    err = await _hearth_run(core, script, inputs)
    assert isinstance(err, ServiceValidationError), (script, inputs)
    assert phone == []
    # Names and the rule only, never the value.
    assert str(err).endswith(f": {DELIVERY_RULE}") and not any(v.strip() in str(err).removesuffix(DELIVERY_RULE) for v in inputs.values())


@pytest.mark.parametrize(
    ("script", "inputs"),
    [
        ("notify_me", {"message": "Dinner is ready"}),
        ("tidy", {"message": "  Dinner "}),
        ("joined", {"a": "Din", "b": "ner"}),
        ("ios", {"message": "Run command_x later", "title": "Hi"}),
        ("relay", {"message": "Door open"}),
        ("family", {"message": "Door open"}),
    ],
)
async def test_the_controls_still_reach_the_phone(core: HomeAssistant, tmp_path: Path, phone: list[Any], script: str, inputs: dict[str, Any]) -> None:
    await _scripts(core, tmp_path)
    assert await _hearth_run(core, script, inputs) is None
    assert len(phone) == 1


async def test_an_owner_automation_still_sends_every_shape(core: HomeAssistant, tmp_path: Path, phone: list[Any]) -> None:
    """Untagged, nothing is read: the owner's own command words, through a relay and a group, still go."""
    await _scripts(core, tmp_path)
    await _owner_automation(core, tmp_path, "relay", {"message": "command_screen_on"})
    await _owner_automation(core, tmp_path, "family", {"message": "update_complications"})
    await _owner_automation(core, tmp_path, "open_app", {"cmd": "com.example.app"})
    assert [c.data["message"] for c in phone] == ["command_screen_on", "update_complications", "Look"]


async def test_an_automation_a_hearth_call_triggered_is_read_one_level_up(
    core: HomeAssistant, tmp_path: Path, phone: list[Any], background_errors: list[BaseException]
) -> None:
    """An automation started by an event under a Hearth context runs with `parent_id` set to it."""
    refused = _refusals(core)
    await _scripts(core, tmp_path)
    await _owner_automation(core, tmp_path, "notify_me", {"message": "command_webview"}, context=hearth_context())
    assert phone == [] and len(refused) == 1
    # Two levels up is not read: a context whose parent is an ordinary one.
    grandchild = Context(parent_id=Context(parent_id=hearth_context().id).id)
    assert not started_by_hearth(grandchild)


async def test_scripts_run_of_a_script_that_fills_in_the_household_one(
    core: HomeAssistant, tmp_path: Path, phone: list[Any], background_errors: list[BaseException]
) -> None:
    """The real path: `scripts.run` (no inputs since #787) of a script that starts "Notify me" with one.

    Written straight to the file, as one authored before #812 would be. `script.turn_on` does not wait,
    so the refusal stops the run in the background rather than failing the RPC.
    """
    refused = _refusals(core)
    await _scripts(core, tmp_path, _caller("ping", "script.notify_me", {"message": "command_webview"}) + _caller("polite", "script.notify_me", {"message": "Dinner"}))
    d = build_dispatcher(core, frozenset({"routines.run"}))

    await d.dispatch("scripts.run", {"entity_id": "script.ping"})
    await core.async_block_till_done()
    assert phone == [] and len(refused) == 1
    assert [type(e) for e in background_errors] == [ServiceValidationError]

    await d.dispatch("scripts.run", {"entity_id": "script.polite"})
    await core.async_block_till_done()
    assert [c.data["message"] for c in phone] == ["Dinner"]


async def test_hearth_calls_carry_a_context_the_guard_recognises(core: HomeAssistant, tmp_path: Path, phone: list[Any]) -> None:
    """`notify.send`, `devices.call` and `scripts.run` each start their call with a Hearth context."""
    d = build_dispatcher(core, frozenset({"notify.send", "devices.control", "routines.run"}))
    await d.dispatch("notify.send", {"target": f"notify.{PHONE}", "message": "Hello"})
    assert started_by_hearth(phone[-1].context)

    toggled = async_mock_service(core, "input_boolean", "toggle")
    await d.dispatch("devices.call", {"domain": "input_boolean", "service": "toggle", "entity_id": ["input_boolean.test"]})
    assert started_by_hearth(toggled[-1].context)

    await _scripts(core, tmp_path)
    await d.dispatch("scripts.run", {"entity_id": "script.notify_me"})
    await core.async_block_till_done()
    assert started_by_hearth(phone[-1].context)
    # An ordinary context is not one.
    assert not started_by_hearth(Context()) and not started_by_hearth(None)


@pytest.mark.parametrize(
    ("data", "fields"),
    [
        ({"message": "hi", "data": {"command": "x"}}, ["data.command"]),
        ({"message": "hi", "data": {"url": "/lovelace/0"}}, ["data.url"]),
        ({"message": "hi", "data": {"clickAction": "https://x"}}, ["data.clickAction"]),
        ({"message": "hi", "data": {"actions": [{"action": "A", "title": "Open"}, {"action": "URI", "title": "Go", "uri": "app://x"}]}}, ["data.actions[1].uri"]),
        ({"message": "hi", "data": {"push": {"sound": {"name": "default", "critical": 1}}}}, ["data.push"]),
        ({"message": "hi", "data": {"push": {"interruption-level": "critical"}}}, ["data.push"]),
        ({"message": "clear_notification", "data": {"tag": "door"}}, ["message"]),
        ({"message": " Update_Widgets", "title": "x"}, ["message"]),
        # R4's exact command (`NOTIFY_EXACT_COMMANDS`): the whole message, stripped, in any case.
        ({"message": " tts\x1f", "title": "Wake up"}, ["message"]),
        ({"message": "TTS is ready"}, []),
        ({"message": "hi", "title": "command_dnd"}, ["title"]),
        ({"message": "hi", "data": "not a mapping"}, ["data"]),
        # Controls.
        ({"message": "hi", "title": "Door", "data": {"tag": "door", "channel": "Alarm", "push": {"badge": 1}}}, []),
        ({"message": "Run command_dnd later", "data": {"actions": [{"action": "A", "title": "OK"}]}}, []),
    ],
)
def test_delivery_problems(data: dict[str, Any], fields: list[str]) -> None:
    assert delivery_problems(data) == fields


async def test_the_guard_is_installed_by_setup_and_removed_on_unload(hass: HomeAssistant) -> None:
    assert type(hass.services) is ServiceRegistry
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert delivery.active(hass)
    with patch("custom_components.hearth_ai.HearthClient.stop"):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert type(hass.services) is ServiceRegistry and not delivery.active(hass)


async def test_install_fails_closed_on_a_registry_it_does_not_know(hass: HomeAssistant) -> None:
    class Other(ServiceRegistry):
        __slots__ = ()

    hass.services.__class__ = Other
    try:
        assert delivery.async_install(hass) is False
        assert not delivery.active(hass)
    finally:
        hass.services.__class__ = ServiceRegistry


async def _validate_automation(core: HomeAssistant, *actions: dict[str, Any]) -> dict[str, Any]:
    return await build_dispatcher(core).dispatch(
        "automations.validate",
        {"config": {"alias": "x", "triggers": [{"trigger": "sun", "event": "sunset"}], "actions": list(actions)}},
    )


@pytest.mark.parametrize(
    "action",
    [
        # The input prefix, by both ways of starting a script.
        {"action": "script.notify_me", "data": {"message": "command_webview"}},
        {"action": "script.turn_on", "target": {"entity_id": "script.notify_me"}, "data": {"variables": {"message": "  Request_Location_Update"}}},
        # An iOS word, and one nested inside a value.
        {"action": "script.ios", "data": {"message": "hi", "title": "update_complications"}},
        {"action": "script.notify_me", "data": {"message": "hi", "extra": {"deep": ["remove_channel"]}}},
        # A template whose literal lead could still become a command.
        {"action": "script.notify_me", "data": {"message": "comm{{ 'and_webview' }}"}},
        # A phone command in `data`, from an input: the guarded field, whatever the input says.
        {"action": "script.open_app", "data": {"cmd": "com.example.app"}},
    ],
)
async def test_an_input_that_could_be_a_command_is_refused_when_written(core: HomeAssistant, tmp_path: Path, action: dict[str, Any]) -> None:
    """#777 amended: every input value through R4's matcher, and a notify `data` key from an input guarded."""
    await _scripts(core, tmp_path)
    res = await _validate_automation(core, action)
    assert res["ok"] is False and res["status"] == "policy", res
    assert "companion-app command" in res["error"] or "started with inputs" in res["error"], res


async def test_notify_data_from_an_input_is_guarded_and_the_words_are_not(core: HomeAssistant, tmp_path: Path) -> None:
    await _scripts(core, tmp_path)
    res = await _validate_automation(core, {"action": "script.open_app", "data": {"cmd": "x"}})
    assert "sequence[0].data.data is decided when script.open_app runs" in res["error"], res
    # Controls: message and title from inputs stay allowed, with words.
    for action in (
        {"action": "script.notify_me", "data": {"message": "Dinner is ready"}},
        {"action": "script.ios", "data": {"message": "Front door: {{ states('binary_sensor.door') }}", "title": "Door"}},
        {"action": "script.relay", "data": {"message": "Run command_dnd later"}},
    ):
        assert (await _validate_automation(core, action))["ok"] is True, action


async def test_a_mobile_app_device_action_fills_only_its_words_from_inputs(core: HomeAssistant, tmp_path: Path) -> None:
    from custom_components.hearth_ai.handlers.inputs import find_input_reachable_violations

    def walk(node: dict[str, Any]) -> list[str]:
        return find_input_reachable_violations({"sequence": [node]}, "s", "script.x")

    base = {"device_id": "phone", "domain": "mobile_app", "type": "notify"}
    assert walk({**base, "message": "{{ m }}", "title": "{{ t }}"}) == []
    assert walk({**base, "message": "hi", "data": {"command": "{{ c }}"}})


async def test_without_the_guard_a_notifying_script_is_not_started_with_inputs(core: HomeAssistant, tmp_path: Path, phone: list[Any]) -> None:
    """Fail closed: when written, and when run (a script written while the guard stood still exists)."""
    await _scripts(core, tmp_path, _caller("ping", "script.notify_me", {"message": "Dinner"}))
    d = build_dispatcher(core, frozenset({"routines.run", "devices.control"}))
    core.states.async_set("group.evening", "off", {"entity_id": ["script.ping"]})
    async_expose_entity(core, "conversation", "group.evening", True)
    allowed = {"action": "script.notify_me", "data": {"message": "Dinner is ready"}}
    assert (await _validate_automation(core, allowed))["ok"] is True

    delivery.async_remove(core)
    try:
        res = await _validate_automation(core, allowed)
        assert res["ok"] is False and NOTIFY_STEP_RULE in res["error"], res
        # A script that starts no notifying script with inputs is unchanged.
        assert (await _validate_automation(core, {"action": "script.notify_me"}))["ok"] is True
        for method, params in (
            ("scripts.run", {"entity_id": "script.ping"}),
            # A group holding it, as `member_violations` asks for every script a call fans out to.
            ("devices.call", {"domain": "homeassistant", "service": "turn_on", "entity_id": ["group.evening"]}),
        ):
            with pytest.raises(RpcError) as ei:
                await d.dispatch(method, params)
            assert ei.value.code == "method_not_allowed" and NOTIFY_STEP_RULE in ei.value.message
        await core.async_block_till_done()
        assert phone == []
        # Control: running the notifying script itself, with no inputs, is the household's own call.
        await d.dispatch("scripts.run", {"entity_id": "script.notify_me"})
    finally:
        assert delivery.async_install(core)

    # With the guard back, the same run goes ahead.
    await d.dispatch("scripts.run", {"entity_id": "script.ping"})
    await core.async_block_till_done()
    assert [c.data["message"] for c in phone][-1] == "Dinner"


def test_the_rule_names_every_command_word() -> None:
    for word in ("command_", "request_location_update", "remove_channel", "clear_notification", "update_complications", "update_widgets"):
        assert word in NOTIFY_COMMAND_RULE


async def test_the_guard_reads_a_target_merged_over_the_data(
    core: HomeAssistant, tmp_path: Path, phone: list[Any], background_errors: list[BaseException]
) -> None:
    """Home Assistant merges `target` into the service data after the guard's check (both reviews)."""
    # Measured first: untagged, a `target` holding a message replaces the data's.
    await core.services.async_call("notify", PHONE, {"message": "hi"}, blocking=True, target={"message": "command_webview"})
    assert [c.data["message"] for c in phone] == ["command_webview"]
    phone.clear()

    with pytest.raises(ServiceValidationError) as ei:
        await core.services.async_call("notify", PHONE, {"message": "hi"}, blocking=True, context=hearth_context(), target={"message": "command_webview"})
    assert str(ei.value) == f"notify.{PHONE}: message: {DELIVERY_RULE}"
    with pytest.raises(ServiceValidationError):
        await core.services.async_call("notify", PHONE, {"message": "hi"}, blocking=True, context=hearth_context(), target={"data": {"command": "x"}})
    assert phone == []

    # A household script with a templated target, reached through `scripts.run`.
    await _scripts(core, tmp_path, _caller("ping", "script.pick", {"t": {"message": "command_webview"}}) + _caller("polite", "script.pick", {"t": {}}))
    d = build_dispatcher(core, frozenset({"routines.run"}))
    await d.dispatch("scripts.run", {"entity_id": "script.ping"})
    await core.async_block_till_done()
    assert phone == [] and [type(e) for e in background_errors] == [ServiceValidationError]
    await d.dispatch("scripts.run", {"entity_id": "script.polite"})
    await core.async_block_till_done()
    assert [c.data["message"] for c in phone] == ["Hello"]


@pytest.mark.parametrize(
    "action",
    [
        # Security finding 1: harmless halves joined by the household's template, under no Hearth context.
        {"action": "script.joined", "data": {"a": "comm", "b": "and_webview"}},
        # A filter the matcher does not model.
        {"action": "script.scrub", "data": {"message": "commaxnd_webview"}},
        # One hop further: a household relay that joins what it was given before passing it on.
        {"action": "script.relay_join", "data": {"a": "comm", "b": "and_webview"}},
        # A name the script assigns itself is not an input placed as it came.
        {"action": "script.assigned", "data": {"a": "comm", "b": "and_webview"}},
        {"action": "script.turn_on", "target": {"entity_id": "script.joined"}, "data": {"variables": {"a": "x", "b": "y"}}},
    ],
)
async def test_words_built_from_inputs_are_refused_when_written(core: HomeAssistant, tmp_path: Path, action: dict[str, Any]) -> None:
    await _scripts(core, tmp_path)
    res = await _validate_automation(core, action)
    assert res["ok"] is False and res["status"] == "policy", res
    assert "bare {{ input }}" in res["error"] or "could become a companion-app command" in res["error"], res


async def test_the_joined_case_through_an_untagged_automation_is_refused_before_it_can_run(
    core: HomeAssistant, tmp_path: Path, phone: list[Any]
) -> None:
    """The finding end to end: the guard does not look at an automation's own context, so the authoring
    walk must stop the model's script S, which a timer automation would otherwise run."""
    await _scripts(core, tmp_path)
    d = build_dispatcher(core)
    res = await d.dispatch("scripts.validate", {"key": "s", "config": {"sequence": [{"action": "script.joined", "data": {"a": "comm", "b": "and_webview"}}]}})
    assert res["ok"] is False, res
    # Why it must: had S been written, an untagged run delivers it past the guard.
    await _owner_automation(core, tmp_path, "joined", {"a": "comm", "b": "and_webview"})
    assert [c.data["message"] for c in phone] == ["command_webview"]


@pytest.mark.parametrize(
    "action",
    [
        {"action": "script.notify_me", "data": {"message": "Dinner is ready"}},
        {"action": "script.tidy", "data": {"message": "  Dinner "}},
        {"action": "script.door", "data": {"message": "Alice"}},
        {"action": "script.relay", "data": {"message": "Door open"}},
        # A script that never notifies takes any value, a template or a command-like word.
        {"action": "script.dim", "data": {"level": "{{ states('input_number.level') }}"}},
        {"action": "script.dim", "data": {"level": "command_x"}},
    ],
)
async def test_inputs_a_notification_cannot_turn_into_a_command_are_allowed(core: HomeAssistant, tmp_path: Path, action: dict[str, Any]) -> None:
    await _scripts(core, tmp_path)
    res = await _validate_automation(core, action)
    assert res["ok"] is True, (action, res)


def test_an_unreadable_script_stays_strict_and_says_why() -> None:
    from custom_components.hearth_ai.handlers.inputs import UNREADABLE, find_input_value_violations

    config = {"actions": [{"action": "script.gone", "data": {"message": "command_x"}}, {"action": "script.quiet", "data": {"message": "command_x"}}]}
    verdicts = {"script.gone": UNREADABLE, "script.quiet": None}
    found = find_input_value_violations(config, "config", verdicts.get)
    assert len(found) == 1 and "what script.gone does cannot be read" in found[0]
    assert "command_x" not in found[0].split("; ")[0]


async def test_a_script_home_assistant_does_not_have_is_matched_strictly(core: HomeAssistant, tmp_path: Path) -> None:
    """Not only refused as unreadable: its inputs are read as if it notified, and the text says why."""
    await _scripts(core, tmp_path)
    res = await _validate_automation(core, {"action": "script.gone", "data": {"message": "command_x"}})
    assert res["ok"] is False and "what script.gone does cannot be read" in res["error"], res
    # Control: a readable script that never notifies is not asked.
    assert (await _validate_automation(core, {"action": "script.dim", "data": {"level": "command_x"}}))["ok"] is True


def test_ids_are_unique_within_a_millisecond_and_all_recognised() -> None:
    ids = [hearth_context().id for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    assert all(delivery.is_hearth_id(i) for i in ids)


async def test_install_declines_when_home_assistant_changes_the_call(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    async def changed(self, domain, service, service_data=None, blocking=False, context=None, target=None, return_response=False, extra=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(ServiceRegistry, "async_call", changed)
    assert delivery.async_install(hass) is False
    assert not delivery.active(hass)


async def _entry(hass: HomeAssistant, unique_id: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=unique_id, data={**ENTRY_DATA, "installation_id": unique_id})
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_two_entries_and_an_options_reload(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    first = await _entry(hass, "inst-1")
    second = await _entry(hass, "inst-2")
    assert delivery.active(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"), patch("custom_components.hearth_ai.HearthClient.stop"):
        # An options reload unloads and sets up again: the guard is there afterwards.
        assert await hass.config_entries.async_reload(first.entry_id)
        await hass.async_block_till_done()
        assert delivery.active(hass)
        assert await hass.config_entries.async_unload(first.entry_id)
        assert delivery.active(hass)
        assert await hass.config_entries.async_unload(second.entry_id)
    assert type(hass.services) is ServiceRegistry and not delivery.active(hass)
