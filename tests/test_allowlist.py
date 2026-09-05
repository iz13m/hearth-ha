"""The Python allowlist must equal the TypeScript one (packages/shared/schema/methods.json)."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.hearth_ai.handlers.registry import ATTRIBUTE_DENYLIST
from custom_components.hearth_ai.const import CAPABILITIES, CAPABILITIES_VERSION, CAPABILITY_FOR_METHOD
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


def test_no_device_control_in_allowlist() -> None:
    for m in ALLOWED_METHODS:
        for banned in ("call_service", "trigger", "run", "turn_on", "turn_off", "lock", "camera"):
            assert banned not in m, m


def test_capability_table_matches_shared() -> None:
    data = _load()
    assert list(data["capabilities"]) == list(CAPABILITIES)
    assert data["capability_for_method"] == CAPABILITY_FOR_METHOD
    assert data["capabilities_version"] == CAPABILITIES_VERSION
    assert set(CAPABILITY_FOR_METHOD) == ALLOWED_METHODS
