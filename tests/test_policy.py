"""Policy lists match the shared package and denied actions are refused before any write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hearth_ai.policy import (
    DENIED_ACTION_DOMAINS,
    DENIED_ACTIONS,
    DENIED_ENTITY_DOMAINS,
    HOST_SERVICE_DOMAINS,
    REFERENCEABLE_DENIED_DOMAINS,
    find_policy_violations,
    find_reference_violations,
    find_scene_policy_violations,
)

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def test_lists_match_shared() -> None:
    data = json.loads(SCHEMA.read_text())
    assert set(data["denied_action_domains"]) == DENIED_ACTION_DOMAINS
    assert set(data["denied_actions"]) == DENIED_ACTIONS
    assert set(data["denied_entity_domains"]) == DENIED_ENTITY_DOMAINS
    assert set(data["referenceable_denied_domains"]) == REFERENCEABLE_DENIED_DOMAINS
    assert set(data["host_service_domains"]) == HOST_SERVICE_DOMAINS


def test_nested_denied_actions_found() -> None:
    cfg = {
        "alias": "evil",
        "triggers": [{"trigger": "time_pattern", "seconds": "/5"}],
        "actions": [
            {"action": "lock.unlock", "target": {"entity_id": "lock.front"}},
            {"choose": [{"conditions": [], "sequence": [{"service": "shell_command.rm"}]}], "default": [{"action": "homeassistant.restart"}]},
            {"repeat": {"count": 3, "sequence": [{"action": "switch.turn_on", "target": {"entity_id": ["switch.a", "camera.b"]}}]}},
        ],
    }
    v = find_policy_violations(cfg)
    assert any("lock.unlock" in x for x in v)
    assert any("shell_command.rm" in x for x in v)
    assert any("homeassistant.restart" in x for x in v)
    assert any("camera.b" in x for x in v)
    assert find_policy_violations({"alias": "ok", "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.a"}}]}) == []
    assert find_scene_policy_violations({"entities": {"lock.a": "locked"}}) == ["entities.lock.a: denied domain lock"]


@pytest.mark.usefixtures("core")
async def test_handlers_refuse_denied_configs(core, tmp_path) -> None:  # noqa: ANN001
    from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

    d = build_dispatcher(core)
    bad = {"alias": "unlock", "triggers": [{"trigger": "sun", "event": "sunset"}], "actions": [{"action": "lock.unlock", "target": {"entity_id": "lock.front"}}]}
    res = await d.dispatch("automations.validate", {"config": bad})
    assert res["ok"] is False and res["status"] == "policy"
    with pytest.raises(RpcError) as ei:
        await d.dispatch("automations.create", {"config": bad})
    assert ei.value.code == "validation_failed"
    assert not (tmp_path / "automations.yaml").read_text().strip("[]\n")
    with pytest.raises(RpcError):
        await d.dispatch("scripts.create", {"config": {"alias": "x", "sequence": [{"action": "shell_command.x"}]}})
    with pytest.raises(RpcError):
        await d.dispatch("scenes.create", {"config": {"name": "x", "entities": {"lock.a": "unlocked"}}})


def test_a_reference_is_found_wherever_it_hides() -> None:
    """Mirror of the policy.test.ts cases; the two implementations must agree exactly (AgDR-0038)."""
    assert len(find_reference_violations({"entity_id": "lock.front_door"})) == 1
    assert len(find_reference_violations({"sources": ["sensor.a", "camera.porch"]})) == 1
    assert len(find_reference_violations({"entities": {"device_tracker.phone": "home"}})) == 1
    assert len(find_reference_violations({"state": "{{ states('lock.front_door') }}"})) == 1
    assert len(find_reference_violations({"state": "{{ states.lock.front_door.state }}"})) == 1
    assert len(find_reference_violations({"turn_on": [{"action": "shell_command.backup_now"}]})) == 1
    assert len(find_reference_violations({"image": "image.doorbell_last"})) == 1


def test_a_reference_scan_leaves_ordinary_configuration_alone() -> None:
    assert find_reference_violations({"state": "{{ states('sensor.door_lock_battery') }}"}) == []
    assert find_reference_violations({"path": "/config/www/camera.jpg"}) == []
    assert find_reference_violations({"entity_id": "binary_sensor.person_detected"}) == []
    assert find_reference_violations({"state": "{{ states('light.hall') }}"}) == []


def test_a_templated_action_name_is_refused() -> None:
    v = find_policy_violations({"actions": [{"action": "{{ 'lock.unlock' }}"}]})
    assert any("is a template" in x for x in v)
    assert find_policy_violations({"actions": [{"action": "light.turn_on"}]}) == []


def test_the_walkers_agree_on_every_shared_case() -> None:
    """
    The same configs, the same verdicts, in both languages.

    `test_lists_match_shared` pins the denied lists, and the cases below were mirrored by hand —
    which is how `scene.apply` came to be caught on the hub and not here (#126). The corpus in
    `schema/methods.json` carries the verdict the TypeScript walker gives each case, so a rule added
    to one side and forgotten on the other fails this test instead of shipping as a hole.
    """
    cases = json.loads(SCHEMA.read_text())["policy_walk_cases"]
    assert len(cases) > 10, "run pnpm --filter @hearth/shared export:jsonschema first"
    for case in cases:
        assert find_policy_violations(case["config"]) == case["violations"], case["name"]


def test_a_lock_hiding_in_scene_data_is_found() -> None:
    """Mirror of the policy.test.ts cases for #126."""
    apply_lock = {"actions": [{"action": "scene.apply", "data": {"entities": {"lock.front_door": {"state": "unlocked"}}}}]}
    assert any("lock.front_door" in x for x in find_policy_violations(apply_lock))
    assert len(find_policy_violations({"actions": [{"action": "scene.create", "snapshot_entities": ["lock.front_door"]}]})) == 1
    assert find_policy_violations({"actions": [{"action": "scene.apply", "data": {"entities": {"light.lamp": {"state": "on"}}}}]}) == []


def test_a_device_action_in_a_denied_domain_is_refused() -> None:
    v = find_policy_violations({"actions": [{"device_id": "abc", "domain": "lock", "type": "unlock"}]})
    assert v == ["config.actions[0]: device in denied domain lock"]
    assert len(find_policy_violations({"triggers": [{"trigger": "device", "device_id": "a", "domain": "lock", "type": "locked"}]})) == 1
    assert find_policy_violations({"actions": [{"device_id": "abc", "domain": "light", "type": "turn_on"}]}) == []


def test_an_automation_is_never_triggered_or_flipped_from_a_config() -> None:
    for action in ("automation.trigger", "automation.turn_on", "automation.turn_off", "automation.toggle"):
        assert len(find_policy_violations({"actions": [{"action": action}]})) == 1
    assert find_policy_violations({"actions": [{"action": "automation.reload"}]}) == []
