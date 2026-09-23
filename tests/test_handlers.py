"""End-to-end handler tests against a real (test) Home Assistant core."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

AUTOMATION = {
    "alias": "Hearth test automation",
    "description": "created by tests",
    "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
    "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.test"}}],
    "mode": "single",
}


async def test_catalog(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    entities = await d.dispatch("entities.list", {"domain": "input_boolean"})
    assert [e["entity_id"] for e in entities] == ["input_boolean.test"]
    state = await d.dispatch("states.get", {"entity_id": "input_boolean.test"})
    assert state["state"] == "off"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.get", {"entity_id": "lock.front_door"})
    assert ei.value.code == "not_found"
    services = await d.dispatch("services.list", {"domain": "input_boolean"})
    assert any(s["service"] == "turn_on" for s in services)
    assert await d.dispatch("areas.list", {}) == []


async def test_areas_carry_their_floor(core: HomeAssistant) -> None:
    """The layout editor stacks storeys by `level`; a slug alone does not say which is upstairs."""
    from homeassistant.helpers import area_registry as ar, floor_registry as fr

    floors = fr.async_get(core)
    ground = floors.async_create("Ground", level=0)
    upstairs = floors.async_create("Upstairs", level=1)
    unordered = floors.async_create("Garden room")  # no level set
    areas = ar.async_get(core)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    areas.async_create("Studio", floor_id=unordered.floor_id)
    areas.async_create("Shed")  # on no floor

    d = build_dispatcher(core)
    by_name = {a["name"]: a for a in await d.dispatch("areas.list", {})}

    assert by_name["Kitchen"]["floor_level"] == 0
    assert by_name["Kitchen"]["floor_name"] == "Ground"
    assert by_name["Bedroom"]["floor_level"] == 1
    # A floor without a level reports none, rather than a guessed one.
    assert by_name["Studio"]["floor_name"] == "Garden room"
    assert by_name["Studio"]["floor_level"] is None
    assert by_name["Shed"]["floor_id"] is None
    assert by_name["Shed"]["floor_name"] is None
    assert by_name["Shed"]["floor_level"] is None


async def test_automation_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    assert (await d.dispatch("automations.validate", {"config": AUTOMATION}))["ok"] is True
    bad = await d.dispatch("automations.validate", {"config": {"alias": "x", "triggers": "nope"}})
    assert bad["ok"] is False

    created = await d.dispatch("automations.create", {"config": AUTOMATION})
    await core.async_block_till_done()
    auto_id = created["id"]
    assert created["entity_id"] == "automation.hearth_test_automation"
    assert (tmp_path / "automations.yaml").is_file()
    assert core.states.get("automation.hearth_test_automation") is not None

    listed = await d.dispatch("automations.list", {})
    assert [a for a in listed if a["id"] == auto_id][0]["editable"] is True

    got = await d.dispatch("automations.get", {"id": auto_id})
    assert got["config"]["alias"] == "Hearth test automation"
    assert got["config"]["id"] == auto_id

    updated = await d.dispatch("automations.update", {"id": auto_id, "config": {**AUTOMATION, "alias": "Renamed"}})
    await core.async_block_till_done()
    assert updated["id"] == auto_id
    assert (await d.dispatch("automations.get", {"id": auto_id}))["config"]["alias"] == "Renamed"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.update", {"id": "missing", "config": AUTOMATION})
    assert ei.value.code == "not_found"

    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": {"alias": "broken", "triggers": [{"trigger": "nope"}]}})
    assert ei.value.code == "validation_failed"

    await d.dispatch("automations.delete", {"id": auto_id})
    await core.async_block_till_done()
    with pytest.raises(RpcError):
        await d.dispatch("automations.get", {"id": auto_id})


async def test_scene_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    created = await d.dispatch("scenes.create", {"config": {"name": "Movie night", "entities": {"input_boolean.test": "on"}}})
    await core.async_block_till_done()
    assert created["entity_id"] == "scene.movie_night"
    assert (tmp_path / "scenes.yaml").is_file()
    got = await d.dispatch("scenes.get", {"id": created["id"]})
    assert got["config"]["name"] == "Movie night"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scenes.create", {"config": {"entities": "not-a-dict"}})
    assert ei.value.code == "validation_failed"
    await d.dispatch("scenes.delete", {"id": created["id"]})


async def test_script_create_same_name_concurrently(core: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two creates with one name at once get two keys, and neither overwrites the other (#173)."""
    from custom_components.hearth_ai.handlers import scripts

    # Hold each create at validation until both have got there — the point where the key used to be
    # chosen already and the lock released — so the two really overlap rather than run in turn.
    real_validate = scripts._validate
    arrived = 0
    both = asyncio.Event()

    async def validate_together(hass, key, config):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return await real_validate(hass, key, config)

    monkeypatch.setattr(scripts, "_validate", validate_together)
    d = build_dispatcher(core)
    cfg = {"alias": "Race", "sequence": [{"action": "input_boolean.toggle", "target": {"entity_id": "input_boolean.test"}}]}
    other = {**cfg, "description": "the second one"}
    first, second = await asyncio.gather(
        d.dispatch("scripts.create", {"config": cfg}),
        d.dispatch("scripts.create", {"config": other}),
    )
    await core.async_block_till_done()
    assert {first["id"], second["id"]} == {"race", "race_2"}
    # Each key holds the body that was sent for it.
    assert (await d.dispatch("scripts.get", {"id": first["id"]}))["config"].get("description") is None
    assert (await d.dispatch("scripts.get", {"id": second["id"]}))["config"]["description"] == "the second one"
    await d.dispatch("scripts.delete", {"id": "race"})
    await d.dispatch("scripts.delete", {"id": "race_2"})


