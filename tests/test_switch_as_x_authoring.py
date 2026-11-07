"""PR B of #643: what a model writes is checked against the `switch_as_x` gate before it is saved.

PR A (`test_switch_as_x.py`) made the readers, `devices.call` and the run checks see a lock's relay as
the lock. These rows cover authoring: automations, scripts and scenes (`handlers/gated.py`, through
`find_config_violations` and `scenes._validate`), config-flow form values (`check_flow_input`), a
script an automation starts (`find_nested_script_violations`), and a scene-script that starts one.

Every row sets up real `switch_as_x` entries through `test_switch_as_x`'s helpers, and every refusal
has a control that differs from it in one thing.
"""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearth_ai import labels as hl
from custom_components.hearth_ai.handlers.gated import GATED_NAMED
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

from .test_switch_as_x import _bare, _calls, _err, _group, _mock_switch, _source, _wrap, _wrapper

KINDS = ["automation", "script"]


def _routine(kind: str, step: dict[str, Any], *, condition: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind == "automation":
        config: dict[str, Any] = {"alias": "probe", "triggers": [{"trigger": "time", "at": "07:00:00"}], "actions": [step]}
        if condition is not None:
            config["conditions"] = [condition]
        return config
    return {"alias": "probe", "sequence": ([{"condition": "template", "value_template": condition["value_template"]}] if condition else []) + [step]}


async def _validate(hass: HomeAssistant, kind: str, step: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return await build_dispatcher(hass).dispatch(f"{kind}s.validate", {"config": _routine(kind, step, **kw)})


def _refused(res: dict[str, Any]) -> str:
    assert res["ok"] is False and res["status"] == "policy", res
    return str(res["error"])


def _on(entity_id: str) -> dict[str, Any]:
    return {"action": "switch.turn_on", "target": {"entity_id": entity_id}}


async def _porch(hass: HomeAssistant) -> str:
    area = ar.async_get(hass).async_create("Porch")
    return area.id


# --------------------------------------------------------------------------- a gated id, named


@pytest.mark.parametrize("kind", KINDS)
async def test_a_routine_that_turns_on_a_lock_relay_is_refused_without_naming_it(core: HomeAssistant, kind: str) -> None:
    """The literal walk reads `switch.relay` as a switch. Refused naming neither the relay nor the lock.
    Control: the same step on a plain switch beside it is accepted."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")

    error = _refused(await _validate(core, kind, _on(relay)))
    assert error == GATED_NAMED, error
    assert relay not in error and "lock" not in error
    assert (await _validate(core, kind, _on(lamp)))["ok"] is True


async def test_nothing_is_written_when_a_routine_names_a_lock_relay(core: HomeAssistant, tmp_path: Any) -> None:
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("automations.create", {"config": _routine("automation", _on(relay))}))
    assert got == ("validation_failed", GATED_NAMED), got
    assert (tmp_path / "automations.yaml").read_text().strip() == "[]"
    got = await _err(d.dispatch("scripts.create", {"config": _routine("script", _on(relay))}))
    assert got == ("validation_failed", GATED_NAMED), got
    assert (tmp_path / "scripts.yaml").read_text().strip() == "{}"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_lock_relay_read_in_a_template_is_refused(core: HomeAssistant, kind: str) -> None:
    """A lock's relay is the lock's state, so naming it anywhere is refused, as naming a `lock.*` is.
    Control: the same template over a plain switch."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    step = {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.test"}}

    error = _refused(await _validate(core, kind, step, condition={"condition": "template", "value_template": f"{{{{ is_state('{relay}', 'on') }}}}"}))
    assert error == GATED_NAMED, error
    ok = await _validate(core, kind, step, condition={"condition": "template", "value_template": f"{{{{ is_state('{lamp}', 'on') }}}}"})
    assert ok["ok"] is True, ok


@pytest.mark.parametrize("kind", KINDS)
async def test_a_lock_relay_by_registry_id_is_refused(core: HomeAssistant, kind: str) -> None:
    """Ruling 10's UUID form: the registry id is resolved before the gate is asked."""
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    relay_uuid = er.async_get(core).async_get(relay).id

    assert _refused(await _validate(core, kind, _on(relay_uuid))) == GATED_NAMED


