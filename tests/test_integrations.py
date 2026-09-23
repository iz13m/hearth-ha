"""Driving Home Assistant config flows, and refusing to relay credentials."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockModule, mock_config_flow, mock_integration, mock_platform

from custom_components.hearth_ai.handlers.flows import DENIED_DOMAINS, is_secret, serialize_schema
from custom_components.hearth_ai.handlers import flows
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


async def test_refuses_the_path_disclosure_integrations_end_to_end(core: HomeAssistant) -> None:
    """#227's and #256's setup axis, exercised through the dispatcher rather than asserted.

    Every other check on these three is **membership**: the anti-revert teeth in `test_policy.py`
    assert `"downloader" in DENIED_DOMAINS`, and `integrations.available` filters on the same set.
    So a refactor of `check_domain` that stopped consulting `DENIED_DOMAINS` would leave all of
    them green while all three denials went inert — the shape that let `find_service_call_violations`
    regress with two walker corpora passing beside it. This is the only test that makes the refusal
    happen, and the test above covers `hearth_ai`/`hassio`/`shell_command` but never these.

    **Three enforcers, not one** — the enumeration was wrong twice, each time because the query
    matched the data rather than the behaviour:

        integrations.py:102  integrations_flow_start  check_domain(params["domain"])
        integrations.py:120  integrations_flow_step   check_domain(flow["handler"])   re-derived
        helpers.py:147       _flow_domains            `d not in DENIED_DOMAINS`, 6 call sites

    `_flow_domains` enforces the setup denial **without going through `check_domain`** and refuses
    with `not_found` rather than `method_not_allowed`. No behaviour change for these three (none
    is a helper flow — checked: `async_get_config_flows(hass, "helper")` excludes all of them),
    but a fourth enforcer would not be surprising: grepping readers of `DENIED_DOMAINS` misses
    `integrations_flow_step`, which never mentions the set, and grepping callers of `check_domain`
    misses `_flow_domains`, which never calls it.
    """
    d = build_dispatcher(core, CAPS)
    for domain in ("downloader", "local_file", "upb"):
        assert domain in DENIED_DOMAINS, domain
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_start", {"domain": domain})
        # Refused for *being denied*, not for being unknown or malformed — `not_found` here would
        # mean the check never ran and HA simply had no such integration.
        assert ei.value.code == "method_not_allowed", (domain, ei.value.code)


async def test_the_setup_denylist_does_not_swallow_what_it_spared(core: HomeAssistant) -> None:
    """The setup axis's over-refusal control — the mirror of teeth item 5 on the call axis (#257).

    #227's load-bearing sentence is a **refusal to deny**: *do not deny the thirteen ungated config
    flows; they are not one class, and denying them would spend `zwave_js`, `mqtt` and `zha` reach
    on non-holes.* That ruling was enforced by nobody — adding `zha`, `velbus`, `zwave_js` and
    `mqtt` to `DENIED_DOMAINS` left the whole suite green, so the product could refuse every serial
    integration it has and nothing would go red.

    **The assertion is "the refusal is not ours", not "the flow starts."** Three of the four cannot
    start in this environment at all — `zha`, `velbus` and `zwave_js` depend on `usb`, which needs
    `aiousbwatcher` — so asserting `type == "form"` would pin the presence of an optional dependency
    rather than the denylist's width. `check_domain` runs first in `integrations_flow_start`, so any
    error that is *not* `method_not_allowed` proves the domain got past our denial and failed on its
    own terms. That is exactly the property at risk, and it holds whether or not the dep is there.
    """
    d = build_dispatcher(core, CAPS)

    # Denied, by name — the other direction, kept in the same test so a reader sees the trade.
    for domain in ("downloader", "local_file", "upb"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_start", {"domain": domain})
        assert ei.value.code == "method_not_allowed", (domain, ei.value.code)

    # Spared — deliberately not denied, and the denylist must not grow over them.
    started: list[str] = []
    for domain in ("zha", "velbus", "zwave_js", "mqtt"):
        try:
            result = await d.dispatch("integrations.flow_start", {"domain": domain})
        except RpcError as err:
            assert err.code != "method_not_allowed", (
                f"{domain} is now refused at setup. #227 declined to deny the ungated serial and "
                "broker flows on purpose — they are not one class with the path-writers. If this "
                "is intentional, the decision belongs in a ruling, not in a widened denylist."
            )
        else:
            started.append(domain)
            await d.dispatch("integrations.flow_abort", {"flow_id": result["flow_id"]})

    # Without this the test degenerates: if every spared flow failed for its own reasons it would
    # still pass, and could no longer tell a spared domain from a broken harness.
    assert started, "no spared flow started at all; this test can no longer distinguish the two"


async def test_the_setup_denial_does_not_reach_an_existing_installation(core: HomeAssistant) -> None:
    """Over-refusal in the other direction: a correct denial leaking into paths it never meant to
    touch (#257).

    The denial is meant to stop *setup*, not to hide a `local_file` camera a household already has.
    `DENIED_DOMAINS` is read only on "what can be added" paths — `check_domain`,
    `integrations_available`, `helpers._flow_domains` — and `integrations.list` filters on nothing.
    That is a structural claim about three enforcers, and the enumeration behind it was wrong twice,
    so it is asserted here as behaviour instead.
    """
    reg = er.async_get(core)
    existing = {}
    for platform, domain, uid in (("upb", "light", "u1"), ("downloader", "sensor", "d1"), ("local_file", "sensor", "l1")):
        entry = reg.async_get_or_create(domain, platform, uid)
        core.states.async_set(entry.entity_id, "on")
        async_expose_entity(core, "conversation", entry.entity_id, True)
        existing[platform] = entry.entity_id
    await core.async_block_till_done()

    d = build_dispatcher(core, frozenset({"entities.read", "integrations.manage"}))
    listed = {e["entity_id"] for e in await d.dispatch("entities.list", {})}
    for platform, entity_id in existing.items():
        assert entity_id in listed, f"denying {platform} at setup hid an entity the household already has"
        assert (await d.dispatch("states.get", {"entity_id": entity_id}))["state"] == "on", platform


async def test_a_flow_opened_before_the_denial_landed_cannot_be_completed(core: HomeAssistant) -> None:
    """`integrations_flow_step` re-derives the domain from `flow["handler"]` and re-checks it, so
    the denial is "cannot start **and** cannot continue" (#257).

    The window is real: a household part-way through a flow when the integration updates. It is
    also the window that matters most for `upb`, whose disclosure happens at the final step — a
    start-only denial would refuse the first step of a flow that was already past it.
    """
    d = build_dispatcher(core, CAPS)
    start = await _start_fake_flow(core, d)          # allowed when it started
    domain = start["domain"]
    assert domain not in flows.DENIED_DOMAINS

    original = flows.DENIED_DOMAINS
    flows.DENIED_DOMAINS = original | {domain}       # ...and denied while it is open
    try:
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"host": "192.168.1.50"}})
        assert ei.value.code == "method_not_allowed", ei.value.code
    finally:
        flows.DENIED_DOMAINS = original


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
