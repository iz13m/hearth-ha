"""Policy lists match the shared package and denied actions are refused before any write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hearth_ai.policy import (
    DENIED_ACTION_DOMAINS,
    DENIED_ACTIONS,
    DENIED_ENTITY_DOMAINS,
    find_policy_violations,
    find_scene_policy_violations,
)

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def test_lists_match_shared() -> None:
    data = json.loads(SCHEMA.read_text())
    assert set(data["denied_action_domains"]) == DENIED_ACTION_DOMAINS
    assert set(data["denied_actions"]) == DENIED_ACTIONS
    assert set(data["denied_entity_domains"]) == DENIED_ENTITY_DOMAINS


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
