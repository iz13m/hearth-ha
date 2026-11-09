"""An authored call that hands a household script inputs is checked, not refused (#777).

Owner ruling, 2026-11-08: when an authored automation, script or config-flow form value starts a
household `script.*` with inputs (`script.turn_on` with `variables`, or `script.<id>` with `data`), the
box reads the started script and refuses only when an input could fill in a guarded field: a target,
an `entity_id`, a notify `icon` (#771), a remote's command or activity (#765), a valve position
(#527), a service name. A parameterised announcement keeps working.

Each household script below is called three ways through `automations.validate`: with inputs, with
none, and (where it matters) through a script that relays its inputs one hop further.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hearth_ai.handlers.flows import check_flow_input
from custom_components.hearth_ai.handlers.inputs import find_input_reachable_violations
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SCRIPTS = """
announce:
  fields:
    message: {}
  sequence:
  - action: notify.notify
    data:
      title: Hearth
      message: "{{ message }}"
warm:
  fields:
    temp: {}
  sequence:
  - action: climate.set_temperature
    target:
      entity_id: climate.hall
    data:
      temperature: "{{ temp }}"
notify_tv:
  fields:
    message: {}
    icon: {}
  sequence:
  - action: notify.living_room_tv
    data:
      message: "{{ message }}"
      data:
        icon: "{{ icon }}"
room_light:
  fields:
    room: {}
  sequence:
  - action: light.turn_on
    target:
      entity_id: "light.{{ room }}"
remote_power:
  fields:
    remote: {}
  sequence:
  - action: remote.send_command
    target:
      entity_id: "{{ remote }}"
    data:
      command: power
deep_light:
  sequence:
  - choose:
    - conditions: "{{ true }}"
      sequence:
      - repeat:
          count: 1
          sequence:
          - action: light.turn_on
            data:
              entity_id: "{{ which }}"
relay_tv:
  sequence:
  - action: script.turn_on
    target:
      entity_id: script.notify_tv
    data:
      variables:
        icon: "{{ icon }}"
        message: "{{ message }}"
relay_announce:
  sequence:
  - action: script.announce
    data:
      message: "{{ message }}"
tv_badge:
  fields:
    message: {}
  sequence:
  - action: notify.living_room_tv
    data:
      message: "{{ message }}"
      data:
        icon: mdi:bell
relay_room:
  sequence:
  - action: script.turn_on
    target:
      entity_id: script.room_light
    data:
      variables:
        room: "{{ room }}"
relay_fixed:
  sequence:
  - action: script.room_light
relay_literal:
  sequence:
  - action: script.room_light
    data:
      room: kitchen
  - action: script.turn_on
    target:
      entity_id: script.tv_badge
    data:
      variables:
        message: hi
picker:
  fields:
    what:
      selector:
        action: {}
    message:
      selector:
        text: {}
  variables:
    preset:
      action: light.turn_on
      target:
        entity_id: "{{ message }}"
  sequence:
  - action: notify.notify
    data:
      message: "{{ message }}"
relay_relay:
  sequence:
  - action: script.relay_room
    data:
      room: "{{ room }}"
