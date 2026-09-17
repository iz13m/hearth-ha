"""Driving Home Assistant config flows, and refusing to relay credentials."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockModule, mock_config_flow, mock_integration, mock_platform

from custom_components.hearth_ai.handlers.flows import DENIED_DOMAINS, is_secret, serialize_schema
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
    assert is_secret({"name": "host", "type": "string"}) is False
    assert is_secret({"name": "password", "type": "string"}) is True          # bare string, older flows
    assert is_secret({"name": "api_key", "type": "string"}) is True
    assert is_secret({"name": "access_token", "type": "string"}) is True
    assert is_secret({"name": "anything", "selector": {"text": {"type": "password"}}}) is True
    assert is_secret({"name": "anything", "selector": {"text": {"type": "url"}}}) is False


def test_serialiser_is_available() -> None:
    """Home Assistant's own form serialiser, however it is packaged in this version."""
    import probatio as vol
    from homeassistant.helpers import config_validation as cv

    fields = serialize_schema(vol.Schema({vol.Required("host"): str}), custom_serializer=cv.custom_serializer)
    assert fields[0]["name"] == "host"
    assert fields[0]["required"] is True


async def test_a_form_field_is_policed_like_the_automation_it_can_be(core: HomeAssistant) -> None:
    """The bypass this whole change exists for (AgDR-0038).

    Home Assistant's `template` helper takes a whole action sequence in a form field and really runs
    it when the entity is operated. Submitting one was a way to author an action the automation path
    would have refused — `lock.unlock` on every lock in the house, with no lock id needed — and then
    fire it with `devices.call` on the resulting `switch.*`.
    """
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d, kind="action")
    unlock = {"name": "Evil", "turn_on": [{"action": "lock.unlock", "target": {"entity_id": "all"}}]}

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": unlock})
    assert ei.value.code == "validation_failed"
    assert "lock.unlock" in ei.value.message

    # ...while the same field carrying an allowed action still works, because refusing every action
    # would cost "a switch that turns on both lamps" and buy nothing a script does not already allow.
    done = await d.dispatch(
        "integrations.flow_step",
        {"flow_id": start["flow_id"], "input": {"name": "Both lamps", "turn_on": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}]}},
    )
    assert done["type"] == "create_entry"


async def test_a_form_field_may_not_name_an_off_limits_entity(core: HomeAssistant) -> None:
    """A trend helper's whole configuration is an entity_id, and no action is involved at all."""
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d, kind="action")

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"name": "Front door trend", "entity_id": "lock.front_door"}})
    assert ei.value.code == "validation_failed"
    assert "lock.front_door" in ei.value.message

    # Nor inside a template, which is how a template sensor would read one.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"name": "Sneaky", "state": "{{ states('camera.porch') }}"}})
    assert ei.value.code == "validation_failed"


async def test_a_password_typed_field_with_an_innocent_name_is_refused(core: HomeAssistant) -> None:
    """The half of the credential check that was dead code.

    `flow_step` consulted `flow.get("data_schema")`, but an `async_progress()` dict is built from
    flow_id/handler/context/step_id only — so it was always None, the selector arm never ran, and a
    password-typed field called `pw` was relayed. The fields we served are remembered instead.
    """
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d, kind="innocent")
    assert start["secret_fields"] == ["pw"]

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"pw": "hunter2"}})
    assert ei.value.code == "method_not_allowed"
    assert "pw" in ei.value.message


async def test_the_remembered_form_is_forgotten_when_the_flow_ends(core: HomeAssistant) -> None:
    from custom_components.hearth_ai.handlers.flows import DATA_FLOW_FIELDS

    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d)
    assert core.data[DATA_FLOW_FIELDS][start["flow_id"]]
    await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"host": "10.0.0.2"}})
    assert start["flow_id"] not in core.data[DATA_FLOW_FIELDS]

    aborted = await _start_fake_flow(core, d)
    assert core.data[DATA_FLOW_FIELDS][aborted["flow_id"]]
    await d.dispatch("integrations.flow_abort", {"flow_id": aborted["flow_id"]})
    assert aborted["flow_id"] not in core.data[DATA_FLOW_FIELDS]


# --------------------------------------------------------------------------- helpers
async def _start_fake_flow(core: HomeAssistant, dispatcher: Any, *, secret: bool = False, kind: str = "host") -> dict[str, Any]:
    """Register a throwaway config flow so the test does not depend on a real integration.

    `kind` picks the shape under test: `host` is an ordinary field, `action` stands in for Home
    Assistant's `template` helper (a form field that takes a whole action sequence), and `innocent`
    is a password-*typed* field whose name says nothing — the case the dead `data_schema` check was
    supposed to catch.
    """
    import probatio as vol
    from homeassistant import config_entries
    from homeassistant.helpers.selector import ActionSelector, TextSelector, TextSelectorConfig, TextSelectorType

    password = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    if secret:
        domain, schema = "demo_secret", vol.Schema({vol.Required("username"): str, vol.Required("password"): password})
    elif kind == "action":
        domain, schema = "demo_actions", vol.Schema({vol.Required("name"): str, vol.Optional("turn_on"): ActionSelector()})
    elif kind == "innocent":
        domain, schema = "demo_innocent", vol.Schema({vol.Required("pw"): password})
    else:
        domain, schema = "demo_device", vol.Schema({vol.Required("host"): str})

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