@pytest.mark.parametrize("kind", KINDS)
async def test_a_device_action_on_a_gated_relay_is_refused(core: HomeAssistant, kind: str) -> None:
    """Ruling 10's device-action form: no `action` key, the relay named by registry id. A valve's relay
    is the row that needs the device-action branch, since a lock's is also refused for being named.
    Control: the same device action on a plain switch on the same device passes the policy walk."""
    entry = MockConfigEntry(domain="hearth_test")
    entry.add_to_hass(core)
    device = dr.async_get(core).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hearth_test", "box")})
    reg = er.async_get(core)
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    lamp = await _source(core, "lamp")
    for entity_id in (relay, tap, lamp):
        reg.async_update_entity(entity_id, device_id=device.id)
    await _wrap(core, relay, "lock")
    await _wrap(core, tap, "valve")

    def action(entity_id: str) -> dict[str, Any]:
        return {"device_id": device.id, "domain": "switch", "type": "turn_off", "entity_id": reg.async_get(entity_id).id}

    assert _refused(await _validate(core, kind, action(relay))) == GATED_NAMED
    error = _refused(await _validate(core, kind, action(tap)))
    assert tap in error and "valve" in error, error
    res = await _validate(core, kind, action(lamp))
    assert res.get("status") != "policy", res


@pytest.mark.parametrize("kind", KINDS)
async def test_a_switch_group_over_a_lock_relay_is_refused(core: HomeAssistant, kind: str) -> None:
    relay = await _source(core, "relay")
    porch = await _source(core, "porch_light")
    await _wrap(core, relay, "lock")
    outside = await _group(core, "switch", [relay, porch], "All outside")

    assert _refused(await _validate(core, kind, _on(outside))) == GATED_NAMED
    # The porch switch beside the relay in that group is not gated (ruling 9: only down from a gated source).
    assert (await _validate(core, kind, _on(porch)))["ok"] is True


