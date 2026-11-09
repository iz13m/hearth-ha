"""Operating the home: devices.call, scenes.activate, scripts.run."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.hearth_ai.handlers import control, registry, scenes
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

TEST = "input_boolean.test"


def row_can_run(rows: list[dict], entity_id: str) -> bool:
    return next(r for r in rows if r["entity_id"] == entity_id)["can_run"]


async def test_capability_is_required(core: HomeAssistant) -> None:
    """Without devices.control the method is refused before any handler runs."""
    d = build_dispatcher(core, frozenset({"entities.read", "scripts.read"}))
    for method, params in [
        ("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]}),
        ("scenes.activate", {"entity_id": "scene.x"}),
        ("scripts.run", {"entity_id": "script.x"}),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed"
        assert "disabled" in ei.value.message


async def test_turns_a_device_on_and_reports_the_new_state(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    assert core.states.get(TEST).state == "off"
    result = await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    await core.async_block_till_done()
    assert result["called"] == "input_boolean.turn_on"
    assert result["entities"] == [{"entity_id": TEST, "state": "on"}]
    assert core.states.get(TEST).state == "on"


async def test_refuses_blocked_domains_entities_and_routines(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    for params in [
        {"domain": "lock", "service": "unlock", "entity_id": ["lock.front"]},
        {"domain": "input_boolean", "service": "turn_on", "entity_id": ["camera.door"]},
        {"domain": "homeassistant", "service": "restart", "entity_id": [TEST]},
        {"domain": "shell_command", "service": "rm", "entity_id": [TEST]},
        {"domain": "script", "service": "turn_on", "entity_id": ["script.x"]},
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("devices.call", params)
        assert ei.value.code == "method_not_allowed", params
    assert core.states.get(TEST).state == "off"


async def test_an_exposed_image_processing_entity_cannot_be_called(core: HomeAssistant) -> None:
    """
    The read path hides `image_processing` (#566), and `devices.call` answers with the state of every
    target it ran. So an entity that was already shared, or shared in HA's own Expose screen, handed the
    model a number plate back through `control_devices` until the call refused the id itself.
    """
    scans: list[str] = []

    async def scan(call: object) -> None:
        scans.append("scan")
        core.states.async_set("image_processing.driveway", "AB12CDE")

    core.services.async_register("image_processing", "scan", scan)
    core.states.async_set("image_processing.driveway", "unknown")
    async_expose_entity(core, "conversation", "image_processing.driveway", True)
    d = build_dispatcher(core)
    for domain, service in [("image_processing", "scan"), ("homeassistant", "update_entity")]:
        assert core.services.has_service(domain, service), f"{domain}.{service} is not registered, so a refusal proves nothing"
        with pytest.raises(RpcError) as ei:
            await d.dispatch("devices.call", {"domain": domain, "service": service, "entity_id": ["image_processing.driveway"]})
        assert ei.value.code == "method_not_allowed", (domain, service)
        assert ei.value.message == "image_processing.driveway: entities in image_processing can never be controlled"
    assert scans == []
    assert core.states.get("image_processing.driveway").state == "unknown"


def in_unknown_ids_words(refused: RpcError, refused_id: str, unknown: RpcError, unknown_id: str) -> bool:
    """The refusal reads exactly as the one for an id that does not exist, once each id is masked."""
    return (refused.code, refused.message.replace(refused_id, "<id>")) == (
        unknown.code,
        unknown.message.replace(unknown_id, "<id>"),
    )


async def test_requires_a_real_exposed_entity_and_service(core: HomeAssistant) -> None:
    """#648: an unexposed entity is refused in an unknown id's words, so the model cannot tell it exists.

    The unknown-id refusal is taken from the handler, not written here, so the row follows that
    message if it changes. Nothing is called, and the same entity exposed is the control.
    """
    d = build_dispatcher(core)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "nope", "entity_id": [TEST]})
    assert ei.value.code == "not_found"
    with pytest.raises(RpcError) as ghost:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": ["input_boolean.ghost"]})
    assert ghost.value.code == "not_found"
    async_expose_entity(core, "conversation", TEST, False)
    calls = async_mock_service(core, "input_boolean", "turn_on")
    with pytest.raises(RpcError) as ei:
        await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    assert in_unknown_ids_words(ei.value, TEST, ghost.value, "input_boolean.ghost"), ei.value.message
    assert calls == []
    async_expose_entity(core, "conversation", TEST, True)
    await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    assert [c.data["entity_id"] for c in calls] == [[TEST]]


@pytest.mark.parametrize(
    "flag",
    [{"hidden_by": er.RegistryEntryHider.USER}, {"disabled_by": er.RegistryEntryDisabler.USER}],
    ids=["hidden", "disabled"],
)
async def test_a_hidden_or_disabled_entity_is_not_operated_even_when_exposed(core: HomeAssistant, flag: dict) -> None:
    """#642: `devices.call` refuses what `states.get` would not show, in the words an unknown id gets.

    The flagged switch and the control differ only in the flag: both are in the registry, have a
    state and are exposed to Assist. A call naming both is refused whole, with nothing sent, which is
    the stated limit for a hub whose entity list is a poll behind: the control in that call does not
    move either. Home Assistant drops a disabled entity's state, so it is set by hand.
    """
    reg = er.async_get(core)
    for object_id, extra in (("flagged", flag), ("control", {})):
        reg.async_get_or_create("switch", "hearth_test", object_id, suggested_object_id=object_id, **extra)
        core.states.async_set(f"switch.{object_id}", "off")
        async_expose_entity(core, "conversation", f"switch.{object_id}", True)
    calls = async_mock_service(core, "switch", "turn_on")
    d = build_dispatcher(core)

    def call(*ids: str):
        return d.dispatch("devices.call", {"domain": "switch", "service": "turn_on", "entity_id": list(ids)})

    with pytest.raises(RpcError) as flagged:
        await call("switch.flagged")
    with pytest.raises(RpcError) as unknown:
        await call("switch.no_such_thing")
    assert (flagged.value.code, flagged.value.message) == ("not_found", "unknown entity switch.flagged")
    assert unknown.value.message == "unknown entity switch.no_such_thing"
    with pytest.raises(RpcError) as mixed:
        await call("switch.control", "switch.flagged")
    assert mixed.value.code == "not_found"
    assert calls == []

    await call("switch.control")
    assert [c.data["entity_id"] for c in calls] == [["switch.control"]]


async def test_an_exposed_entity_in_a_hidden_domain_is_refused_by_the_policy_first(core: HomeAssistant) -> None:
    """`image` is in `HIDDEN_DOMAINS`, and since #566 also in `REFERENCEABLE_DENIED_DOMAINS`.

    So the action policy refuses it before `visible()` is asked, in the policy's words, and those
    words name only the domain the caller sent. `visible()`'s off-limits clause cannot be reached
    from `devices.call` at all: every `OFF_LIMITS` domain is referenceable-denied. That clause is
    pinned on the read path, in `test_visibility.py`. The entity has a state and is exposed, which
    is asserted, so the refusal cannot be the exposure clause either.
    """
    core.states.async_set("image.doorbell", "2026-10-14T00:00:00+00:00")
    async_expose_entity(core, "conversation", "image.doorbell", True)
    assert registry._exposed(core, "image.doorbell")
    calls = async_mock_service(core, "image", "snapshot")
    d = build_dispatcher(core)

    def call(entity_id: str):
        return d.dispatch("devices.call", {"domain": "image", "service": "snapshot", "entity_id": [entity_id]})

    for entity_id in ("image.doorbell", "image.ghost"):
        with pytest.raises(RpcError) as ei:
            await call(entity_id)
        assert (ei.value.code, ei.value.message) == (
            "method_not_allowed",
            f"{entity_id}: entities in image can never be controlled",
        )
    assert calls == []


async def test_data_cannot_widen_the_target(core: HomeAssistant) -> None:
    """entity_id smuggled through `data` is stripped, not honoured."""
    core.states.async_set("input_boolean.other", "off")
    d = build_dispatcher(core)
    await d.dispatch(
        "devices.call",
        {
            "domain": "input_boolean",
            "service": "turn_on",
            "entity_id": [TEST],
            "data": {"entity_id": ["input_boolean.other"], "area_id": "kitchen"},
        },
    )
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "on"
    assert core.states.get("input_boolean.other").state == "off"


async def test_activate_scene_and_run_script(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "All on", "entities": {TEST: "on"}}})
    await core.async_block_till_done()
    result = await d.dispatch("scenes.activate", {"entity_id": scene["entity_id"]})
    await core.async_block_till_done()
    assert result["entity_id"] == scene["entity_id"]
    assert core.states.get(TEST).state == "on"

    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Turn it off", "sequence": [{"action": "input_boolean.turn_off", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()
    # `scene` is in Home Assistant's DEFAULT_EXPOSED_DOMAINS and `script` is not, so a script has to
    # be exposed deliberately before Hearth will run it — the same bar HA's own Assist applies.
    async_expose_entity(core, "conversation", script["entity_id"], True)
    await d.dispatch("scripts.run", {"entity_id": script["entity_id"]})
    await core.async_block_till_done()
    assert core.states.get(TEST).state == "off"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "light.not_a_script"})
    assert ei.value.code == "invalid_params"


async def test_a_script_runs_with_no_inputs_whatever_the_call_carries(core: HomeAssistant, tmp_path: Path) -> None:
    """
    #787: an unlabelled household script runs unchecked (AgDR-0005) because it runs *as the household
    wrote it*. A script that takes a field (`{{ msg }}`) into a notify service would be the model's
    to fill in if `scripts.run` passed anything on, so it passes nothing: only the entity id reaches
    `script.turn_on`, and extra keys in the request, under any name, are not forwarded as variables.
    """
    from pytest_homeassistant_custom_component.common import async_capture_events

    (tmp_path / "scripts.yaml").write_text(
        "announce:\n  alias: Announce\n  fields:\n    msg:\n      selector:\n        text: {}\n"
        "  sequence:\n  - event: hearth_test_said\n    event_data:\n"
        "      message: \"{{ msg | default('as the household wrote it') }}\"\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.announce", True)
    said = async_capture_events(core, "hearth_test_said")

    d = build_dispatcher(core)
    await d.dispatch(
        "scripts.run",
        {
            "entity_id": "script.announce",
            "variables": {"msg": "chosen by the model"},
            "msg": "chosen by the model",
            "data": {"msg": "chosen by the model"},
            "fields": {"msg": "chosen by the model"},
        },
    )
    await core.async_block_till_done()
    assert [e.data["message"] for e in said] == ["as the household wrote it"]


async def test_running_a_routine_requires_exposure(core: HomeAssistant) -> None:
    """
    Exposure gates activating a scene and running a script, exactly as it gates a direct service
    call. Without this, activation was the one way to reach something the owner kept out of Assist.
    """
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "Evening", "entities": {TEST: "on"}}})
    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Nudge", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()

    # A script is not exposed by default, so this also pins that `run_script` refuses one the owner
    # never exposed — today the `routines.run` capability alone let the AI run *any* script in the
    # house, and a script performs whatever its author wrote, locks included.
    assert row_can_run(await d.dispatch("scripts.list", {}), script["entity_id"]) is False

    # #648: refused in the words an unknown scene or script gets, and nothing runs. `scenes.list` and
    # `scripts.list` already name these, so this discloses nothing; it keeps one refusal sentence.
    for method, entity_id in [("scenes.activate", scene["entity_id"]), ("scripts.run", script["entity_id"])]:
        domain = entity_id.split(".")[0]
        with pytest.raises(RpcError) as ghost:
            await d.dispatch(method, {"entity_id": f"{domain}.ghost"})
        async_expose_entity(core, "conversation", entity_id, False)
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, {"entity_id": entity_id})
        assert in_unknown_ids_words(ei.value, entity_id, ghost.value, f"{domain}.ghost"), ei.value.message
        await core.async_block_till_done()
        assert core.states.get(TEST).state == "off"
        async_expose_entity(core, "conversation", entity_id, True)
        await d.dispatch(method, {"entity_id": entity_id})
        await core.async_block_till_done()
        assert core.states.get(TEST).state == "on"
        await core.services.async_call("input_boolean", "turn_off", {"entity_id": TEST}, blocking=True)


async def test_refuses_to_activate_a_scene_that_sets_a_denied_entity(core: HomeAssistant) -> None:
    """
    The hole this closes: the scene policy only ever ran over scenes Hearth *writes*, so a scene the
    owner wrote by hand could set a lock and activating it applied that. Membership is read from the
    scene entity's own `entity_id` attribute, so it works for scenes we cannot edit.
    """
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    async_expose_entity(core, "conversation", "scene.open_the_gate", True)
    d = build_dispatcher(core)

    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.activate", {"entity_id": "scene.open_the_gate"})
    assert ei.value.code == "method_not_allowed"
    # Refused without naming the lock (#722): for a scene with no scenes.yaml entry this attribute is
    # the only place its id appears, and every reader answers for it as for an unknown id.
    assert ei.value.message == scenes.UNREACHABLE_SCENE


async def test_scene_and_script_lists_report_whether_they_can_be_run(core: HomeAssistant) -> None:
    """Listed either way — the catalog is also how a scene is found in order to edit it."""
    d = build_dispatcher(core)
    scene = await d.dispatch("scenes.create", {"config": {"name": "Reading", "entities": {TEST: "on"}}})
    script = await d.dispatch(
        "scripts.create",
        {"config": {"alias": "Later", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": TEST}}]}},
    )
    await core.async_block_till_done()

    def row(rows: list[dict], entity_id: str) -> dict:
        return next(r for r in rows if r["entity_id"] == entity_id)

    assert row(await d.dispatch("scenes.list", {}), scene["entity_id"])["can_run"] is True
    # Not exposed by default, unlike a scene.
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is False
    async_expose_entity(core, "conversation", script["entity_id"], True)
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is True

    async_expose_entity(core, "conversation", scene["entity_id"], False)
    async_expose_entity(core, "conversation", script["entity_id"], False)
    scenes = await d.dispatch("scenes.list", {})
    assert row(scenes, scene["entity_id"])["can_run"] is False
    assert row(scenes, scene["entity_id"])["editable"] is True, "an un-exposed scene is still listed and editable"
    assert row(await d.dispatch("scripts.list", {}), script["entity_id"])["can_run"] is False

    core.states.async_set("scene.side_door", "unknown", {"friendly_name": "Side door", "entity_id": ["lock.side"]})
    async_expose_entity(core, "conversation", "scene.side_door", True)
    assert row(await d.dispatch("scenes.list", {}), "scene.side_door")["can_run"] is False


async def test_reports_the_state_the_device_actually_reached(core: HomeAssistant) -> None:
    """
    `blocking=True` waits for the service handler, not for the entity to write its state, so reading
    straight afterwards used to return the *pre-call* value — the app's tile flipped back and people
    pressed twice. The call now waits for the entity to report before answering.
    """
    d = build_dispatcher(core)
    await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    result = await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_off", "entity_id": [TEST]})
    assert result["entities"] == [{"entity_id": TEST, "state": "off"}], "reported a state the device had not reached"
    assert core.states.get(TEST).state == "off"


async def test_the_settle_listener_wakes_the_call_on_the_event_loop(
    core: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    #649: `_seen` was a plain function, which Home Assistant runs as an executor job, so
    `asyncio.Event.set()` ran on a worker thread. Event is not thread-safe. In the test harness
    (asyncio debug mode) `set()` raised `RuntimeError: Non-thread-safe operation invoked on an event
    loop other than the current one` after marking the waiter's future done, so on runs where the
    call was already waiting the task sat on a finished future with nothing scheduled, past both the
    settle timeout and `CALL_TIMEOUT_S`, until CI's 30-minute limit.

    The hang itself is a race, so this pins its cause rather than waiting for it: the thread that
    sets the event. The `assert set_from` below is the control. If the listener never fired, the
    thread assertion would pass with nothing recorded.
    """
    loop_thread = threading.get_ident()
    set_from: list[int] = []

    class RecordingEvent(asyncio.Event):
        def set(self) -> None:
            set_from.append(threading.get_ident())
            super().set()

    monkeypatch.setattr(asyncio, "Event", RecordingEvent)
    d = build_dispatcher(core)
    assert core.states.get(TEST).state == "off"
    result = await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_on", "entity_id": [TEST]})
    assert result["entities"] == [{"entity_id": TEST, "state": "on"}]
    assert set_from, "the state change never reached the settle listener, so the thread check below proves nothing"
    assert set(set_from) == {loop_thread}, "the settle listener set its Event off the event loop thread"


async def test_a_command_that_changes_nothing_still_answers(core: HomeAssistant) -> None:
    """No state event fires, so this is the path that waits out SETTLE_TIMEOUT_S. It must not hang."""
    d = build_dispatcher(core)
    await d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_off", "entity_id": [TEST]})
    result = await asyncio.wait_for(
        d.dispatch("devices.call", {"domain": "input_boolean", "service": "turn_off", "entity_id": [TEST]}),
        timeout=control.SETTLE_TIMEOUT_S + 5,
    )
    assert result["entities"] == [{"entity_id": TEST, "state": "off"}]


async def test_a_script_hearth_presents_as_a_scene_is_checked_when_it_runs(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The run-time half of #126, which that change left to this one.

    A scene is checked when it runs (AgDR-0012); a script is not (AgDR-0005). So a scene stored as a
    labelled script would have been the one way to spend an unchecked run on scene-shaped contents.
    The check is on the label, so it narrows what a labelled script may do and leaves every other
    script exactly as AgDR-0005 describes.
    """
    from custom_components.hearth_ai import labels as hl

    await hl.async_ensure_labels(core)
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    d = build_dispatcher(core)

    # Written before the label exists on it, so the authoring check does not catch it — which is
    # exactly the case a hand-labelled script, or a script written by an older Hearth, would be in.
    scripts_yaml = tmp_path / "scripts.yaml"
    scripts_yaml.write_text("let_me_in:\n  alias: Let me in\n  sequence:\n  - action: scene.turn_on\n    target:\n      entity_id: scene.open_the_gate\n")
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()

    async_expose_entity(core, "conversation", "script.let_me_in", True)

    # Unlabelled, it runs: a script is the owner's own sequence (AgDR-0005).
    assert (await d.dispatch("scripts.run", {"entity_id": "script.let_me_in"}))["entity_id"] == "script.let_me_in"

    # Labelled as a Hearth scene, its contents are checked and the lock stops it.
    await hl.async_mark_hearth_scene(core, "script.let_me_in", True)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.let_me_in"})
    assert ei.value.code == "method_not_allowed"
    # The nested scene's lock is refused without being named (#722).
    assert scenes.UNREACHABLE_SCENE in ei.value.message and "lock.gate" not in ei.value.message

    # A labelled script of ordinary actions still runs.
    scripts_yaml.write_text("film_time:\n  alias: Film time\n  sequence:\n  - action: input_boolean.turn_on\n    target:\n      entity_id: input_boolean.test\n")
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.film_time", True)
    await hl.async_mark_hearth_scene(core, "script.film_time", True)
    assert (await d.dispatch("scripts.run", {"entity_id": "script.film_time"}))["entity_id"] == "script.film_time"


async def test_a_hearth_scene_script_that_sends_a_raw_remote_code_is_refused_when_it_runs(
    core: HomeAssistant, tmp_path: Path
) -> None:
    """#568: a scene stored as a labelled script cannot play a raw `b64:` code, whenever it was written.

    A code the household taught the remote, sent by name, is allowed (the owner's ruling of
    2026-11-08); the last part runs one through the same labelled path.

    The script is written to `scripts.yaml` directly, as a script authored before the refusal would
    be, so only the run-time scan stands between it and the remote. Unlabelled, it still runs: a
    plain script is not re-scanned (AgDR-0005), which is the known limit #568 records.
    """
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.hearth_ai import labels as hl

    await hl.async_ensure_labels(core)
    sent = async_mock_service(core, "remote", "send_command")
    (tmp_path / "scripts.yaml").write_text(
        "disarm:\n  alias: Disarm\n  sequence:\n  - action: remote.send_command\n    target:\n"
        "      entity_id: remote.living_room\n    data:\n      device: alarm_keyfob\n      command: b64:JgBQAAABKZIUEhQ2\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    async_expose_entity(core, "conversation", "script.disarm", True)
    d = build_dispatcher(core)

    await hl.async_mark_hearth_scene(core, "script.disarm", True)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    assert ei.value.code == "method_not_allowed"
    assert "action remote.send_command sends a raw remote code" in ei.value.message
    await core.async_block_till_done()
    assert sent == []

    # The control, and the limit: the same script unlabelled reaches the remote.
    await hl.async_mark_hearth_scene(core, "script.disarm", False)
    await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    await core.async_block_till_done()
    assert [c.data.get("device") for c in sent] == ["alarm_keyfob"]

    # A taught code, by name, runs through the labelled path, to a remote that plays stored names alone.
    er.async_get(core).async_get_or_create("remote", "broadlink", "rm-1", suggested_object_id="living_room")
    (tmp_path / "scripts.yaml").write_text(
        "disarm:\n  alias: Disarm\n  sequence:\n  - action: remote.send_command\n    target:\n"
        "      entity_id: remote.living_room\n    data:\n      device: television\n      command: power\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    await hl.async_mark_hearth_scene(core, "script.disarm", True)
    await d.dispatch("scripts.run", {"entity_id": "script.disarm"})
    await core.async_block_till_done()
    assert [c.data.get("device") for c in sent] == ["alarm_keyfob", "television"]
