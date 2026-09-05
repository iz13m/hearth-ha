"""Fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: PT004
    """Enable loading custom_components in tests."""
    yield


@pytest.fixture
async def core(hass: HomeAssistant, tmp_path: Path) -> HomeAssistant:
    """HA core with automation/scene/script set up against a default-shaped config dir."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\nscene: !include scenes.yaml\nscript: !include scripts.yaml\n"
        "input_boolean:\n  test:\n    initial: false\n"
    )
    for name in ("automations.yaml", "scenes.yaml"):
        (tmp_path / name).write_text("[]\n")
    (tmp_path / "scripts.yaml").write_text("{}\n")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"test": {"initial": False}}})
    # input_boolean is not exposed to Assist by default; the AI only sees exposed entities.
    async_expose_entity(hass, "conversation", "input_boolean.test", True)
    assert await async_setup_component(hass, "automation", {"automation": []})
    assert await async_setup_component(hass, "scene", {"scene": []})
    assert await async_setup_component(hass, "script", {"script": {}})
    await hass.async_block_till_done()
    return hass
