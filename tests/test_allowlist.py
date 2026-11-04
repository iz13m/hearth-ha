"""The Python allowlist must equal the TypeScript one (packages/shared/schema/methods.json)."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.hearth_ai.handlers.flows import DENIED_DOMAINS
from custom_components.hearth_ai.handlers.registry import ATTRIBUTE_DENYLIST
from custom_components.hearth_ai.const import (
    CAPABILITIES,
    CAPABILITIES_VERSION,
    CAPABILITY_FOR_METHOD,
    OPT_IN_CAPABILITIES,
)
from custom_components.hearth_ai.rpc import ALLOWED_METHODS, ENTITY_ID, ENTITY_ID_PARAMS, ERROR_CODES

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


def test_setup_denials_match_shared() -> None:
    """The hub refuses the same setups the box does (#467), so a hub deploy covers every box.

    Compared as sets in both directions: an entry only the box has protects no home that has not
    updated, and an entry only the hub has is one a home talking to an older hub was never told about.
    """
    data = _load()
    assert set(data["denied_setup_domains"]) == DENIED_DOMAINS
    assert len(data["denied_setup_domains"]) == len(DENIED_DOMAINS)


def test_control_is_limited_to_opt_in_methods() -> None:
    """Every method behind an opt-in switch, named here so adding one is a decision.

    All of them operate the home except `states.history`, which only reads, and the `helpers.*`
    and `labels.*`/`categories.*` families, which change the home's *configuration* rather than the
    home (AgDR-0039, AgDR-0040) — it is opt-in because a
    month of readings shows when the house is empty, not because it changes anything (AgDR-0036).
    """
    control = {m for m in ALLOWED_METHODS if CAPABILITY_FOR_METHOD[m] in OPT_IN_CAPABILITIES}
    assert control == {
        "states.history",
        "helpers.types",
        "helpers.describe",
        "helpers.list",
        "helpers.create",
        "helpers.update",
        "helpers.rename",
        "helpers.delete",
        "labels.list",
        "labels.create",
        "labels.update",
        "labels.delete",
        "labels.assign",
        "categories.list",
        "categories.create",
        "categories.update",
        "categories.delete",
        "categories.assign",
        "notify.targets",
        "notify.send",
        "devices.call",
        "scenes.activate",
        "scripts.run",
        "integrations.list",
        "integrations.discovered",
        "integrations.available",
        "integrations.flow_start",
        "integrations.flow_step",
        "integrations.flow_abort",
        "areas.create",
        "areas.set_floor",
        "floors.create",
        "areas.update",
        "areas.delete",
        "floors.update",
        "floors.delete",
        "areas.assign",
        "entities.exposable",
        "entities.expose",
        "access.list",
        "access.operate",
        "vision.list",
        "vision.snapshot",
        "vision.webrtc_config",
        "vision.webrtc_offer",
        "vision.webrtc_candidate",
        "vision.webrtc_close",
    }
    # Methods that deliberately reach a sensitive domain. The list is the point of the check, not a
    # hole in it: the guard catches a method that *accidentally* names one, so a deliberate one has
    # to be written here, which forces the decision rather than letting it slip in. Both have a
    # record (AgDR-0025) and both are app-only on the hub side.
    reviewed = {
        "access.list",
        "access.operate",
        "vision.list",
        "vision.snapshot",
        "vision.webrtc_config",
        "vision.webrtc_offer",
        "vision.webrtc_candidate",
        "vision.webrtc_close",
    }
    for m in ALLOWED_METHODS:
        if m in reviewed:
            continue
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


def test_reported_version_matches_the_manifest() -> None:
    """HACS installs the manifest version; the hub must be told the same one."""
    from custom_components.hearth_ai.const import INTEGRATION_VERSION

    manifest = json.loads((Path(__file__).resolve().parents[1] / "custom_components" / "hearth_ai" / "manifest.json").read_text())
    assert INTEGRATION_VERSION == manifest["version"]
    assert INTEGRATION_VERSION.count(".") == 2


def test_switching_an_automation_is_a_write_and_not_a_new_capability() -> None:
    """
    `automations.set_enabled` (AgDR-0044) sits under the capability that already means "may change
    this home's automations". A new capability would have needed a `CAPABILITIES_VERSION` bump and a
    new switch in the options flow for something that is not a new kind of reach — and it would have
    let a household grant "may disable my rules" without granting "may rewrite them", which is the
    weaker of the two, not the stronger.
    """
    assert "automations.set_enabled" in ALLOWED_METHODS
    assert CAPABILITY_FOR_METHOD["automations.set_enabled"] == "automations.write"
    # Still never a way to run one: that is the line AgDR-0042 drew.
    assert "automations.trigger" not in ALLOWED_METHODS


def _entity_id_paths(schema: dict, pattern: str) -> list[tuple[str, ...]]:
    """Every path in one params schema whose value is the shared `EntityId`, found by its pattern.

    Refuses a keyword it does not walk, so a schema reshaped with `$ref` or `prefixItems` is red here
    rather than walked past.
    """
    walked = {"properties", "items", "anyOf", "oneOf", "allOf", "additionalProperties", "propertyNames"}
    unwalked = {"$ref", "$defs", "definitions", "prefixItems", "patternProperties", "not", "if", "then", "else"}
    out: list[tuple[str, ...]] = []

    def walk(node: object, path: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        assert not (unwalked & node.keys()), (path, unwalked & node.keys())
        if node.get("pattern") == pattern:
            out.append(path)
            return
        for name, child in node.get("properties", {}).items():
            walk(child, (*path, name))
        for key in walked - {"properties"}:
            child = node.get(key)
            for c in child if isinstance(child, list) else [child]:
                walk(c, (*path, "[]") if key == "items" else path)

    walk(schema, ())
    return out


def test_entity_id_params_match_shared() -> None:
    """The dispatcher checks the parameter the schema types as `EntityId`, for every method (#646).

    Derived from the schema, not from a grep over spellings: two handlers take their ids under
    `entity_ids`, and a check keyed on `entity_id` alone passed every row that named only that.
    The dispatcher looks one top-level key up per method, so a deeper or a second path is red here.
    """
    data = _load()
    pattern = data["methods"]["states.get"]["params"]["properties"]["entity_id"]["pattern"]
    assert pattern == f"^{ENTITY_ID.pattern}$"
    derived: dict[str, str] = {}
    for method in data["hub_to_ha_allowlist"]:
        paths = _entity_id_paths(data["methods"][method]["params"], pattern)
        if not paths:
            continue
        assert len(paths) == 1 and paths[0][1:] in ((), ("[]",)), (method, paths)
        derived[method] = paths[0][0]
    # The control: the walk finds a single id and a list, under both spellings.
    assert derived["states.get"] == "entity_id" and derived["entities.expose"] == "entity_ids"
    assert derived == ENTITY_ID_PARAMS
