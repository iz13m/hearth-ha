"""A config-flow form value that activates a scene or starts a script is checked one hop away (#721).

`check_flow_input` ran the policy, reference and `switch_as_x` gate walks over form values, but not
the two that read what a scene holds or what a script does. So a model with `helpers.manage` (or
`integrations.manage`) made a `template` switch whose `turn_on` was `scene.turn_on` of a household
scene setting `lock.gate: unlocked`, and turning the switch on unlocked the gate.

Each attack is first run through Home Assistant's own template flow, with Hearth not involved, and
the helper it makes is operated with `lock.unlock` mocked: that pins the vector as real on the pinned
release. Then the same form is refused through `helpers.create` and through `integrations.flow_step`,
leaving no helper behind (`helpers.create` abandons its flow; a refused `flow_step` leaves the flow
open for the person to correct, which `_hearth` asserts before it aborts it), and a control that
differs in one thing (a scene that only lights the porch) is made. `helpers.update` is checked the
same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai.handlers.scenes import UNREACHABLE_SCENE
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

LOCK = "lock.gate"
AUTOMATION = "automation.night_watch"

SCENES = (
    "- id: gate\n  name: Gate\n  entities:\n    lock.gate: unlocked\n"
    "- id: porch\n  name: Porch\n  entities:\n    light.porch: 'on'\n"
    "- id: away\n  name: Away\n  entities:\n    automation.night_watch: 'off'\n"
)
SCRIPTS = "door:\n  sequence:\n  - action: lock.unlock\n    target:\n      entity_id: lock.gate\n"

# variant -> the form field that holds its action sequence, and how a person operates it.
VARIANTS = {"switch": ("turn_on", "switch", "turn_on"), "button": ("press", "button", "press")}
PATHS = ["helpers.create", "integrations.flow_step"]


async def _household(hass: HomeAssistant, tmp_path: Path) -> None:
    """The owner's own scenes and script, as Home Assistant loads them, and the things they set."""
    hass.states.async_set(LOCK, "locked")
    hass.states.async_set("light.porch", "off")
    hass.states.async_set(AUTOMATION, "on")
    (tmp_path / "scenes.yaml").write_text(SCENES)
    (tmp_path / "scripts.yaml").write_text(SCRIPTS)
    await hass.services.async_call("scene", "reload", blocking=True)
    await hass.services.async_call("script", "reload", blocking=True)
    await hass.async_block_till_done()
    for eid in ("scene.gate", "scene.porch", "scene.away", "script.door"):
        assert hass.states.get(eid) is not None, eid


def _form(variant: str, step: dict[str, Any]) -> dict[str, Any]:
    return {"name": f"Probe {variant}", VARIANTS[variant][0]: [step]}


def _scene(entity_id: str) -> dict[str, Any]:
    return {"action": "scene.turn_on", "target": {"entity_id": entity_id}}


def _script(entity_id: str) -> dict[str, Any]:
    return {"action": "script.turn_on", "target": {"entity_id": entity_id}}


def _made(hass: HomeAssistant, variant: str) -> list[str]:
    return [e.entity_id for e in er.async_get(hass).entities.values() if e.platform == "template" and e.domain == VARIANTS[variant][1]]


async def _operate(hass: HomeAssistant, variant: str, entity_id: str) -> None:
    _, domain, service = VARIANTS[variant]
    await hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


async def _ha_flow(hass: HomeAssistant, variant: str, config: dict[str, Any]) -> str:
    """Make the helper through Home Assistant's own template flow, Hearth not involved; its entity id."""
    flow = hass.config_entries.flow
    result = await flow.async_init("template", context={"source": "user"})
    assert result["type"] is FlowResultType.MENU, result
    result = await flow.async_configure(result["flow_id"], {"next_step_id": variant})
    result = await flow.async_configure(result["flow_id"], config)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    (made,) = _made(hass, variant)
    return made


async def _hearth(hass: HomeAssistant, path: str, variant: str, config: dict[str, Any]) -> tuple[str, str] | None:
    """Ask Hearth to make the helper by `path`; the refusal, or None once it is made."""
    d = build_dispatcher(hass)
    try:
        if path == "helpers.create":
            await d.dispatch("helpers.create", {"domain": "template", "variant": variant, "config": config})
            return None
        started = await d.dispatch("integrations.flow_start", {"domain": "template"})
        menu = await d.dispatch("integrations.flow_step", {"flow_id": started["flow_id"], "input": {"next_step_id": variant}})
        assert menu.get("step_id") == variant, menu
        try:
            done = await d.dispatch("integrations.flow_step", {"flow_id": started["flow_id"], "input": config})
            assert done["type"] == "create_entry", done
            return None
        except RpcError:
            # A refused step leaves the flow open, as `integrations.flow_step` always has: the person
            # may correct the form. So a refusal here must find it still in progress, unchanged.
            assert [f["flow_id"] for f in hass.config_entries.flow.async_progress()] == [started["flow_id"]]
            await d.dispatch("integrations.flow_abort", {"flow_id": started["flow_id"]})
            raise
    except RpcError as e:
        return (e.code, e.message)


