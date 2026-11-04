"""Checks hassfest runs on the hearth-ha mirror, pinned here so they fail in Hearth's CI first."""

import pathlib
import re

INTEGRATION = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "hearth_ai"

# Copied from Home Assistant's `script/hassfest/services.py` (`validate_services`). With no
# `services.yaml`, hassfest greps every `.py` file for this and fails the integration on any match,
# comments included. v0.31.0 shipped red that way: a comment in `policy.py` named the registration
# helper. The integration registers no services, so the pattern must not appear anywhere.
HASSFEST_SERVICES_PATTERN = re.compile(
    r"(hass\.services\.(register|async_register))|async_register_entity_service|async_register_admin_service"
)


def test_nothing_reads_to_hassfest_as_registering_a_service() -> None:
    assert not (INTEGRATION / "services.yaml").exists(), "a services.yaml exists: delete this test"
    hits = [
        f"{path.relative_to(INTEGRATION)}:{n}"
        for path in sorted(INTEGRATION.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if HASSFEST_SERVICES_PATTERN.search(line)
    ]
    assert hits == []


def test_the_pattern_still_fires() -> None:
    assert HASSFEST_SERVICES_PATTERN.search("    # async_register_entity_service in lock.py")