@pytest.mark.parametrize("kind", KINDS)
async def test_a_valve_relay_is_refused_both_ways_and_may_still_be_read(core: HomeAssistant, kind: str) -> None:
    """Ruling 1: through `switch.*` a valve's relay is refused in both directions, named with its gate,
    since its wrapper is listed. Reading it is not moving it, so a condition on it is accepted, and the
    wrapper itself still closes (#527)."""
    tap = await _source(core, "tap")
    entry = await _wrap(core, tap, "valve")
    valve = _wrapper(core, entry)

    for service in ("turn_on", "turn_off"):
        error = _refused(await _validate(core, kind, {"action": f"switch.{service}", "target": {"entity_id": tap}}))
        assert tap in error and "valve" in error, error
    step = {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.test"}}
    ok = await _validate(core, kind, step, condition={"condition": "template", "value_template": f"{{{{ is_state('{tap}', 'on') }}}}"})
    assert ok["ok"] is True, ok
    assert (await _validate(core, kind, {"action": "valve.close_valve", "target": {"entity_id": valve}}))["ok"] is True


# --------------------------------------------------------------------------- ruling 2: indirect and unknowable targets, home-wide

_SELECTIONS: dict[str, dict[str, Any]] = {
    "area": {"target": {"area_id": "porch"}},
    "device": {"target": {"device_id": "0123456789abcdef0123456789abcdef"}},
    "label": {"target": {"label_id": "outside"}},
    "floor": {"target": {"floor_id": "ground"}},
    "template_id": {"target": {"entity_id": "{{ 'switch.' ~ 'relay' }}"}},
    "data_template": {"data_template": "{{ {'entity_id': 'switch.relay'} }}"},
    "all": {"target": {"entity_id": "all"}},
}


@pytest.mark.parametrize("form", list(_SELECTIONS))
@pytest.mark.parametrize("kind", KINDS)
async def test_a_switch_action_that_selects_indirectly_is_refused_in_a_gated_home(core: HomeAssistant, kind: str, form: str) -> None:
    """Ruling 2, and ruling 10's template form. Home-wide on purpose: the porch has no relay in it at
    all, and the area target is still refused, because what an area holds changes after the config is
    written. The words name neither the relay nor the lock."""
    await _porch(core)
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")

    error = _refused(await _validate(core, kind, {"action": "switch.turn_on", **_SELECTIONS[form]}))
    assert "switch.turn_on" in error and relay not in error and "lock" not in error, error


@pytest.mark.parametrize("form", list(_SELECTIONS))
@pytest.mark.parametrize("kind", KINDS)
async def test_a_switch_action_that_selects_indirectly_is_accepted_in_a_home_with_no_gate(core: HomeAssistant, kind: str, form: str) -> None:
    """Ruling 2's control: the same configs, the same relay, no gated wrapper. A `cover` wrapper is not
    a gate, so a home with one is a home with no gate."""
    await _porch(core)
    relay = await _source(core, "relay")
    await _wrap(core, relay, "cover")

    res = await _validate(core, kind, {"action": "switch.turn_on", **_SELECTIONS[form]})
    assert res.get("status") != "policy", res


@pytest.mark.parametrize("kind", KINDS)
async def test_a_sibling_wrappers_domain_is_refused_indirect_targets_and_others_are_not(core: HomeAssistant, kind: str) -> None:
    """Ruling 10's sibling-wrapper form: a `cover` beside a lock wrapper moves the lock, so `cover.*` by
    area is refused in that home. Control: in the same home a `light.*` by area is accepted, and in a
    home whose lock wrapper has no cover beside it, so is `cover.*` by area."""
    await _porch(core)
    relay = await _source(core, "relay")
    await _wrap(core, relay, "lock")
    by_area = {"target": {"area_id": "porch"}}

    assert (await _validate(core, kind, {"action": "light.turn_on", **by_area}))["ok"] is True
    assert (await _validate(core, kind, {"action": "cover.open_cover", **by_area}))["ok"] is True
    await _wrap(core, relay, "cover")
    error = _refused(await _validate(core, kind, {"action": "cover.open_cover", **by_area}))
    assert "cover.open_cover" in error and "lock" not in error, error
    assert (await _validate(core, kind, {"action": "light.turn_on", **by_area}))["ok"] is True


def _blueprint(hass: HomeAssistant, target: str) -> dict[str, Any]:
    """An automation that is nothing but `use_blueprint`, whose body's `switch.turn_on` target is a template
    built from an input. Raw, the config has no action; validated, the target is a compiled `Template`."""
    import pathlib  # noqa: PLC0415

    d = pathlib.Path(hass.config.path("blueprints/automation/hearth_gate"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "p.yaml").write_text(
        "blueprint:\n  name: probe\n  domain: automation\n  input:\n    w:\n      name: w\n"
        "triggers:\n  - trigger: time\n    at: '07:00:00'\n"
        "variables:\n  w: !input w\n"
        "actions:\n  - action: switch.turn_on\n" + target
    )
    return {"alias": "looks harmless", "use_blueprint": {"path": "hearth_gate/p.yaml", "input": {"w": "re" + "lay"}}}


@pytest.mark.parametrize("shape", ["carrier", "value"])
async def test_a_blueprint_whose_target_is_a_template_is_refused_in_a_gated_home(core: HomeAssistant, shape: str) -> None:
    """Security on db974825: in the validated pass a blueprint's templates are `Template` objects, not
    strings, so a check reading only `str`/`list`/`dict` saw no target at all. Either shape, a whole
    templated `target` or a templated `entity_id`, is unknowable. Control: the same blueprint in a home
    with no gated wrapper validates."""
    target = {
        "carrier": "    target: \"{{ {'entity_id': 'switch.' ~ w} }}\"\n",
        "value": "    target:\n      entity_id: \"{{ 'switch.' ~ w }}\"\n",
    }[shape]
    relay = await _source(core, "relay")
    cfg = _blueprint(core, target)
    d = build_dispatcher(core)
    assert (await d.dispatch("automations.validate", {"config": cfg}))["ok"] is True

    await _wrap(core, relay, "lock")
    error = _refused(await d.dispatch("automations.validate", {"config": cfg}))
    assert "switch.turn_on" in error and relay not in error and "lock" not in error, error


async def test_a_lock_relay_named_inside_a_blueprints_template_is_refused(core: HomeAssistant) -> None:
    """The named-anywhere scan reads a compiled `Template`'s text too: the raw config is only
    `use_blueprint`, and the relay appears in the validated body as a `Template`, not a string.
    Control: before the relay is wrapped, the same blueprint validates."""
    relay = await _source(core, "relay")
    cfg = _blueprint(core, "    target:\n      entity_id: input_boolean.test\n")
    import pathlib  # noqa: PLC0415

    body = pathlib.Path(core.config.path("blueprints/automation/hearth_gate/p.yaml"))
    body.write_text(body.read_text().replace("actions:\n", f"conditions:\n  - condition: template\n    value_template: \"{{{{ is_state('{relay}', 'on') }}}}\"\nactions:\n"))
    cfg["use_blueprint"]["input"] = {"w": "x"}
    d = build_dispatcher(core)
    assert (await d.dispatch("automations.validate", {"config": cfg}))["ok"] is True

    await _wrap(core, relay, "lock")
    assert _refused(await d.dispatch("automations.validate", {"config": cfg})) == GATED_NAMED


# --------------------------------------------------------------------------- scenes


async def test_a_scene_that_sets_a_gated_relay_is_not_written(core: HomeAssistant, tmp_path: Any) -> None:
    """A scene's own `entities` keys are what it sets. A lock's relay is refused in a ghost-safe
    sentence, a valve's naming its gate; nothing is written. Control: a plain switch is written."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await _wrap(core, tap, "valve")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("scenes.create", {"config": {"name": "Door", "entities": {relay: "off"}}}))
    assert got is not None and got[0] == "validation_failed" and relay not in got[1] and "lock" not in got[1], got
    assert GATED_NAMED in got[1]
    got = await _err(d.dispatch("scenes.create", {"config": {"name": "Water", "entities": {tap: "off"}}}))
    assert got is not None and got[0] == "validation_failed" and tap in got[1] and "valve" in got[1], got
    assert (tmp_path / "scenes.yaml").read_text().strip() == "[]"
    assert await _err(d.dispatch("scenes.create", {"config": {"name": "Lamp", "entities": {lamp: "on"}}})) is None


# --------------------------------------------------------------------------- config-flow form values (ruling 6)


@pytest.mark.parametrize("path", ["helpers.create", "integrations.flow_step"])
async def test_a_group_helper_over_a_gated_relay_is_not_made(core: HomeAssistant, path: str) -> None:
    """Ruling 6: the flow-input check refuses a form value that names a gated id, a valve's included,
    since a group over the relay is one more way to move it. Control: a group of plain switches is made."""
    relay = await _source(core, "relay")
    tap = await _source(core, "tap")
    a = await _source(core, "a")
    b = await _source(core, "b")
    await _wrap(core, relay, "lock")
    await _wrap(core, tap, "valve")
    d = build_dispatcher(core)

    async def make(members: list[str]) -> tuple[str, str] | None:
        config = {"name": "Outside", "entities": members}
        if path == "helpers.create":
            return await _err(d.dispatch("helpers.create", {"domain": "group", "variant": "switch", "config": config}))
        started = await d.dispatch("integrations.flow_start", {"domain": "group"})
        try:
            menu = await d.dispatch("integrations.flow_step", {"flow_id": started["flow_id"], "input": {"next_step_id": "switch"}})
            assert menu.get("step_id") == "switch", menu
            return await _err(d.dispatch("integrations.flow_step", {"flow_id": started["flow_id"], "input": config}))
        finally:
            await _err(d.dispatch("integrations.flow_abort", {"flow_id": started["flow_id"]}))

    before = len(core.config_entries.async_entries("group"))
    got = await make([a, relay])
    assert got is not None and got[0] == "validation_failed" and relay not in got[1] and "lock" not in got[1], got
    got = await make([a, tap])
    assert got is not None and got[0] == "validation_failed" and tap in got[1] and "valve" in got[1], got
    assert len(core.config_entries.async_entries("group")) == before
    assert await make([a, b]) is None
    assert len(core.config_entries.async_entries("group")) == before + 1


async def test_a_template_switch_whose_action_reaches_a_gated_relay_is_not_made(core: HomeAssistant) -> None:
    """Ruling 6's stated limit is an owner's template switch; one the model writes is caught here, by
    name, by registry id, and by a target that cannot be known, all in a gated home."""
    await _porch(core)
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    d = build_dispatcher(core)
    relay_uuid = er.async_get(core).async_get(relay).id

    for target in ({"entity_id": relay}, {"entity_id": relay_uuid}, {"entity_id": "{{ 'switch.' ~ 'relay' }}"}, {"area_id": "porch"}):
        config = {"name": "Porch", "turn_on": [{"action": "switch.turn_on", "target": target}]}
        with pytest.raises(RpcError) as ei:
            await d.dispatch("helpers.create", {"domain": "template", "variant": "switch", "config": config})
        assert ei.value.code == "validation_failed" and relay not in ei.value.message and "lock" not in ei.value.message, (target, ei.value.message)
    assert core.config_entries.flow.async_progress() == []
    made = await d.dispatch("helpers.create", {"domain": "template", "variant": "switch", "config": {"name": "Lamp", "turn_on": [_on(lamp)]}})
    assert made["entities"], made


# --------------------------------------------------------------------------- a started script


async def _owner_scripts(hass: HomeAssistant, tmp_path: Any, body: str) -> None:
    (tmp_path / "scripts.yaml").write_text(body)
    assert await async_setup_component(hass, "script", {})
    await hass.services.async_call("script", "reload", blocking=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize("kind", KINDS)
async def test_starting_an_owner_script_that_moves_a_lock_relay_is_refused(core: HomeAssistant, tmp_path: Any, kind: str) -> None:
    """`find_nested_script_violations` walked a started script with the literal walk only, so an owner
    script that turns on the relay was a way round the check above. It is asked what the script can do,
    and that includes ruling 2's indirect target. Control: a script that turns on a plain switch."""
    await _porch(core)
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await _owner_scripts(
        core,
        tmp_path,
        f"door:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: {relay}\n"
        "porch:\n  sequence:\n  - action: switch.turn_on\n    target:\n      area_id: porch\n"
        f"lamp:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: {lamp}\n",
    )

    def start(script: str) -> dict[str, Any]:
        return {"action": "script.turn_on", "target": {"entity_id": script}}

    assert _refused(await _validate(core, kind, start("script.door"))) == GATED_NAMED
    error = _refused(await _validate(core, kind, start("script.porch")))
    assert "area_id" in error and relay not in error and "lock" not in error, error
    assert (await _validate(core, kind, start("script.lamp")))["ok"] is True


async def test_a_scene_script_that_starts_a_script_reaching_the_lock_relay_is_not_run(core: HomeAssistant, tmp_path: Any) -> None:
    """Nested-script reach from a scene-script: a Hearth-labelled script is checked when it runs (ruling
    7), and that check did not follow `script.turn_on`. It now asks a started script the gate half of
    the authoring question. Control: the same scene-script starting a script that turns on a plain
    switch runs."""
    relay = await _source(core, "relay")
    lamp = await _source(core, "lamp")
    await _wrap(core, relay, "lock")
    await hl.async_ensure_labels(core)
    await _owner_scripts(
        core,
        tmp_path,
        f"door:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: {relay}\n"
        f"lamp:\n  sequence:\n  - action: switch.turn_on\n    target:\n      entity_id: {lamp}\n"
        "evening:\n  sequence:\n  - action: script.turn_on\n    target:\n      entity_id: script.door\n"
        "calm:\n  sequence:\n  - action: script.turn_on\n    target:\n      entity_id: script.lamp\n",
    )
    for script in ("script.evening", "script.calm"):
        _bare(core, script)
        await hl.async_mark_hearth_scene(core, script, True)
    switched = _mock_switch(core, "turn_on")
    d = build_dispatcher(core)

    got = await _err(d.dispatch("scripts.run", {"entity_id": "script.evening"}))
    assert got is not None and got[0] == "method_not_allowed", got
    assert relay not in got[1] and "lock" not in got[1], got
    await core.async_block_till_done()
    assert not switched
    assert await _err(d.dispatch("scripts.run", {"entity_id": "script.calm"})) is None
    await core.async_block_till_done()
    assert _calls(switched) == [("turn_on", [lamp])]


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize("started", ["missing", "unresolved"])
async def test_a_scene_script_runs_a_started_script_the_authoring_check_would_fail_closed_on(
    core: HomeAssistant, tmp_path: Any, gated: bool, started: str
) -> None:
    """Code review on db974825: the run check asks a started script only the gate half. Authoring fails
    closed on a script Home Assistant does not have and on a registry id that resolves to nothing; at
    run time AgDR-0044 and #663 let those run, in a home with a gated wrapper or without one, and
    nothing here may change that. A started script that is missing is skipped; one holding an
    unresolved registry id is walked and finds nothing gated."""
    lamp = await _source(core, "lamp")
    if gated:
        relay = await _source(core, "relay")
        await _wrap(core, relay, "lock")
    await hl.async_ensure_labels(core)
    body = "evening:\n  sequence:\n  - action: script.turn_on\n    target:\n      entity_id: script.other\n"
    if started == "unresolved":
        body += (
            "other:\n  sequence:\n  - action: switch.turn_on\n    continue_on_error: true\n    target:\n      entity_id: 0123456789abcdef0123456789abcdef\n"
            f"  - action: switch.turn_on\n    target:\n      entity_id: {lamp}\n"
        )
    else:
        body = body.replace("script.other", "script.missing")
    await _owner_scripts(core, tmp_path, body)
    _bare(core, "script.evening")
    await hl.async_mark_hearth_scene(core, "script.evening", True)
    _mock_switch(core, "turn_on")

    assert await _err(build_dispatcher(core).dispatch("scripts.run", {"entity_id": "script.evening"})) is None
    await core.async_block_till_done()


@pytest.mark.parametrize("kind", KINDS)
async def test_scene_data_that_sets_a_valve_relay_or_a_cover_beside_a_valve_is_refused(core: HomeAssistant, kind: str) -> None:
    """The scene-data branch of the acted-on check. A valve gate is not refused for being named, so only
    that branch sees `scene.apply` setting a valve's relay, or a `cover` beside a valve wrapper, and
    `scene.create` snapshotting one. Control: the same services on a plain switch are accepted."""
    tap = await _source(core, "tap")
    lamp = await _source(core, "lamp")
    await _wrap(core, tap, "valve")
    cover = _wrapper(core, await _wrap(core, tap, "cover"))

    for eid in (tap, cover):
        for step in (
            {"action": "scene.apply", "data": {"entities": {eid: "off"}}},
            {"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": [eid]}},
        ):
            error = _refused(await _validate(core, kind, step))
            assert eid in error and "valve" in error, (step, error)
    assert (await _validate(core, kind, {"action": "scene.apply", "data": {"entities": {lamp: "off"}}}))["ok"] is True
    assert (await _validate(core, kind, {"action": "scene.create", "data": {"scene_id": "x", "snapshot_entities": [lamp]}}))["ok"] is True


# --------------------------------------------------------------------------- ruling 9: group membership is never changed from a config


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("service", ["set", "remove", "reload"])
async def test_a_routine_may_not_change_a_groups_members(core: HomeAssistant, kind: str, service: str) -> None:
    """In `DENIED_ACTIONS` on both sides; `POLICY_WALK_CASES` pins the hub's verdict. Here, the box's,
    in a home with no gate at all, since this refusal does not depend on one."""
    step = {"action": f"group.{service}", "data": {"object_id": "outside", "add_entities": ["switch.a"]} if service == "set" else {}}
    error = _refused(await _validate(core, kind, step))
    assert f"group.{service} is not allowed" in error, error