async def test_script_crud(core: HomeAssistant, tmp_path: Path) -> None:
    d = build_dispatcher(core)
    cfg = {"alias": "Blink", "sequence": [{"action": "input_boolean.toggle", "target": {"entity_id": "input_boolean.test"}}]}
    assert (await d.dispatch("scripts.validate", {"config": cfg}))["ok"] is True
    created = await d.dispatch("scripts.create", {"config": cfg})
    await core.async_block_till_done()
    assert created["id"] == "blink"
    assert created["entity_id"] == "script.blink"
    # second create with the same alias gets a suffixed id
    created2 = await d.dispatch("scripts.create", {"config": cfg})
    await core.async_block_till_done()
    assert created2["id"] == "blink_2"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.get", {"id": "Not A Slug"})
    assert ei.value.code == "invalid_params"
    await d.dispatch("scripts.delete", {"id": "blink"})
    await d.dispatch("scripts.delete", {"id": "blink_2"})
    assert (await d.dispatch("scripts.list", {})) == []


async def test_no_service_calls_leak(core: HomeAssistant) -> None:
    """Even a read-only catalog call must never invoke a service."""
    calls = async_mock_service(core, "input_boolean", "turn_on")
    d = build_dispatcher(core)
    await d.dispatch("services.list", {})
    await d.dispatch("entities.list", {})
    assert calls == []


async def test_assist_exposure_is_honoured(core: HomeAssistant) -> None:
    d = build_dispatcher(core)
    assert [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "input_boolean"})] == ["input_boolean.test"]
    async_expose_entity(core, "conversation", "input_boolean.test", False)
    assert await d.dispatch("entities.list", {"domain": "input_boolean"}) == []
    with pytest.raises(RpcError) as ei:
        await d.dispatch("states.get", {"entity_id": "input_boolean.test"})
    assert ei.value.code == "not_found"
    async_expose_entity(core, "conversation", "input_boolean.test", True)
    assert (await d.dispatch("states.get", {"entity_id": "input_boolean.test"}))["state"] == "off"