"""

# The two ways an authored call hands a script inputs.
def _turn_on(script: str, variables: dict[str, Any] | None) -> dict[str, Any]:
    node: dict[str, Any] = {"action": "script.turn_on", "target": {"entity_id": script}}
    if variables is not None:
        node["data"] = {"variables": variables}
    return node


def _direct(script: str, variables: dict[str, Any] | None) -> dict[str, Any]:
    node: dict[str, Any] = {"action": script}
    if variables is not None:
        node["data"] = variables
    return node


CALLS = {"turn_on": _turn_on, "direct": _direct}

INPUTS = {
    "script.announce": {"message": "Dinner"},
    "script.warm": {"temp": 21},
    "script.notify_tv": {"message": "hi", "icon": "/config/secrets.yaml"},
    "script.room_light": {"room": "kitchen"},
    "script.remote_power": {"remote": "remote.tv"},
    "script.deep_light": {"which": "light.hall"},
    "script.relay_tv": {"message": "hi", "icon": "/config/secrets.yaml"},
    "script.relay_announce": {"message": "Dinner"},
    "script.relay_fixed": {"message": "hi"},
    "script.relay_relay": {"room": "kitchen"},
    "script.relay_room": {"room": "kitchen"},
    "script.tv_badge": {"message": "hi"},
    "script.relay_literal": {"message": "hi"},
    "script.picker": {"message": "hi", "what": [{"action": "light.turn_on"}]},
}

ALLOWED_WITH_INPUTS = ["script.announce", "script.warm", "script.tv_badge", "script.relay_announce", "script.relay_fixed", "script.relay_literal", "script.picker"]
# Refused with inputs and allowed without: for these, inputs are what matters.
REFUSED_WITH_INPUTS = ["script.room_light", "script.deep_light", "script.relay_room", "script.relay_relay"]
# A templated notify `icon` one hop away is #771's to refuse, with or without inputs (`find_policy_violations`).
REFUSED_ALWAYS = ["script.notify_tv", "script.relay_tv"]


@pytest.fixture
async def household(core: HomeAssistant, tmp_path: Path) -> HomeAssistant:
    (tmp_path / "scripts.yaml").write_text(SCRIPTS)
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    for key in INPUTS:
        assert core.states.get(key) is not None, key
    return core


async def _verdict(hass: HomeAssistant, *actions: dict[str, Any]) -> dict[str, Any]:
    return await build_dispatcher(hass).dispatch(
        "automations.validate",
        {"config": {"alias": "x", "triggers": [{"trigger": "sun", "event": "sunset"}], "actions": list(actions)}},
    )


@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize("script", ALLOWED_WITH_INPUTS)
async def test_inputs_that_feed_only_unguarded_fields_are_allowed(household: HomeAssistant, call: str, script: str) -> None:
    res = await _verdict(household, CALLS[call](script, INPUTS[script]))
    assert res["ok"] is True, res


@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize("script", REFUSED_WITH_INPUTS)
async def test_inputs_that_could_fill_a_guarded_field_are_refused(household: HomeAssistant, call: str, script: str) -> None:
    res = await _verdict(household, CALLS[call](script, INPUTS[script]))
    assert res["ok"] is False and res["status"] == "policy", res
    assert "started with inputs" in res["error"], res


@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize("script", ALLOWED_WITH_INPUTS + REFUSED_WITH_INPUTS)
async def test_the_same_scripts_started_without_inputs_are_unchanged(household: HomeAssistant, call: str, script: str) -> None:
    """The control: no inputs, no new question. Calls with an empty `variables` or `data` count as none."""
    for node in (CALLS[call](script, None), CALLS[call](script, {})):
        res = await _verdict(household, node)
        assert res["ok"] is True, (node, res)


@pytest.mark.parametrize("call", list(CALLS))
@pytest.mark.parametrize("script", REFUSED_ALWAYS)
async def test_a_templated_icon_in_a_household_script_is_refused_with_or_without_inputs(household: HomeAssistant, call: str, script: str) -> None:
    """#771 landed first: its icon rule runs in the nested policy walk, so a household script whose
    notify `icon` is a template is refused even with no inputs. With inputs, this rule says so too."""
    res = await _verdict(household, CALLS[call](script, INPUTS[script]))
    assert res["ok"] is False and "started with inputs" in res["error"] and "mdi:" in res["error"], res
    for node in (CALLS[call](script, None), CALLS[call](script, {})):
        res = await _verdict(household, node)
        assert res["ok"] is False and "mdi:" in res["error"] and "started with inputs" not in res["error"], (node, res)


@pytest.mark.parametrize("call", list(CALLS))
async def test_a_remote_target_fed_from_an_input_is_refused(household: HomeAssistant, call: str) -> None:
    """Refused with inputs. Without them it is refused too, by #765's rule, which predates this one:
    a household script's remote target may not be a template at all (`find_nested_remote_violations`)."""
    res = await _verdict(household, CALLS[call]("script.remote_power", INPUTS["script.remote_power"]))
    assert res["ok"] is False and "started with inputs" in res["error"], res
    res = await _verdict(household, CALLS[call]("script.remote_power", None))
    assert res["ok"] is False and "started with inputs" not in res["error"], res


