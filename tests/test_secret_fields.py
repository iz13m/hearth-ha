"""Which form fields are credentials, and that Hearth never relays one (#668, invariant 1).

The census at the bottom reads every `config_flow.py` in the Home Assistant this suite pins, so a
release that adds a credential field under a name the matcher does not know turns this file red
instead of passing silently, the way `test_every_path_writing_service_is_classified` does for paths.
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockModule, mock_config_flow, mock_integration, mock_platform

from custom_components.hearth_ai.handlers import flows
from custom_components.hearth_ai.handlers.flows import find_secret_paths, flatten, is_secret, is_secret_name, serialize_fields
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"
CAPS = frozenset({"integrations.manage"})
BINDKEY = "0123456789abcdef01234567"


def _shared() -> dict[str, Any]:
    assert SCHEMA.is_file(), "run `pnpm --filter @hearth/shared export:jsonschema` first"
    return json.loads(SCHEMA.read_text())["secret_fields"]


# --------------------------------------------------------------------------- the hub agrees


def test_the_lists_equal_the_hubs() -> None:
    shared = _shared()
    assert list(flows.SECRET_NAME_SUBSTRINGS) == shared["substrings"]
    assert flows.SECRET_NAME_TOKENS == set(shared["tokens"])
    assert flows.SECRET_NAME_FINAL_TOKENS == set(shared["final_tokens"])
    assert flows.SECRET_NAMES == set(shared["names"])
    assert flows.NOT_SECRET_NAMES == set(shared["not_secret_names"])
    assert flows.SECRET_WALK_MAX_DEPTH == shared["walk_max_depth"]


def test_every_name_gets_the_hubs_verdict() -> None:
    cases = _shared()["name_cases"]
    assert len(cases) > 50
    assert {c["secret"] for c in cases} == {True, False}, "the corpus needs controls both ways"
    for case in cases:
        assert is_secret_name(case["name"]) is case["secret"], case["name"]


def test_every_input_gets_the_hubs_paths() -> None:
    cases = _shared()["input_cases"]
    assert any(c["paths"] for c in cases) and any(not c["paths"] for c in cases)
    for case in cases:
        assert find_secret_paths(case["input"]) == case["paths"], case["name"]


# --------------------------------------------------------------------------- the classes, with controls

KEYS = ["bindkey", "key", "encryption_key", "client_key", "noise_psk", "backbone_key", "device_authentication", "user_key", "key_pem", "passkey"]
CODES = ["code", "installer_code", "user_code", "usercodes", "install_code", "activation_code", "pairing_code"]
ONE_TIME = ["otp", "mfa_code", "two_factor_code", "sms_code", "verification_code", "captcha_code"]
CONTROLS = ["host", "country_code", "zip_code", "post_code", "language_code", "audio_codec", "code_format", "key_id", "qr_code", "station_code"]


@pytest.mark.parametrize("name", KEYS + CODES + ONE_TIME)
def test_a_credential_name_is_secret(name: str) -> None:
    assert is_secret_name(name)
    assert is_secret({"name": name, "type": "string"})


@pytest.mark.parametrize("name", CONTROLS)
def test_an_ordinary_name_is_not(name: str) -> None:
    assert not is_secret_name(name)


# --------------------------------------------------------------------------- end to end, through the dispatcher


async def _start(core: HomeAssistant, d: Any, domain: str, schema: Any, *, reauth_default: str | None = None) -> dict[str, Any]:
    """A throwaway flow whose form is `schema`; with `reauth_default`, a failed login re-shows it."""
    from homeassistant import config_entries

    class FakeFlow(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_user(self, user_input=None):
            if user_input is None or reauth_default is not None:
                # As blink's `reauth_confirm` does: the stored password as the field's default.
                return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "invalid_auth"} if user_input else None)
            return self.async_create_entry(title="Fake", data=user_input)

    mock_integration(core, MockModule(domain), built_in=False)
    mock_platform(core, f"{domain}.config_flow", None)
    with (
        mock_config_flow(domain, FakeFlow),
        patch("custom_components.hearth_ai.handlers.integrations.async_get_config_flows", return_value={domain}),
    ):
        result = await d.dispatch("integrations.flow_start", {"domain": domain})
    assert result["type"] == FlowResultType.FORM.value
    return result


@pytest.mark.parametrize(
    ("domain", "field", "value"),
    [
        ("xiaomi_ble_like", "bindkey", BINDKEY),  # xiaomi_ble, bthome
        ("yalexs_ble_like", "key", "00112233445566778899aabbccddeeff"),  # a lock's offline key
        ("switchbot_like", "encryption_key", "00112233445566778899aabbccddeeff"),
        ("esphome_like", "noise_psk", "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0MTI="),
        ("bosch_alarm_like", "installer_code", "1234"),
        ("abode_like", "mfa_code", "654321"),
    ],
)
async def test_a_bare_string_credential_is_refused_and_handed_back(core: HomeAssistant, domain: str, field: str, value: str) -> None:
    import probatio as vol

    d = build_dispatcher(core, CAPS)
    start = await _start(core, d, domain, vol.Schema({vol.Required("address"): str, vol.Required(field): vol.All(str, vol.Strip)}))
    assert start["secret_fields"] == [field]
    assert [f["secret"] for f in start["fields"]] == [False, True]

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"address": "A4:C1:38:00:00:00", field: value}})
    assert ei.value.code == "method_not_allowed"
    assert field in ei.value.message and "Devices & services" in ei.value.message
    # The message reaches `audit_log.error`: it names the field, never the value.
    assert value not in ei.value.message
    # Handed back: the flow is still there for the person.
    assert any(f["flow_id"] == start["flow_id"] for f in await d.dispatch("integrations.discovered", {}))


async def test_an_ordinary_form_still_goes_through(core: HomeAssistant) -> None:
    import probatio as vol

    d = build_dispatcher(core, CAPS)
    start = await _start(core, d, "plain_like", vol.Schema({vol.Required("host"): str, vol.Optional("country_code"): str, vol.Optional("key_id"): str}))
    assert start["secret_fields"] == []
    done = await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"host": "10.0.0.2", "country_code": "SA", "key_id": "k1"}})
    assert done["type"] == "create_entry"


def _ntfy_like(innocent: bool = False) -> Any:
    import probatio as vol
    from homeassistant.data_entry_flow import section
    from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

    password = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    inner = {vol.Optional("username"): str, vol.Optional("pw" if innocent else "password", description={"suggested_value": "stored-pw"}): password}
    return vol.Schema({vol.Required("url"): str, vol.Required("auth"): section(vol.Schema(inner), {"collapsed": True})})


@pytest.mark.parametrize("innocent", [False, True])
async def test_a_password_inside_a_section_is_refused(core: HomeAssistant, innocent: bool) -> None:
    """Security reproduced this on `main`: an ntfy-shaped flow reached `create_entry` with the password."""
    d = build_dispatcher(core, CAPS)
    start = await _start(core, d, f"ntfy_like_{str(innocent).lower()}", _ntfy_like(innocent))
    pw = "pw" if innocent else "password"
    assert start["secret_fields"] == [f"auth.{pw}"]
    [url, auth] = start["fields"]
    assert url["secret"] is False and auth["secret"] is True
    assert "stored-pw" not in json.dumps(start)

    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"url": "https://ntfy.sh", "auth": {"username": "me", pw: "hunter2"}}})
    assert ei.value.code == "method_not_allowed" and f"auth.{pw}" in ei.value.message
    assert "hunter2" not in ei.value.message

    # The section without its secret is a form the model may fill.
    done = await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"url": "https://ntfy.sh", "auth": {"username": "me"}}})
    assert done["type"] == "create_entry"


async def test_a_stored_secret_is_not_sent_back_on_a_reauth_form(core: HomeAssistant) -> None:
    """blink's `reauth_confirm` re-shows the form with `default=entry.data[CONF_PASSWORD]` (#668, item 3)."""
    import probatio as vol

    stored = "the-stored-password"
    schema = vol.Schema(
        {
            vol.Required("username", default="me@example.com"): str,
            vol.Required("password", default=stored): str,
            vol.Optional("bindkey", description={"suggested_value": BINDKEY}): str,
        }
    )
    d = build_dispatcher(core, CAPS)
    start = await _start(core, d, "blink_like", schema, reauth_default=stored)
    again = await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {}})
    for shown in (start, again):
        text = json.dumps(shown)
        assert stored not in text and BINDKEY not in text, shown
        username, password, bindkey = shown["fields"]
        assert username["default"] == "me@example.com"  # the control: an ordinary default still goes out
        assert password["secret"] and "default" not in password and "description" not in password
        assert bindkey["secret"] and "description" not in bindkey


