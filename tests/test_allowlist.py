"""The Python allowlist must equal the TypeScript one (packages/shared/schema/methods.json)."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.hearth_ai.handlers.registry import ATTRIBUTE_DENYLIST
from custom_components.hearth_ai.const import (
    CAPABILITIES,
    CAPABILITIES_VERSION,
    CAPABILITY_FOR_METHOD,
    OPT_IN_CAPABILITIES,
)
from custom_components.hearth_ai.rpc import ALLOWED_METHODS, ERROR_CODES

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"


def _load() -> dict:
    assert SCHEMA.is_file(), "run `pnpm --filter @hearth/shared export:jsonschema` first"
    return json.loads(SCHEMA.read_text())


def test_allowlist_matches_shared() -> None:
    data = _load()
    assert set(data["hub_to_ha_allowlist"]) == ALLOWED_METHODS


def test_error_codes_match_shared() -> None:
    data = _load()
    assert set(data["error_codes"]) == ERROR_CODES


def test_attribute_denylist_matches_shared() -> None:
    data = _load()
    assert set(data["attribute_denylist"]) == ATTRIBUTE_DENYLIST


def test_control_is_limited_to_opt_in_methods() -> None:
    """Operating the home is possible, but only through these three opt-in methods."""
    control = {m for m in ALLOWED_METHODS if CAPABILITY_FOR_METHOD[m] in OPT_IN_CAPABILITIES}
    assert control == {"devices.call", "scenes.activate", "scripts.run"}
    for m in ALLOWED_METHODS:
        for banned in ("lock", "camera", "alarm", "shell", "restart"):
            assert banned not in m, m
        if m not in control:
            for banned in ("call_service", "trigger", "turn_on", "turn_off"):
                assert banned not in m, m
    # automations can still only be authored, never fired
    assert "automations.trigger" not in ALLOWED_METHODS


def test_opt_in_capabilities_match_shared() -> None:
    data = _load()
    assert set(data["opt_in_capabilities"]) == OPT_IN_CAPABILITIES


def test_capability_table_matches_shared() -> None:
    data = _load()
    assert list(data["capabilities"]) == list(CAPABILITIES)
    assert data["capability_for_method"] == CAPABILITY_FOR_METHOD
    assert data["capabilities_version"] == CAPABILITIES_VERSION
    assert set(CAPABILITY_FOR_METHOD) == ALLOWED_METHODS