async def test_a_script_reached_first_without_inputs_is_still_asked_when_reached_with_them(household: HomeAssistant) -> None:
    """The walk visits once per (script, inputs): keyed on the script alone, the first call's verdict
    stood for the second and the icon went through."""
    res = await _verdict(household, _turn_on("script.room_light", None), _turn_on("script.room_light", INPUTS["script.room_light"]))
    assert res["ok"] is False and "started with inputs" in res["error"], res


@pytest.mark.parametrize(
    "node",
    [
        # A whole-template `data` can render to `variables`; a templated key can render to it too.
        {"action": "script.turn_on", "target": {"entity_id": "script.room_light"}, "data": "{{ {'variables': {'room': 'kitchen'}} }}"},
        {"action": "script.turn_on", "target": {"entity_id": "script.room_light"}, "data": {"{{ 'variables' }}": {"room": "kitchen"}}},
        # `data_template` is `data` by another name.
        {"action": "script.turn_on", "target": {"entity_id": "script.room_light"}, "data_template": {"variables": {"room": "kitchen"}}},
        {"action": "script.room_light", "data_template": {"room": "kitchen"}},
        # HA folds `target` into the service data, and the per-script service passes all of it as
        # variables (security F1), so a target or a node-level selector on `script.<id>` is an input.
        {"action": "script.room_light", "target": {"entity_id": "light.x"}},
        {"action": "script.room_light", "target": "{{ {'room': 'kitchen'} }}"},
        {"action": "script.room_light", "entity_id": "light.x"},
        {"action": "script.room_light", "area_id": "kitchen"},
        # Case-folded like every service name.
        {"action": "Script.Room_Light", "data": {"room": "kitchen"}},
    ],
)
async def test_every_spelling_of_passing_inputs_counts(household: HomeAssistant, node: dict[str, Any]) -> None:
    res = await _verdict(household, node)
    assert res["ok"] is False and "started with inputs" in res["error"], res


async def test_an_unreadable_script_started_with_inputs_fails_closed(household: HomeAssistant) -> None:
    res = await _verdict(household, _turn_on("script.not_loaded", {"icon": "/x"}))
    assert res["ok"] is False and "does not have" in res["error"], res


async def test_an_authored_script_is_held_to_the_same_rule(household: HomeAssistant) -> None:
    d = build_dispatcher(household)
    refused = await d.dispatch("scripts.validate", {"key": "probe", "config": {"sequence": [_turn_on("script.room_light", INPUTS["script.room_light"])]}})
    assert refused["ok"] is False and "started with inputs" in refused["error"], refused
    allowed = await d.dispatch("scripts.validate", {"key": "probe", "config": {"sequence": [_direct("script.announce", INPUTS["script.announce"])]}})
    assert allowed["ok"] is True, allowed