# --------------------------------------------------------------------------- a form Hearth did not show (Security, #668)


def _password_typed(name: str) -> Any:
    """adax's shape: a password-typed field whose name says nothing, beside an ordinary one."""
    import probatio as vol
    from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

    return vol.Schema({vol.Required("ssid"): str, vol.Required(name): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))})


async def _init_outside_hearth(core: HomeAssistant, domain: str, schema: Any) -> str:
    """A flow Home Assistant started itself (discovery, reauth): Hearth never served its form."""
    from homeassistant import config_entries

    class FakeFlow(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_user(self, user_input=None):
            if user_input is None:
                return self.async_show_form(step_id="user", data_schema=schema)
            return self.async_create_entry(title="Fake", data=user_input)

    mock_integration(core, MockModule(domain), built_in=False)
    mock_platform(core, f"{domain}.config_flow", None)
    with mock_config_flow(domain, FakeFlow):
        result = await core.config_entries.flow.async_init(domain, context={"source": "user"})
    return str(result["flow_id"])


async def test_a_password_typed_field_on_a_flow_hearth_did_not_start_is_refused(core: HomeAssistant) -> None:
    d = build_dispatcher(core, CAPS)
    flow_id = await _init_outside_hearth(core, "adax_like", _password_typed("wlan_word"))
    assert flow_id not in (core.data.get(flows.DATA_FLOW_FIELDS) or {})
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": {"ssid": "Home", "wlan_word": "hunter2"}})
    assert ei.value.code == "method_not_allowed" and "wlan_word" in ei.value.message and "hunter2" not in ei.value.message
    # The control: the ordinary field alone goes through to Home Assistant, whose own schema then asks again.
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": {"ssid": "Home"}})
    assert ei.value.code == "ha_error"


async def test_adaxs_wifi_pswd_is_refused_by_its_name(core: HomeAssistant) -> None:
    assert is_secret_name("wifi_pswd") and is_secret_name("admin_pwd")
    d = build_dispatcher(core, CAPS)
    flow_id = await _init_outside_hearth(core, "adax_named", _password_typed("wifi_pswd"))
    with (
        patch.object(flows, "_live_form", return_value=[]),  # even with the form read as harmless
        pytest.raises(RpcError) as ei,
    ):
        await d.dispatch("integrations.flow_step", {"flow_id": flow_id, "input": {"ssid": "Home", "wifi_pswd": "hunter2"}})
    assert ei.value.code == "method_not_allowed" and "wifi_pswd" in ei.value.message


async def test_opening_many_flows_does_not_make_hearth_forget_a_form(core: HomeAssistant) -> None:
    """Remembering a 33rd form used to clear every remembered form; now the oldest goes, and the
    forgotten one is read again from the flow manager."""
    d = build_dispatcher(core, CAPS)
    first = await _start(core, d, "innocent_first", _password_typed("wlan_word"))
    second = await _start(core, d, "innocent_second", _password_typed("other"))
    for i in range(flows.MAX_TRACKED_FLOWS - 1):
        await _start(core, d, f"filler_{i}", _password_typed("x"))
    store = core.data[flows.DATA_FLOW_FIELDS]
    assert len(store) == flows.MAX_TRACKED_FLOWS
    assert first["flow_id"] not in store and second["flow_id"] in store
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": first["flow_id"], "input": {"ssid": "Home", "wlan_word": "hunter2"}})
    assert ei.value.code == "method_not_allowed" and "wlan_word" in ei.value.message


async def test_a_form_that_cannot_be_read_is_not_filled(core: HomeAssistant) -> None:
    """The private flow-manager read failing is the fallback's case: what Hearth showed, else refuse."""
    d = build_dispatcher(core, CAPS)
    unseen = await _init_outside_hearth(core, "unreadable", _password_typed("wlan_word"))
    with patch.object(flows, "_live_form", return_value=None):
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_step", {"flow_id": unseen, "input": {"ssid": "Home"}})
        assert ei.value.code == "method_not_allowed" and "cannot read the form" in ei.value.message
        # An empty step sends nothing, so it is not refused (Home Assistant asks again).
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_step", {"flow_id": unseen, "input": {}})
        assert ei.value.code == "ha_error"

        # A form Hearth did show is still the fallback.
        shown = await _start(core, d, "shown", _password_typed("wlan_word"))
        with pytest.raises(RpcError) as ei:
            await d.dispatch("integrations.flow_step", {"flow_id": shown["flow_id"], "input": {"ssid": "Home", "wlan_word": "x"}})
        assert ei.value.code == "method_not_allowed" and "wlan_word" in ei.value.message


async def test_the_live_form_is_read_when_the_private_api_is_there(core: HomeAssistant) -> None:
    """Pins the private read, so a Home Assistant that moves it turns this red rather than every
    discovered flow quietly becoming 'cannot read the form'."""
    flow_id = await _init_outside_hearth(core, "live_read", _password_typed("wlan_word"))
    fields = flows._live_form(core, flow_id)
    assert fields is not None and flows.secret_paths(fields) == ["wlan_word"]
    assert flows._live_form(core, "no-such-flow") is None


async def test_otps_new_token_stays_refused_and_its_seed_placeholder_is_withheld(core: HomeAssistant) -> None:
    """otp's confirm step shows the new TOTP seed as the `code` placeholder. Hearth cannot reach it,
    because asking for a new token is refused; and were a confirm form shown, the seed is left out."""
    import probatio as vol
    from homeassistant.helpers.selector import BooleanSelector

    assert is_secret_name("new_token") and is_secret_name("token")
    d = build_dispatcher(core, CAPS)
    otp_user = vol.Schema({vol.Optional("token"): str, vol.Optional("new_token"): BooleanSelector(), vol.Required("name", default="OTP Sensor"): str})
    start = await _start(core, d, "otp_like", otp_user)
    assert set(start["secret_fields"]) == {"token", "new_token"}
    with pytest.raises(RpcError) as ei:
        await d.dispatch("integrations.flow_step", {"flow_id": start["flow_id"], "input": {"name": "Phone", "new_token": True}})
    assert ei.value.code == "method_not_allowed" and "new_token" in ei.value.message

    confirm = {
        "type": FlowResultType.FORM,
        "flow_id": "f-confirm",
        "step_id": "confirm",
        "data_schema": vol.Schema({vol.Required("code"): str}),
        "description_placeholders": {"code": "JBSWY3DPEHPK3PXP", "issuer": "Home"},
    }
    shown = flows.shape(core, "otp", confirm)
    assert shown["description_placeholders"] == {"issuer": "Home"}
    assert "JBSWY3DPEHPK3PXP" not in json.dumps(shown)


def test_flatten_keeps_an_ordinary_fields_suggested_value() -> None:
    out = flatten({"name": "host", "type": "string", "description": {"suggested_value": "10.0.0.2"}, "default": "x"})
    assert out["default"] == "x" and "10.0.0.2" in out["description"]


def test_a_section_is_serialised_with_its_fields() -> None:
    fields = serialize_fields(_ntfy_like(), "test")
    assert fields[1]["name"] == "auth" and [f["name"] for f in fields[1]["fields"]] == ["username", "password"]
    assert flows.secret_paths(fields) == ["auth.password"]


async def test_a_helper_config_carrying_a_credential_is_refused(core: HomeAssistant) -> None:
    d = build_dispatcher(core, frozenset({"helpers.manage"}))
    with pytest.raises(RpcError) as ei:
        await d.dispatch("helpers.create", {"domain": "input_text", "config": {"name": "Door", "code": "1234"}})
    assert ei.value.code == "method_not_allowed" and "code" in ei.value.message and "1234" not in ei.value.message
    assert "Helpers" in ei.value.message


async def test_a_template_helper_whose_action_data_has_ordinary_keys_is_made(core: HomeAssistant) -> None:
    """The control for walking into a helper's action sequence: ordinary service data is not a credential.

    The walk does refuse a key holding an old substring anywhere in `config`, `pin` included, so a
    key such as `mapping` or `ping_count` is refused too. That was already the rule for an integration's
    form on the box, and it is now the rule for helpers and on the hub. It fails closed: the cost is a
    helper the person makes in Home Assistant, and no core helper form has such a field.
    """
    d = build_dispatcher(core, frozenset({"helpers.manage"}))
    data = {"brightness_pct": 40, "transition": 2, "color_name": "orange", "kelvin": 2700}
    config = {"name": "Warm lamp", "turn_on": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}, "data": data}]}
    made = await d.dispatch("helpers.create", {"domain": "template", "variant": "switch", "config": config})
    assert made["entities"], made

    with pytest.raises(RpcError) as ei:
        bad = {"name": "Pad", "turn_on": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}, "data": {"mapping": 1}}]}
        await d.dispatch("helpers.create", {"domain": "template", "variant": "switch", "config": bad})
    assert ei.value.code == "method_not_allowed" and "turn_on.0.data.mapping" in ei.value.message
    assert core.config_entries.flow.async_progress() == []