# --------------------------------------------------------------------------- the attack is real


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("step", ["scene", "script"])
async def test_home_assistant_unlocks_through_a_template_helper(core: HomeAssistant, tmp_path: Path, variant: str, step: str) -> None:
    """Pins the vector on the pinned release: the helper's action really reaches the lock."""
    await _household(core, tmp_path)
    unlocked = async_mock_service(core, "lock", "unlock")
    action = _scene("scene.gate") if step == "scene" else _script("script.door")
    made = await _ha_flow(core, variant, _form(variant, action))
    assert not unlocked
    await _operate(core, variant, made)
    assert [c.data["entity_id"] for c in unlocked] in ([LOCK], [[LOCK]]), unlocked


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_home_assistant_switches_an_automation_off_through_a_template_helper(core: HomeAssistant, tmp_path: Path, variant: str) -> None:
    await _household(core, tmp_path)
    turned_off = async_mock_service(core, "automation", "turn_off")
    made = await _ha_flow(core, variant, _form(variant, _scene("scene.away")))
    await _operate(core, variant, made)
    assert [c.data["entity_id"] for c in turned_off] in ([AUTOMATION], [[AUTOMATION]]), turned_off


# --------------------------------------------------------------------------- and Hearth refuses it


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_a_template_helper_activating_a_scene_that_unlocks_is_not_made(core: HomeAssistant, tmp_path: Path, path: str, variant: str) -> None:
    """By the scene's entity id and by its registry id (#663). Control: a scene that lights the porch."""
    await _household(core, tmp_path)
    gate_uuid = er.async_get(core).async_get("scene.gate").id
    for target in ("scene.gate", gate_uuid):
        got = await _hearth(core, path, variant, _form(variant, _scene(target)))
        # The attribute check no longer names the lock (#722). This scene is in scenes.yaml, so the
        # config check still does: `scenes.get` returns that config as written.
        assert got is not None and got[0] == "validation_failed" and UNREACHABLE_SCENE in got[1], (target, got)
        assert _made(core, variant) == [] and core.config_entries.flow.async_progress() == [], target
    assert await _hearth(core, path, variant, _form(variant, _scene("scene.porch"))) is None
    assert len(_made(core, variant)) == 1


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_a_template_helper_starting_a_script_that_unlocks_is_not_made(core: HomeAssistant, tmp_path: Path, path: str, variant: str) -> None:
    """`script.turn_on` and the per-script service alike; a script Home Assistant does not have is refused too."""
    await _household(core, tmp_path)
    for step in (_script("script.door"), {"action": "script.door"}, _script("script.not_there")):
        got = await _hearth(core, path, variant, _form(variant, step))
        assert got is not None and got[0] == "validation_failed", (step, got)
        assert _made(core, variant) == [] and core.config_entries.flow.async_progress() == [], step
    got = await _hearth(core, path, variant, _form(variant, _script("script.door")))
    assert got is not None and "script.door" in got[1] and "lock" in got[1], got


@pytest.mark.parametrize("path", PATHS)
async def test_a_scene_activated_by_every_scene_or_a_room_is_refused_in_a_form(core: HomeAssistant, tmp_path: Path, path: str) -> None:
    """`entity_id: all` reaches `scene.gate` with the rest, and an area cannot be read as named scenes."""
    await _household(core, tmp_path)
    for step in (_scene("all"), {"action": "scene.turn_on", "target": {"area_id": "porch"}}):
        got = await _hearth(core, path, "switch", _form("switch", step))
        assert got is not None and got[0] == "validation_failed", (step, got)
    assert _made(core, "switch") == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_a_template_helper_activating_a_scene_that_switches_an_automation_off_is_not_made(core: HomeAssistant, tmp_path: Path, path: str, variant: str) -> None:
    """#713's authoring-time refusal, reached from a form as from an automation. Its control is the
    porch scene above; activating `scene.away` itself stays the household's own business."""
    await _household(core, tmp_path)
    got = await _hearth(core, path, variant, _form(variant, _scene("scene.away")))
    assert got is not None and got[0] == "validation_failed" and AUTOMATION in got[1] and "through a scene" in got[1], got
    assert _made(core, variant) == []


async def test_helpers_update_is_checked_like_a_create(core: HomeAssistant, tmp_path: Path) -> None:
    """`helpers.update` runs the same form check before it touches the item. Control: a rename-free
    patch that names nothing is applied."""
    await _household(core, tmp_path)
    d = build_dispatcher(core)
    made = await d.dispatch("helpers.create", {"domain": "input_boolean", "config": {"name": "Guests coming"}})
    for config, words in (
        ({"icon": "mdi:door", "entity_id": "lock.front_door"}, "lock.front_door"),
        ({"icon": "mdi:door", "turn_on": [_scene("scene.gate")]}, UNREACHABLE_SCENE),
    ):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("helpers.update", {"id": made["id"], "config": config})
        assert ei.value.code == "validation_failed" and ei.value.message.startswith("Hearth will not save that") and words in ei.value.message, ei.value.message
        assert core.states.get(made["entities"][0]).attributes.get("icon") is None
    done = await d.dispatch("helpers.update", {"id": made["id"], "config": {"icon": "mdi:door"}})
    assert done["id"] == made["id"]
    assert core.states.get(made["entities"][0]).attributes.get("icon") == "mdi:door"
