"""Driving Home Assistant config flows, and refusing to relay credentials."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockModule, mock_config_flow, mock_integration, mock_platform

from custom_components.hearth_ai.handlers.integrations import DENIED_DOMAINS, _is_secret, _serialize_schema
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

CAPS = frozenset({"integrations.manage"})


async def test_capability_is_required(core: HomeAssistant) -> None:
    d = build_dispatcher(core, frozenset({"entities.read"}))
    for method, params in [
        ("integrations.list", {}),
        ("integrations.available", {"query": "lg"}),
        ("integrations.flow_start", {"domain": "lg_soundbar"}),
    ]:
        with pytest.raises(RpcError) as ei:
            await d.dispatch(method, params)
        assert ei.value.code == "method_not_allowed"
        assert "disabled" in ei.value.message


async def test_lists_what_is_configured_and_searchable(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    assert isinstance(await d.dispatch("integrations.list", {}), list)
    assert await d.dispatch("integrations.discovered", {}) == []
    found = await d.dispatch("integrations.available", {"query": "soundbar"})
    assert any(i["domain"] == "lg_soundbar" for i in found), found
    assert all("name" in i and "already_configured" in i for i in found)


async def test_refuses_hearth_itself_and_host_level_integrations(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    assert "hearth_ai" in DENIED_DOMAINS
    for domain in ("hearth_ai", "hassio", "shell_command"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_start", {"domain": domain})
        assert ei.value.code == "method_not_allowed"
    # and they never appear in search results
    for i in await d.dispatch("integrations.available", {"query": "hearth"}):
        assert i["domain"] != "hearth_ai"


async def test_unknown_domain(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_start", {"domain": "not_a_real_integration"})
    assert ei.value.code == "not_found"


async def test_drives_a_form_flow_and_creates_the_entry(core: HomeAssistant) -> None:
    """A no-secret flow (like the LG soundbar's host field) can be completed end to end."""
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d)
    assert start["type"] == "form"
    assert start["step_id"] == "user"
    assert [f["name"] for f in start["fields"]] == ["host"]
    assert start["fields"][0]["required"] is True
    assert start["secret_fields"] == []

    done = await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"host": "192.168.1.50"}})
    assert done["type"] == "create_entry"
    assert done["title"] == "Fake device at 192.168.1.50"


async def test_never_relays_a_password(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d, secret=True)
    assert start["secret_fields"] == ["password"]
    assert [f["secret"] for f in start["fields"]] == [False, True]

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"username": "me", "password": "hunter2"}})
    assert ei.value.code == "method_not_allowed"
    assert "credentials" in ei.value.message
    assert "Devices & services" in ei.value.message
    # the flow is still open, waiting for the person to finish it in HA
    assert any(f["flow_id"] == start["flow_id"] for f in await d.dispatch("integrations.discovered", {}))
    await d.dispatch("integrations.flow_abort", {"flow_id": start["flow_id"]})
    assert await d.dispatch("integrations.discovered", {}) == []


def test_secret_detection_covers_selectors_and_names() -> None:
    assert _is_secret({"name": "host", "type": "string"}) is False
    assert _is_secret({"name": "password", "type": "string"}) is True          # bare string, older flows
    assert _is_secret({"name": "api_key", "type": "string"}) is True
    assert _is_secret({"name": "access_token", "type": "string"}) is True
    assert _is_secret({"name": "anything", "selector": {"text": {"type": "password"}}}) is True
    assert _is_secret({"name": "anything", "selector": {"text": {"type": "url"}}}) is False


def test_serialiser_is_available() -> None:
    """Home Assistant's own form serialiser, however it is packaged in this version."""
    import probatio as vol
    from homeassistant.helpers import config_validation as cv

    fields = _serialize_schema(vol.Schema({vol.Required("host"): str}), custom_serializer=cv.custom_serializer)
    assert fields[0]["name"] == "host"
    assert fields[0]["required"] is True


# --------------------------------------------------------------------------- helpers
async def _start_fake_flow(core: HomeAssistant, dispatcher: Any, *, secret: bool = False) -> dict[str, Any]:
    """Register a throwaway config flow so the test does not depend on a real integration."""
    import probatio as vol
    from homeassistant import config_entries
    from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

    domain = "demo_secret" if secret else "demo_device"
    schema = (
        vol.Schema(
            {
                vol.Required("username"): str,
                vol.Required("password"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        if secret
        else vol.Schema({vol.Required("host"): str})
    )

    class FakeFlow(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_user(self, user_input=None):
            if user_input is None:
                return self.async_show_form(step_id="user", data_schema=schema)
            return self.async_create_entry(title=f"Fake device at {user_input.get('host')}", data=user_input)

    mock_integration(core, MockModule(domain), built_in=False)
    mock_platform(core, f"{domain}.config_flow", None)
    with (
        mock_config_flow(domain, FakeFlow),
        patch("custom_components.hearth_ai.handlers.integrations.async_get_config_flows", return_value={domain}),
    ):
        result = await dispatcher.dispatch("integrations.flow_start", {"domain": domain})
    assert result["type"] in (FlowResultType.FORM.value, "form")
    return result