# --------------------------------------------------------------------------- the census

# Any config-flow field name that looks like it could be a credential. Deliberately wider than the
# matcher: what it catches and the matcher allows must be in `REVIEWED_NOT_SECRET`, each one read.
CANDIDATE = re.compile(r"key|psk|passphrase|cookie|auth|bind|hash|otp|code|cert|private|secret|pass|pin|token|pswd|pw", re.I)

# Each read in Home Assistant 2026.9.3; the component is where it was read.
REVIEWED_NOT_SECRET: dict[str, str] = {
    "allow_insecure_setup_codes": "homekit_controller: a boolean",
    "api_auth": "solaredge: a section; its fields are checked one by one",
    "audio_codec": "jellyfin: a choice of codec",
    "auth": "ntfy, scrape: a section; its fields are checked one by one",
    "auth_method": "proxmoxve: a choice",
    "authentication": "generic, scrape, vivotek: basic or digest",
    "authority_id": "azure_data_explorer: a tenant id",
    "auto_bypass": "alarmdecoder: a boolean",
    "auto_bypass_low_battery": "totalconnect: a boolean",
    "cert_pem": "harbor: a certificate, which is public",
    "certificate": "mqtt: an uploaded certificate, which is public",
    "client_cert": "mqtt: the client's certificate, which is public (client_key is refused)",
    "code_arm_required": "alarmdecoder, risco: a boolean",
    "code_disarm_required": "risco: a boolean",
    "code_execution": "anthropic: a boolean",
    "code_interpreter": "openai_conversation: a boolean",
    "code_required": "totalconnect: a boolean",
    "country_code": "a country",
    "id_or_passport": "ituran: who is signing in, not a secret; the OTP that follows is refused",
    "event_code": "rfxtrx: an RF device's identifying code",
    "key_id": "backblaze_b2, switchbot, weatherkit: which key, not the key",
    "knxkeys_file": "knx: the keyring file, encrypted under knxkeys_password (which is refused)",
    "language_code": "a language",
    "lock_code_digits": "verisure, yale_smart_alarm: how many digits",
    "mac_code": "motionblinds_ble: the last four hex of the MAC",
    "place_code": "meteo_lt: a place",
    "post_code": "twentemilieu: an address",
    "qr_code": "otp, tesla_fleet: named a non-secret by the issue",
    "reauth_login": "habitica: a section; its fields are checked one by one",
    "request_new_code": "icloud: a boolean",
    "set_ca_cert": "mqtt: a choice",
    "set_client_cert": "mqtt: a boolean",
    "ssl_ca_cert": "influxdb: a CA certificate, which is public",
    "station_code": "meteoclimatic: a station",
    "unauthenticated_mode": "huawei_lte: a boolean",
    "use_psk": "braviatv: a boolean",
    "verify_mqtt_certificate": "ecovacs: a boolean",
    "web_auth": "solaredge: a section; its fields are checked one by one",
    "zip_code": "an address",
}