async def test_automations_create_refuses_and_writes_nothing(household: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(household)
    config = {"alias": "TV icon", "triggers": [{"trigger": "event", "event_type": "probe"}], "actions": [_direct("script.room_light", INPUTS["script.room_light"])]}
    with pytest.raises(RpcError) as e:
        await d.dispatch("automations.create", {"config": config})
    assert "started with inputs" in e.value.message
    assert "TV icon" not in (tmp_path / "automations.yaml").read_text()
    ok = dict(config, alias="Dinner", actions=[_direct("script.announce", INPUTS["script.announce"])])
    await d.dispatch("automations.create", {"config": ok})
    assert "Dinner" in (tmp_path / "automations.yaml").read_text()


async def test_a_config_flow_value_is_held_to_the_same_rule(household: HomeAssistant) -> None:
    """A template switch's `turn_on` shares the walk (#721), so it shares the rule."""
    with pytest.raises(RpcError) as e:
        await check_flow_input(household, {"name": "Probe", "turn_on": [_turn_on("script.room_light", INPUTS["script.room_light"])]})
    assert "started with inputs" in e.value.message
    await check_flow_input(household, {"name": "Probe", "turn_on": [_turn_on("script.announce", INPUTS["script.announce"])]})


# --------------------------------------------------------------------------- the guarded fields, one by one


def _one(action: dict[str, Any]) -> list[str]:
    return find_input_reachable_violations({"sequence": [action]}, "s", "script.x")


@pytest.mark.parametrize(
    "action",
    [
        {"action": "notify.tv", "data": {"message": "m", "data": {"icon": "{{ icon }}"}}},
        {"action": "notify.tv", "data": {"message": "m", "target": "{{ who }}"}},
        {"action": "light.turn_on", "target": {"entity_id": "light.{{ room }}"}},
        {"action": "light.turn_on", "target": "{{ t }}"},
        {"action": "light.turn_on", "target": {"{{ k }}": "kitchen"}},
        {"action": "light.turn_on", "entity_id": "{{ e }}"},
        {"action": "light.turn_on", "data": {"area_id": "{{ a }}"}},
        {"action": "light.turn_on", "data": "{{ d }}"},
        {"action": "light.turn_on", "data_template": {"device_id": "{{ d }}"}},
        {"action": "light.turn_on", "data": {"floor_id": "{{ f }}"}},
        {"action": "light.turn_on", "data": {"label_id": "{{ l }}"}},
        {"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": "{{ s }}"}},
        {"action": "light.turn_on", "device_id": "{{ d }}"},
        {"action": "light.turn_on", "area_id": "{{ a }}"},
        {"action": "light.turn_on", "floor_id": "{{ f }}"},
        {"action": "light.turn_on", "label_id": "{{ l }}"},
        {"action": "{{ svc }}"},
        {"service_template": "light.{{ s }}"},
        {"scene": "scene.{{ s }}"},
        {"action": "scene.apply", "data": {"entities": {"light.a": "{{ s }}"}}},
        {"action": "remote.send_command", "target": {"entity_id": "remote.tv"}, "data": {"command": "{{ c }}"}},
        {"action": "remote.learn_command", "target": {"entity_id": "{{ r }}"}, "data": {"command": "x"}},
        {"action": "remote.turn_on", "target": {"entity_id": "remote.tv"}, "data": {"activity": "{{ a }}"}},
        {"action": "valve.set_valve_position", "target": {"entity_id": "valve.main"}, "data": {"position": "{{ p }}"}},
        {"action": "media_player.play_media", "data": {"media_content_id": "{{ m }}", "media_content_type": "video"}},
        {"action": "script.turn_on", "target": {"entity_id": "script.{{ s }}"}},
        {"device_id": "{{ d }}", "domain": "light", "type": "turn_on"},
        {"device_id": "abc", "domain": "light", "type": "{{ t }}"},
    ],
)
def test_each_guarded_field_is_caught(action: dict[str, Any]) -> None:
    assert _one(action) != [], action


@pytest.mark.parametrize(
    "action",
    [
        {"action": "notify.tv", "data": {"message": "{{ m }}", "title": "{{ t }}", "data": {"icon": "/local/tv.png"}}},
        {"action": "climate.set_temperature", "target": {"entity_id": "climate.hall"}, "data": {"temperature": "{{ t }}"}},
        {"action": "light.turn_on", "target": {"entity_id": "light.hall"}, "data": {"brightness_pct": "{{ b }}"}},
        {"action": "cover.set_cover_position", "target": {"entity_id": "cover.blind"}, "data": {"position": "{{ p }}"}},
        # The started script's own variables are walked one hop further, not here.
        {"action": "script.turn_on", "target": {"entity_id": "script.y"}, "data": {"variables": {"icon": "{{ icon }}"}}},
        {"action": "script.y", "data": {"entity_id": "{{ e }}"}},
        {"delay": "{{ d }}"},
        {"condition": "template", "value_template": "{{ x }}"},
        {"alias": "{{ a }}", "action": "light.turn_on", "target": {"entity_id": "light.hall"}},
    ],
)
def test_unguarded_fields_are_not(action: dict[str, Any]) -> None:
    assert _one(action) == [], action