async def test_entities_list_pages_by_keyset(core: HomeAssistant) -> None:
    """#12: the 500 cap used to be the end of the list, with no way to read past it."""
    d = build_dispatcher(core)
    # Lights are exposed to Assist by default, so plain states are enough; no registry entry needed.
    for name in ("delta", "alpha", "charlie", "bravo", "echo"):
        core.states.async_set(f"light.{name}", "off")
    await core.async_block_till_done()

    all_ids = [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "light"})]
    assert all_ids == sorted(all_ids)
    assert len(all_ids) == 5

    # Walk it two at a time, the way the hub does: a short page is the last one.
    seen: list[str] = []
    after: str | None = None
    for _ in range(10):
        params: dict[str, object] = {"domain": "light", "limit": 2}
        if after is not None:
            params["after"] = after
        page = await d.dispatch("entities.list", params)
        seen.extend(e["entity_id"] for e in page)
        if len(page) < 2:
            break
        after = page[-1]["entity_id"]

    # Every entity exactly once, in order, and no page repeated the cursor's own row.
    assert seen == all_ids

    # A cursor past the end is empty, not an error, and one before the start changes nothing.
    assert await d.dispatch("entities.list", {"domain": "light", "after": "zzz.zzz"}) == []
    assert [e["entity_id"] for e in await d.dispatch("entities.list", {"domain": "light", "after": "aaa.aaa"})] == all_ids

    with pytest.raises(RpcError) as ei:
        await d.dispatch("entities.list", {"after": 7})
    assert ei.value.code == "invalid_params"