def _string_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            out[target.id] = value.value
    return out


def config_flow_field_names() -> dict[str, set[str]]:
    """Every `vol.Required`/`vol.Optional` key in every core config flow, sections included.

    A key counts when it is a string literal, a module constant, or a constant imported from the
    component's own `const.py` or from `homeassistant.const`. Fields built at run time are not seen.
    """
    import homeassistant.components as components
    import homeassistant.const as ha_const

    root = Path(components.__file__).parent
    found: dict[str, set[str]] = {}
    for path in sorted(root.glob("*/config_flow.py")):
        tree = ast.parse(path.read_text())
        names = _string_constants(tree)
        local_const = path.parent / "const.py"
        own = _string_constants(ast.parse(local_const.read_text())) if local_const.is_file() else {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "homeassistant.const" and isinstance(getattr(ha_const, alias.name, None), str):
                    names.setdefault(local, getattr(ha_const, alias.name))
                elif node.level == 1 and node.module == "const" and alias.name in own:
                    names.setdefault(local, own[alias.name])
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if called not in ("Required", "Optional"):
                continue
            arg = node.args[0]
            key = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else names.get(arg.id) if isinstance(arg, ast.Name) else None
            if key:
                found.setdefault(key, set()).add(path.parent.name)
    return found


def test_every_credential_looking_config_flow_field_is_refused_or_reviewed() -> None:
    census = config_flow_field_names()
    assert len(census) > 500, "the scan found too little to mean anything"
    # The scan reaches the fields the issue named, sections included (solaredge's `api_key` is in one).
    for name in ("bindkey", "key", "encryption_key", "noise_psk", "client_key", "installer_code", "activation_code", "api_key"):
        assert name in census, name
    allowed = {name: sorted(where) for name, where in census.items() if CANDIDATE.search(name) and not is_secret_name(name)}
    unreviewed = {name: where for name, where in allowed.items() if name not in REVIEWED_NOT_SECRET}
    assert not unreviewed, f"read these and either refuse them in packages/shared/src/secrets.ts or list them here: {unreviewed}"
    # A reviewed name the matcher now refuses is a stale review: take it off the list. That fails,
    # because the list is then claiming something false about the matcher.
    refused_now = {name for name in REVIEWED_NOT_SECRET if is_secret_name(name)}
    assert not refused_now, f"the matcher refuses these now; take them off REVIEWED_NOT_SECRET: {refused_now}"
    # A reviewed name Home Assistant no longer has is only tidying, so an upgrade that drops a field
    # warns rather than failing CI.
    if gone := sorted(set(REVIEWED_NOT_SECRET) - set(census)):
        warnings.warn(f"no longer in any config flow, can come off REVIEWED_NOT_SECRET: {gone}", stacklevel=1)


def test_the_census_would_catch_a_new_name() -> None:
    """The control: a name the matcher misses and the candidate pattern sees is reported."""
    assert CANDIDATE.search("frobnicate_auth_blob") and not is_secret_name("frobnicate_auth_blob")
    assert "frobnicate_auth_blob" not in REVIEWED_NOT_SECRET