async def test_refuses_a_script_that_turns_on_a_scene_setting_a_lock(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The last way round the promise that a lock is refused to every model (#126).

    `scene.turn_on` is an allowed action, its target is in `scene`, and what the scene *contains* is
    only knowable here — so the hub cannot decide it and `find_policy_violations` never saw it. A
    scene the owner wrote by hand could set a lock, and a script Hearth wrote could activate it.
    Scenes are checked when they *run* (AgDR-0012), but a script is not, which is why this is
    checked when the script is *written*.
    """
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    core.states.async_set("scene.movie_night", "unknown", {"friendly_name": "Movie night", "entity_id": ["light.hall"]})
    d = build_dispatcher(core)

    nested = {"alias": "Let me in", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.open_the_gate"}}]}
    res = await d.dispatch("scripts.validate", {"key": "let_me_in", "config": nested})
    assert res["ok"] is False and res["status"] == "policy"
    assert "lock.gate" in res["error"]
    with pytest.raises(RpcError) as ei:
        await d.dispatch("scripts.create", {"key": "let_me_in", "config": nested})
    assert ei.value.code == "validation_failed"
    assert "scripts.yaml" not in [p.name for p in tmp_path.iterdir()] or not (tmp_path / "scripts.yaml").read_text().strip("{}\n")

    # Nested in a branch, and reached through an automation, which is the default-on capability.
    buried = {
        "alias": "Let me in quietly",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
        "actions": [{"choose": [{"conditions": [], "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.open_the_gate"}}]}]}],
    }
    res = await d.dispatch("automations.validate", {"config": buried})
    assert res["ok"] is False and res["status"] == "policy"

    # A target that names no scene is refused rather than ignored: `all` is ENTITY_MATCH_ALL, which
    # is every scene in the house including the gate, and an area cannot be resolved to scenes here.
    for target in ({"entity_id": "all"}, {"area_id": "hall"}, {"device_id": "abc"}):
        res = await d.dispatch("scripts.validate", {"key": "sweep", "config": {"alias": "Sweep", "sequence": [{"action": "scene.turn_on", "target": target}]}})
        assert res["ok"] is False and res["status"] == "policy", target

    # A scene of lights is the ordinary case and must still be writable.
    fine = {"alias": "Film time", "sequence": [{"action": "scene.turn_on", "target": {"entity_id": "scene.movie_night"}}]}
    assert (await d.dispatch("scripts.validate", {"key": "film_time", "config": fine}))["ok"] is True


async def test_labels_a_routine_as_a_hearth_scene_and_reports_it(core: HomeAssistant, tmp_path: Path) -> None:
    """
    A scene with steps is stored as a **labelled script** (AgDR-0044), so saving one has to put the
    label on, and the lists have to say which routines carry it — that is how the Scenes tab knows a
    script belongs in it.
    """
    from custom_components.hearth_ai import labels as hl

    await hl.async_ensure_labels(core)
    d = build_dispatcher(core)
    config = {"alias": "Evening", "sequence": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.test"}}]}

    created = await d.dispatch("scripts.create", {"key": "evening", "config": config, "hearth_scene": True})
    await core.async_block_till_done()
    entity_id = created["entity_id"]
    assert entity_id and hl.is_hearth_scene(core, entity_id)
    listed = await d.dispatch("scripts.list", {})
    assert next(s for s in listed if s["entity_id"] == entity_id)["hearth_scene"] is True

    # Absent means leave it alone: an ordinary save must not silently take the label off.
    await d.dispatch("scripts.update", {"id": "evening", "config": config})
    await core.async_block_till_done()
    assert hl.is_hearth_scene(core, entity_id) is True

    # And false takes it off, which is how a scene stops being one.
    await d.dispatch("scripts.update", {"id": "evening", "config": config, "hearth_scene": False})
    await core.async_block_till_done()
    assert hl.is_hearth_scene(core, entity_id) is False

    # An automation carries it too: that is a scene's schedule.
    auto = await d.dispatch("automations.create", {"config": AUTOMATION, "hearth_scene": True})
    await core.async_block_till_done()
    assert hl.is_hearth_scene(core, auto["entity_id"])
    rows = await d.dispatch("automations.list", {})
    assert next(a for a in rows if a["entity_id"] == auto["entity_id"])["hearth_scene"] is True


async def test_switches_an_automation_on_and_off_without_ever_running_it(core: HomeAssistant, tmp_path: Path) -> None:
    """
    `automations.set_enabled` is the one place Hearth touches an automation's state (AgDR-0044).

    Two things matter and neither is the happy path: it must never *trigger* the automation, and it
    must refuse an automation Hearth cannot edit — one from a package or a blueprint is the owner's
    own arrangement, not Hearth's to switch.
    """
    d = build_dispatcher(core)
    created = await d.dispatch("automations.create", {"config": AUTOMATION})
    await core.async_block_till_done()
    key, entity_id = created["id"], created["entity_id"]

    triggered = async_mock_service(core, "input_boolean", "turn_off")

    off = await d.dispatch("automations.set_enabled", {"id": key, "enabled": False})
    await core.async_block_till_done()
    assert off["state"] == "off"
    on = await d.dispatch("automations.set_enabled", {"id": key, "enabled": True})
    await core.async_block_till_done()
    assert on["state"] == "on"
    assert on["entity_id"] == entity_id
    # Switching a rule is not running it.
    assert triggered == []

    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.set_enabled", {"id": "not-in-our-file", "enabled": False})
    assert ei.value.code == "not_found"


async def test_refuses_the_scene_shorthand_that_activates_a_scene_setting_a_lock(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The same bypass as #126, reached through an action *type* rather than a service (#228).

    `{scene: "scene.gate"}` is not a service call at all — `cv.ACTIONS_MAP` maps `scene` to its own
    action type and `determine_script_action` returns `"scene"` — so it activates a scene while
    naming no service, and every check keyed on a service name skipped it. It is the one nested
    shape generic recursion cannot catch, because the id it names is a *scene*, which is an allowed
    reference; only resolving the scene's contents here reaches the lock.
    """
    core.states.async_set("scene.open_the_gate", "unknown", {"friendly_name": "Open the gate", "entity_id": ["lock.gate"]})
    core.states.async_set("scene.movie_night", "unknown", {"friendly_name": "Movie night", "entity_id": ["light.hall"]})
    d = build_dispatcher(core)

    shorthand = {"alias": "Let me in", "sequence": [{"scene": "scene.open_the_gate"}]}
    res = await d.dispatch("scripts.validate", {"key": "let_me_in", "config": shorthand})
    assert res["ok"] is False and res["status"] == "policy"
    assert "lock.gate" in res["error"]
    with pytest.raises(RpcError):
        await d.dispatch("scripts.create", {"key": "let_me_in", "config": shorthand})

    # Buried in a branch, reached through the default-on automation capability.
    buried = {
        "alias": "Quietly",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.test", "to": "on"}],
        "actions": [{"choose": [{"conditions": [], "sequence": [{"scene": "scene.open_the_gate"}]}]}],
    }
    res = await d.dispatch("automations.validate", {"config": buried})
    assert res["ok"] is False and res["status"] == "policy"

    # Mixed case, for the same reason #220 folds: HA lower-cases the id itself.
    mixed = {"alias": "Shout", "sequence": [{"scene": "Scene.Open_The_Gate"}]}
    res = await d.dispatch("scripts.validate", {"key": "shout", "config": mixed})
    assert res["ok"] is False and res["status"] == "policy"

    # `all` through the shorthand: ENTITY_MATCH_ALL is every scene in the house, including the gate
    # one. Covered today only because the shorthand normalises to `scene.turn_on` and inherits that
    # rule — pinned here so a change to the normalisation cannot drop it silently. Folded, since HA
    # lower-cases the id itself (#220).
    # `comp_entity_ids` is `Any(All(Lower, Any("all", "none")), entity_ids)` — the sentinel is
    # **case-insensitive**, so `ALL` folds to `all` and must be refused with it.
    for value in ("all", "ALL"):
        res = await d.dispatch("scripts.validate", {"key": "everything", "config": {"alias": "All", "sequence": [{"scene": value}]}})
        assert res["ok"] is False and res["status"] == "policy", value
    # `none` is the other sentinel. It is **also** refused — not by the `all` rule but by the
    # pre-existing "only a named scene entity may be activated" one, which predates #228. Pinned as
    # it actually behaves rather than as I first assumed: refusing it is over-refusal of a no-op,
    # which costs a household nothing real, and special-casing a sentinel that selects nothing would
    # add a branch to buy nothing. If that ever changes, this test says so deliberately.
    res_none = await d.dispatch("scripts.validate", {"key": "nothing", "config": {"alias": "None", "sequence": [{"scene": "none"}]}})
    assert res_none["ok"] is False and "only a named scene" in res_none["error"], res_none

    # A benign scene through the shorthand still works — this narrows, it does not ban the shape.
    ok = {"alias": "Film", "sequence": [{"scene": "scene.movie_night"}]}
    assert (await d.dispatch("scripts.validate", {"key": "film", "config": ok}))["ok"] is True


async def test_refuses_a_config_that_starts_a_script_reaching_a_lock(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The last container without a mechanism (#225): a script is not checked when it runs (AgDR-0005).

    `script.turn_on` at a script whose body unlocks a door is `scene.turn_on` before #126, one name
    removed. The five shapes here are the ones #220 deferred — including the **per-script service**
    `script.<id>`, which names no target at all, and the legacy `data`/top-level `entity_id` forms.

    `script.evening` is the control that matters: `sceneSchedule.ts` compiles a converted scene's
    schedule to exactly `script.turn_on` at a `script.*` id, so a blunt `script.*` refusal would
    break Hearth's own feature. It must stay allowed.
    """
    (tmp_path / "scripts.yaml").write_text(
        "gate_opener:\n"
        "  alias: Gate opener\n"
        "  sequence:\n"
        "  - action: lock.unlock\n"
        "    target:\n"
        "      entity_id: lock.gate\n"
        "evening:\n"
        "  alias: Evening\n"
        "  sequence:\n"
        "  - action: light.turn_on\n"
        "    target:\n"
        "      entity_id: light.hall\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    d = build_dispatcher(core)

    deferred = [
        {"action": "script.turn_on", "target": {"entity_id": "script.gate_opener"}},
        {"action": "script.toggle", "target": {"entity_id": "script.gate_opener"}},
        {"action": "script.gate_opener"},
        {"action": "script.turn_on", "data": {"entity_id": "script.gate_opener"}},
        {"action": "script.turn_on", "entity_id": "script.gate_opener"},
    ]
    for node in deferred:
        cfg = {"alias": "Let me in", "triggers": [{"trigger": "time_pattern", "seconds": "/5"}], "actions": [node]}
        res = await d.dispatch("automations.validate", {"config": cfg})
        assert res["ok"] is False and res["status"] == "policy", node
        assert "lock.gate" in res["error"], node

    # Mixed case, and buried in a branch.
    buried = {"alias": "Quietly", "triggers": [{"trigger": "sun", "event": "sunset"}],
              "actions": [{"choose": [{"conditions": [], "sequence": [{"action": "Script.Turn_On", "target": {"entity_id": "Script.Gate_Opener"}}]}]}]}
    assert (await d.dispatch("automations.validate", {"config": buried}))["ok"] is False

    # Fail closed: a script Home Assistant does not have cannot be checked, so it is refused.
    missing = {"alias": "Ghost", "sequence": [{"action": "script.turn_on", "target": {"entity_id": "script.not_loaded"}}]}
    res = await d.dispatch("scripts.validate", {"key": "ghost", "config": missing})
    assert res["ok"] is False and res["status"] == "policy"
    assert "does not have" in res["error"]

    # THE control: #132's converted-scene schedule must still be writable.
    schedule = {
        "alias": "Evening at sunset",
        "triggers": [{"trigger": "sun", "event": "sunset"}],
        "actions": [{"action": "script.turn_on", "target": {"entity_id": "script.evening"}}],
    }
    assert (await d.dispatch("automations.validate", {"config": schedule}))["ok"] is True


async def test_a_script_raw_config_is_blueprint_expanded_but_not_validated(core: HomeAssistant, tmp_path: Path) -> None:
    """
    The Home Assistant behaviour #225's resolver depends on, and it is the opposite of #226's.

    **Scripts:** `raw_config` is already blueprint-**expanded**, so one pass over it sees the
    blueprint's body and no second pass is needed.
    **Automations:** the raw config keeps `use_blueprint`, which is exactly why #226 had to walk
    `async_validate_config_item`'s output as well.

    Same attribute name, opposite behaviour, and nothing but an HA implementation detail stands
    between us and #226's hole reappearing here. If a release stops expanding at this point, the
    resolver would quietly walk a `use_blueprint` stub and find nothing — #226's bug, inside the fix
    for #225. This test is what makes that a failure with a reason rather than a silent reopening.

    The second half matters for a different rule: `raw_config` is pre-**validation**, so a templated
    action name is still a `str` and AgDR-0042's refusal can see it. If HA ever validates earlier it
    becomes a `Template` object and the walk goes blind to it — the same way it would have if #226
    had replaced its raw pass instead of adding to it.
    """
    d = Path(core.config.path("blueprints/script/probe"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "b.yaml").write_text(
        "blueprint:\n  name: gate\n  domain: script\n  input:\n    pause:\n      name: pause\n"
        "sequence:\n"
        "  - action: lock.unlock\n"
        "    target:\n"
        "      entity_id: lock.gate\n"
        "  - delay: !input pause\n"
    )
    (tmp_path / "scripts.yaml").write_text(
        "via_bp:\n  alias: Via blueprint\n  use_blueprint:\n    path: probe/b.yaml\n    input:\n      pause: '00:00:05'\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()

    from homeassistant.components.script import DOMAIN as SCRIPT_DOMAIN

    entity = core.data[SCRIPT_DOMAIN].get_entity("script.via_bp")
    raw = entity.raw_config

    # Expanded: the blueprint's body is here, the reference is not.
    assert "use_blueprint" not in raw, raw
    assert raw["sequence"][0]["action"] == "lock.unlock", raw
    # Informational only — the walk does not need it, because the body is already present. It is a
    # *property*, not a method: a `callable()` guard in the original probe silently took the other
    # branch and printed the right value for the wrong reason, which this assertion caught.
    assert entity.referenced_blueprint == "probe/b.yaml"

    # Not validated: a duration is still the string as written, not a timedelta. The same is true
    # of a templated action name, which is why the policy walk can still see one.
    assert raw["sequence"][1]["delay"] == "00:00:05", raw["sequence"][1]

    # And the resolver really does refuse it, which is the behaviour all of the above protects.
    res = await build_dispatcher(core).dispatch(
        "automations.validate",
        {"config": {"alias": "x", "triggers": [{"trigger": "sun", "event": "sunset"}],
                    "actions": [{"action": "script.turn_on", "target": {"entity_id": "script.via_bp"}}]}},
    )
    assert res["ok"] is False and res["status"] == "policy"
    assert "lock.gate" in res["error"]


async def test_the_script_resolver_answers_every_target_and_scopes_to_what_a_script_does(core: HomeAssistant, tmp_path: Path) -> None:
    """
    Three review findings on #225, two of which are one change.

    **Drop nothing silently.** `_script_targets` used to keep only ids starting `script.`, so
    `entity_id: all` — every script in the house — was never visited, and a non-script id was
    skipped rather than refused, which falsified the claim that it failed closed. The sentinel is
    case-insensitive (`comp_entity_ids` applies `Lower`), so `ALL` must go with `all`.

    **A dict is not resolved.** `config.py` sets `raw_config = dict(config)` *before* validation and
    `_minimal_config` keeps it for a FAILED_BLUEPRINT script, so a script whose `use_blueprint`
    points at a missing blueprint has a good dict holding the *unexpanded* reference.
    `isinstance(raw, dict)` therefore never fired and the resolver walked a stub — #226's bug inside
    the fix for #225.

    **Scope to what a script can do, not what it mentions** — and this is why the two are one
    change. Dropping the reference scan removes the only thing that was catching a stub whose
    *input* names a lock, so `use_blueprint`-means-unresolved has to land with it or that case goes
    from refused to allowed.
    """
    bp = Path(core.config.path("blueprints/script/exists"))
    bp.mkdir(parents=True, exist_ok=True)
    (bp / "b.yaml").write_text(
        "blueprint:\n  name: ok\n  domain: script\n  input:\n    lamp:\n      name: lamp\n"
        "sequence:\n  - action: light.turn_on\n    target:\n      entity_id: !input lamp\n"
    )
    (tmp_path / "scripts.yaml").write_text(
        "gate:\n  sequence:\n  - action: lock.unlock\n    target:\n      entity_id: lock.gate\n"
        "evening:\n  sequence:\n  - action: light.turn_on\n    target:\n      entity_id: light.hall\n"
        "reads_a_lock:\n  sequence:\n  - condition: state\n    entity_id: lock.front_door\n    state: locked\n"
        "  - action: light.turn_on\n    target:\n      entity_id: light.hall\n"
        "mentions_a_lock:\n  sequence:\n  - action: notify.persistent_notification\n    data:\n      message: check lock.front_door please\n"
        "bp_ghost:\n  use_blueprint:\n    path: does_not_exist/ghost.yaml\n    input:\n      lamp: light.hall\n"
        "bp_ok:\n  use_blueprint:\n    path: exists/b.yaml\n    input:\n      lamp: light.hall\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()
    d = build_dispatcher(core)

    async def verdict(node: dict) -> dict:
        return await d.dispatch("automations.validate", {"config": {
            "alias": "x", "triggers": [{"trigger": "sun", "event": "sunset"}], "actions": [node]}})

    # Finding 2 — every spelling of the sentinel, and a target that is not a script at all.
    for node in (
        {"action": "script.turn_on", "target": {"entity_id": "all"}},
        {"action": "script.turn_on", "target": {"entity_id": "ALL"}},
        {"action": "script.toggle", "target": {"entity_id": "all"}},
        {"action": "script.turn_on", "data": {"entity_id": "all"}},
        {"action": "script.turn_on", "target": {"entity_id": ["script.evening", "all"]}},
        {"action": "script.turn_on", "target": {"entity_id": "input_boolean.test"}},
    ):
        assert (await verdict(node))["ok"] is False, node

    # Finding 1 — a missing blueprint leaves an unexpanded stub that is still a dict.
    assert (await verdict({"action": "script.turn_on", "target": {"entity_id": "script.bp_ghost"}}))["ok"] is False
    # ...while a blueprint HA *could* load is expanded, so it resolves and is allowed.
    assert (await verdict({"action": "script.turn_on", "target": {"entity_id": "script.bp_ok"}}))["ok"] is True

    # Finding 3 — the over-refusal controls. A household script that *reads* a lock, or merely
    # names one in free text, must stay callable; refusing it takes every automation with it.
    for key in ("script.reads_a_lock", "script.mentions_a_lock"):
        res = await verdict({"action": "script.turn_on", "target": {"entity_id": key}})
        assert res["ok"] is True, (key, res)

    # And the thing all of the above protects still refuses.
    assert (await verdict({"action": "script.turn_on", "target": {"entity_id": "script.gate"}}))["ok"] is False
    assert (await verdict({"action": "script.turn_on", "target": {"entity_id": "script.evening"}}))["ok"] is True


async def test_a_blueprint_stub_whose_input_names_a_lock_is_refused_by_the_use_blueprint_rule_alone(
    core: HomeAssistant, tmp_path: Path
) -> None:
    """
    **The coupling between two changes, pinned so it cannot be simplified apart.**

    Until #225's review this shape was refused by `find_reference_violations`, which the resolver
    ran over the whole script config and which spotted `lock.gate` sitting in the stub's *input*.
    That scan was removed deliberately — it refused any household script that merely **read** a
    lock's state or named one in free text, which is the largest over-refusal this work produced.

    Removing it left `use_blueprint` present means unresolved as the **only** guard on any blueprint
    stub. So the removal and the replacement are one change, and this test exists to say so in the
    place someone would break them: a future reader who sees `if "use_blueprint" in raw` and reads
    it as belt-and-braces will simplify it away, because the scan that used to back it is long gone
    and nothing else records that it was ever the other guard.

    If this test fails, do not weaken it — the `use_blueprint` rule is load-bearing on its own.
    """
    (tmp_path / "scripts.yaml").write_text(
        "bp_lock_input:\n"
        "  use_blueprint:\n"
        "    path: does_not_exist/ghost.yaml\n"
        "    input:\n"
        "      which: lock.gate\n"
    )
    await core.services.async_call("script", "reload", blocking=True)
    await core.async_block_till_done()

    from custom_components.hearth_ai.handlers.scenes import find_nested_script_violations
    from custom_components.hearth_ai.policy import find_reference_violations

    cfg = {"alias": "x", "actions": [{"action": "script.turn_on", "target": {"entity_id": "script.bp_lock_input"}}]}

    # The resolver refuses it, and the reference scan is no longer what does so.
    assert await find_nested_script_violations(core, cfg) != []
    res = await build_dispatcher(core).dispatch("automations.validate", {"config": dict(cfg, triggers=[{"trigger": "sun", "event": "sunset"}])})
    assert res["ok"] is False and "blueprint" in res["error"], res

    # Proof the old guard is genuinely gone: the scan sees nothing in the *automation* we wrote,
    # because the lock is in the script's stub rather than in this config.
    assert find_reference_violations(cfg, "config") == []
