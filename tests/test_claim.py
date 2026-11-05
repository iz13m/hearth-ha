"""Claiming a home from inside Home Assistant (AgDR-0064).

| test                                             | what breaks it                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| contract constants match the shared schema       | a code, pattern or method drifting from `packages/shared`          |
| a claim link is read the way the hub reads it    | a looser or stricter extractor than `extractClaimToken`            |
| no token registers an unclaimed home             | the pairing token staying required                                 |
| a token still pairs, unchanged                   | the token path learning the claim keys                             |
| an unclaimed home sends only the claim methods   | dropping the guard in `_request`                                   |
| an unclaimed hello starts nothing that pushes    | starting the subscriber / watcher / `ha.started` regardless        |
| an unclaimed home answers no hub request         | dispatching on an unclaimed socket                                 |
| the offered secret is on disk before it is sent  | sending `new_secret` before `on_secret_pending` returns            |
| a lost reply keeps both secrets                  | dropping either secret before a hello settles it                   |
| 4010 comes back on the new secret                | reconnecting with backoff, or on the old secret                    |
| a lost reply / an approval that did not land     | not trying the new secret first, or not falling back to the old    |
| a restart before confirmation                    | relying on the config entry's delayed save alone                   |
| 4011 / both secrets refused: re-register         | reconnecting forever on a dead secret                              |
| panel claim commands are admin-only              | a command without `require_admin`                                  |
| lookup / approve / decline round-trip            | the secret reaching the browser, the email not shown, no audit ids |
| a malformed link never reaches the hub           | sending whatever was pasted                                        |
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai import panel as panel_mod
from custom_components.hearth_ai.claim import (
    STORAGE_KEY,
    async_confirm,
    async_pending_secret,
    async_save_pending,
    extract_claim_token,
    new_install_secret,
)
from custom_components.hearth_ai.client import HearthClient
from custom_components.hearth_ai.const import (
    CLAIM_CONFIRMED_CLOSE_CODE,
    CLAIM_LINK_PREFIX,
    CLAIM_TOKEN_PATTERN,
    CLAIMED_CLOSE_CODE,
    CONF_CLAIMED,
    CONF_HUB_URL,
    CONF_INSTALL_SECRET,
    CONF_INSTALLATION_ID,
    CONF_OWNER_EMAIL,
    CONF_UNCLAIMED_EXPIRES_AT,
    CONF_WS_URL,
    DOMAIN,
    INSTALL_SECRET_PATTERN,
    INSTALLATION_REMOVED_CLOSE_CODE,
    CONF_CONFIRMED,
    UNCLAIMED_EXPIRED_CLOSE_CODE,
    UNCLAIMED_HA_TO_HUB_METHODS,
)
from custom_components.hearth_ai.rpc import RpcError

SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "schema" / "methods.json"
HUB = "https://hub.test"
TOKEN = "hhc_" + "A" * 21 + "b"
UNCLAIMED_DATA = {
    CONF_HUB_URL: HUB,
    CONF_INSTALLATION_ID: "inst-u",
    CONF_INSTALL_SECRET: "his_old",
    CONF_WS_URL: "wss://hub.test/ws/integration",
    CONF_CLAIMED: False,
    CONF_UNCLAIMED_EXPIRES_AT: "2026-11-06T00:00:00.000Z",
}
# The hub keeps only the hash of the secret the box offered, and returns none.
APPROVED = {"outcome": "approved", "owner_email": "owner@example.com", "installation_id": "inst-u"}


# ---------------------------------------------------------------- the contract


def test_contract_constants_match_the_shared_schema() -> None:
    claim = json.loads(SCHEMA.read_text())["claim"]
    assert set(claim["unclaimed_ha_to_hub_methods"]) == UNCLAIMED_HA_TO_HUB_METHODS
    assert claim["claim_token_pattern"] == CLAIM_TOKEN_PATTERN
    assert claim["claim_link_prefix"] == CLAIM_LINK_PREFIX
    assert claim["install_secret_pattern"] == INSTALL_SECRET_PATTERN
    assert claim["claimed_close_code"] == CLAIMED_CLOSE_CODE
    assert claim["unclaimed_expired_close_code"] == UNCLAIMED_EXPIRED_CLOSE_CODE
    assert claim["claim_confirmed_close_code"] == CLAIM_CONFIRMED_CLOSE_CODE
    assert claim["installation_removed_close_code"] == INSTALLATION_REMOVED_CLOSE_CODE


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        (TOKEN, TOKEN),
        (f"  {TOKEN}\n", TOKEN),
        (f"{CLAIM_LINK_PREFIX}{TOKEN}", TOKEN),
        (f" {CLAIM_LINK_PREFIX}{TOKEN} ", TOKEN),
        (TOKEN[:-1], None),  # one character short
        (TOKEN + "x", None),  # one too many is a different string, not this token
        ("hcl_" + "A" * 22, None),  # an OAuth client id is not a claim token
        (f"{CLAIM_LINK_PREFIX}", None),
        ("", None),
        ("hhc_" + "A" * 21 + "!", None),
    ],
)
def test_a_claim_link_is_read_the_way_the_hub_reads_it(pasted: str, expected: str | None) -> None:
    assert extract_claim_token(pasted) == expected


# ---------------------------------------------------------------- config flow


def _register_reply(**over: Any) -> dict[str, Any]:
    return {
        "installation_id": "inst-u",
        "install_secret": "his_old",
        "ws_url": "wss://hub.test/ws/integration",
        "claimed": False,
        "expires_at": "2026-11-06T00:00:00.000Z",
        **over,
    }


async def _flow_deps(hass: HomeAssistant) -> None:
    # The manifest's `conversation` dependency needs `homeassistant`'s exposure store first.
    assert await async_setup_component(hass, "homeassistant", {})


async def _run_user_step(hass: HomeAssistant, user_input: dict[str, Any]) -> Any:
    await _flow_deps(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.hearth_ai.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
        await hass.async_block_till_done()
    return result


async def test_no_token_registers_an_unclaimed_home(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{HUB}/api/integration/register", json=_register_reply())
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == UNCLAIMED_DATA
    assert result["result"].unique_id == "inst-u"
    assert result["description"] == "unclaimed"
    [(method, url, body, _headers)] = aioclient_mock.mock_calls
    assert (method, str(url)) == ("POST", f"{HUB}/api/integration/register")
    # Exactly the strict `RegisterUnclaimedRequest`: no token, nothing extra.
    assert set(body) == {"ha_version", "integration_version", "name"}


async def test_an_empty_token_field_also_registers(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{HUB}/api/integration/register", json=_register_reply())
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB, "pairing_token": "   "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CLAIMED] is False


async def test_a_token_still_pairs_unchanged(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        f"{HUB}/api/integration/pair",
        json={"installation_id": "inst-p", "install_secret": "his_p", "ws_url": "wss://hub.test/ws/integration"},
    )
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB, "pairing_token": " hpt_abc "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The same four keys the token path always wrote, and no claim state at all.
    assert result["data"] == {CONF_HUB_URL: HUB, CONF_INSTALLATION_ID: "inst-p", CONF_INSTALL_SECRET: "his_p", CONF_WS_URL: "wss://hub.test/ws/integration"}
    [(_m, url, body, _h)] = aioclient_mock.mock_calls
    assert str(url) == f"{HUB}/api/integration/pair"
    assert body["pairing_token"] == "hpt_abc"


@pytest.mark.parametrize(
    ("status", "reply", "error"),
    [
        (404, {}, "register_unsupported"),
        (503, {}, "hub_full"),
        (429, {}, "register_rate_limited"),  # five an hour, not pairing's "wait a minute"
        (200, _register_reply(claimed=True), "cannot_connect"),  # not an unclaimed home
        (200, _register_reply(ws_url="ws://hub.test/ws"), "cannot_connect"),  # the secret in cleartext
    ],
)
async def test_registration_refusals_are_shown_on_the_form(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, reply: dict[str, Any], error: str
) -> None:
    aioclient_mock.post(f"{HUB}/api/integration/register", status=status, json=reply)
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (429, {"error": "rate_limited", "message": "slow down"}, "register_rate_limited"),
        (503, {"error": "hub_full", "message": "full"}, "hub_full"),
        (400, {"error": "invalid_request", "message": "bad"}, "register_invalid"),
        # The body decides when it names an error: a proxy's status must not override the hub's word.
        (500, {"error": "rate_limited", "message": "slow down"}, "register_rate_limited"),
        # And an error this form does not know falls back to the status.
        (503, {"error": "something_new", "message": "?"}, "hub_full"),
        (400, {}, "cannot_connect"),
    ],
)
async def test_registration_refusals_are_read_from_their_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, body: dict[str, Any], error: str
) -> None:
    aioclient_mock.post(f"{HUB}/api/integration/register", status=status, json=body)
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB})
    assert result["errors"] == {"base": error}


async def test_a_pairing_refusal_is_still_read_from_its_status(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{HUB}/api/integration/pair", status=401, json={"error": "hub_full"})
    result = await _run_user_step(hass, {CONF_HUB_URL: HUB, "pairing_token": "hpt_x"})
    assert result["errors"] == {"base": "invalid_token"}


async def test_reauth_registers_a_new_home_for_the_same_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """What the expired-home notification leads to: a new unclaimed home, same entry, same options."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA, options={"connection_enabled": True})
    entry.add_to_hass(hass)
    await _flow_deps(hass)
    aioclient_mock.post(f"{HUB}/api/integration/register", json=_register_reply(installation_id="inst-v", install_secret="his_v"))
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.hearth_ai.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == "inst-v"
    assert entry.data[CONF_INSTALL_SECRET] == "his_v"
    assert entry.data[CONF_CLAIMED] is False
    assert entry.options == {"connection_enabled": True}


# ---------------------------------------------------------------- the client, unclaimed


class _Wire:
    """A stand-in socket: records what the box sends, and lets a scripted hub answer through `_resolve`."""

    def __init__(self, client: HearthClient, answers: dict[str, Any] | None = None) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._client = client
        self._answers = answers or {}

    async def send_str(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if frame.get("t") == "req" and frame["method"] in self._answers:
            answer = self._answers[frame["method"]]
            reply = {"t": "res", "id": frame["id"], "result": answer(frame["params"]) if callable(answer) else answer}
            asyncio.get_running_loop().call_soon(self._client._resolve, reply)  # noqa: SLF001

    async def close(self, **_: Any) -> None:
        self.closed = True

    @property
    def methods(self) -> list[str]:
        return [f["method"] for f in self.sent if f.get("t") == "req"]


def _unclaimed_client(hass: HomeAssistant, answers: dict[str, Any] | None = None, **kw: Any) -> tuple[HearthClient, _Wire]:
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=["entities.subscribe"], claimed=False, **kw)
    wire = _Wire(client, answers)
    client._ws = wire  # noqa: SLF001
    return client, wire


@pytest.mark.parametrize("method", ["entities.changed", "registry.changed", "ha.started", "account.status", "chat.process"])
async def test_an_unclaimed_home_sends_only_the_claim_methods(hass: HomeAssistant, method: str) -> None:
    client, wire = _unclaimed_client(hass)
    with pytest.raises(RpcError) as err:
        await client.async_call(method, {}, timeout=1)
    assert err.value.code == "unauthorized"
    assert wire.sent == []


async def test_the_claim_methods_do_go_out_while_unclaimed(hass: HomeAssistant) -> None:
    client, wire = _unclaimed_client(hass, {"claim.lookup": {"outcome": "expired"}})
    assert await client.async_claim("claim.lookup", {"token": TOKEN}) == {"outcome": "expired"}
    assert wire.methods == ["claim.lookup"]


async def test_an_unclaimed_hello_starts_nothing_that_pushes(hass: HomeAssistant) -> None:
    client, wire = _unclaimed_client(hass, {"hello": {"installation_id": "inst-u", "server_time": "t", "claimed": False, "unclaimed_expires_at": "2026-11-06T00:00:00Z"}})
    with patch.object(client._subscriber, "start") as sub, patch.object(client._watcher, "start") as watch:  # noqa: SLF001
        await client._hello()  # noqa: SLF001
    assert client.connected is True and client.claimed is False
    assert client.unclaimed_expires_at == "2026-11-06T00:00:00Z"
    sub.assert_not_called()
    watch.assert_not_called()
    assert wire.methods == ["hello"]  # and no `ha.started`, although HA is running
    await client._registry_changed()  # noqa: SLF001 - a panel save while unclaimed sends nothing
    await client._push_states([{"entity_id": "light.a"}])  # noqa: SLF001
    assert wire.methods == ["hello"]


async def test_a_claimed_hello_from_an_old_hub_is_claimed(hass: HomeAssistant) -> None:
    """`claimed` is absent from a hub older than the claim flow, which only knows claimed homes."""
    client, _ = _unclaimed_client(hass, {"hello": {"installation_id": "inst-u", "server_time": "t"}, "ha.started": {}})
    try:
        await client._hello()  # noqa: SLF001
        assert client.claimed is True
    finally:
        client._watcher.stop()  # noqa: SLF001
        if client._subscriber is not None:  # noqa: SLF001
            client._subscriber.stop()  # noqa: SLF001


async def test_a_claimed_home_is_not_live_before_its_hello(hass: HomeAssistant) -> None:
    """Defence in depth: a request that arrives on a new socket before hello is never dispatched,
    whatever the last connection said."""
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[])
    wire = _Wire(client)
    client._ws = wire  # noqa: SLF001
    assert client.claimed and client.confirmed and not client.live
    await client._handle_request({"t": "req", "id": "r0", "method": "entities.list", "params": {}})  # noqa: SLF001
    client._dispatcher.dispatch.assert_not_called()  # noqa: SLF001
    assert wire.sent[-1]["code"] == "unauthorized"
    with pytest.raises(RpcError):
        await client.async_call("entities.changed", {"entities": []}, timeout=1)


async def test_a_live_flag_does_not_survive_into_the_next_connection(hass: HomeAssistant) -> None:
    client, _ = _unclaimed_client(hass, {"hello": {"installation_id": "inst-u", "server_time": "t"}, "ha.started": {}})
    try:
        await client._hello()  # noqa: SLF001
        assert client.live
    finally:
        client._watcher.stop()  # noqa: SLF001
        client._subscriber.stop()  # noqa: SLF001

    class _Never:
        def ws_connect(self, *a: Any, **k: Any) -> Any:
            raise aiohttp.ClientError("down")

    with pytest.raises(aiohttp.ClientError):
        await client._connect_once(_Never())  # type: ignore[arg-type]  # noqa: SLF001
    assert not client.live


async def test_an_unclaimed_home_answers_no_hub_request(hass: HomeAssistant) -> None:
    client, wire = _unclaimed_client(hass)
    await client._handle_request({"t": "req", "id": "r1", "method": "entities.list", "params": {}})  # noqa: SLF001
    client._dispatcher.dispatch.assert_not_called()  # noqa: SLF001
    assert wire.sent == [{"t": "err", "id": "r1", "code": "unauthorized", "message": "this home has not been claimed yet"}]


async def test_approving_offers_a_secret_written_down_before_it_is_sent(hass: HomeAssistant) -> None:
    """The hub keeps only the hash of what the box offers, so the box must already have kept it."""
    events: list[str] = []

    async def on_secret_pending(secret: str | None) -> None:
        await asyncio.sleep(0)  # a real write yields; the send must still wait for it
        events.append(f"saved:{secret}")

    def approve(params: dict[str, Any]) -> dict[str, Any]:
        events.append(f"sent:{params['new_secret']}")
        return APPROVED

    client, wire = _unclaimed_client(hass, {"claim.approve": approve}, on_secret_pending=on_secret_pending)
    out = await client.async_claim("claim.approve", {"token": TOKEN})
    secret = wire.sent[0]["params"]["new_secret"]
    assert re.fullmatch(INSTALL_SECRET_PATTERN, secret)  # the hub's own `newSecret("his")` shape
    assert events == [f"saved:{secret}", f"sent:{secret}"]
    assert out == APPROVED
    assert secret not in json.dumps(out)
    # Both kept: the old one is still the one in use until a hello on the new one proves it.
    assert client._secret == "his_old"  # noqa: SLF001
    assert client._pending_secret == secret  # noqa: SLF001


def test_the_offered_secret_is_never_the_one_in_use() -> None:
    """The hub refuses a `new_secret` equal to the pre-claim secret; draw again rather than send it."""
    body = "A" * 43
    with patch("custom_components.hearth_ai.claim.secrets.token_urlsafe", side_effect=[body, "B" * 43]):
        assert new_install_secret(f"his_{body}") == "his_" + "B" * 43


async def test_each_approval_offers_a_fresh_secret(hass: HomeAssistant) -> None:
    client, wire = _unclaimed_client(hass, {"claim.approve": {"outcome": "expired"}})
    await client.async_claim("claim.approve", {"token": TOKEN})
    await client.async_claim("claim.approve", {"token": TOKEN})
    first, second = (f["params"]["new_secret"] for f in wire.sent)
    assert first != second


async def test_already_claimed_drops_the_socket_so_a_reconnect_settles_it(hass: HomeAssistant) -> None:
    """The hub holds the pending secret; this socket runs on the stale one. Drop it now, rather than
    sit restricted until the hub's own expiry."""
    client, wire = _unclaimed_client(hass, {"claim.approve": {"outcome": "already_claimed"}})
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert wire.closed is True
    assert client._pending_secret is not None  # noqa: SLF001


@pytest.mark.parametrize("outcome", ["expired", "declined"])
async def test_a_definite_no_keeps_the_socket(hass: HomeAssistant, outcome: str) -> None:
    client, wire = _unclaimed_client(hass, {"claim.approve": {"outcome": outcome}})
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert wire.closed is False


@pytest.mark.parametrize("outcome", ["expired", "declined"])
async def test_a_definite_no_forgets_the_offered_secret(hass: HomeAssistant, outcome: str) -> None:
    saved: list[str | None] = []

    async def on_secret_pending(secret: str | None) -> None:
        saved.append(secret)

    client, _ = _unclaimed_client(hass, {"claim.approve": {"outcome": outcome}}, on_secret_pending=on_secret_pending)
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert client._pending_secret is None  # noqa: SLF001
    assert saved[-1] is None and len(saved) == 2


async def test_already_claimed_never_forgets_the_offered_secret(hass: HomeAssistant) -> None:
    """From this box, `already_claimed` can only mean an earlier approval of its own committed."""
    saved: list[str | None] = []

    async def on_secret_pending(secret: str | None) -> None:
        saved.append(secret)

    client, wire = _unclaimed_client(hass, {"claim.approve": {"outcome": "already_claimed"}}, on_secret_pending=on_secret_pending)
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert client._pending_secret == wire.sent[-1]["params"]["new_secret"]  # noqa: SLF001
    assert None not in saved


async def test_timeout_then_retry_keeps_the_first_secret(hass: HomeAssistant) -> None:
    """The first approval may have committed: a retry must offer the same secret, not a new one."""
    saved: list[str | None] = []

    async def on_secret_pending(secret: str | None) -> None:
        saved.append(secret)

    client, wire = _unclaimed_client(hass, on_secret_pending=on_secret_pending)  # the hub does not answer
    with patch("custom_components.hearth_ai.client.CLAIM_TIMEOUT_S", 0.01), pytest.raises(RpcError):
        await client.async_claim("claim.approve", {"token": TOKEN})
    first = wire.sent[-1]["params"]["new_secret"]
    wire._answers["claim.approve"] = APPROVED  # noqa: SLF001
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert wire.sent[-1]["params"]["new_secret"] == first
    assert client._pending_secret == first  # noqa: SLF001
    assert saved == [first]  # written once, never overwritten


@pytest.mark.parametrize("outcome", ["already_claimed", "expired", "declined"])
async def test_an_ambiguous_approve_followed_by_any_answer_keeps_it(hass: HomeAssistant, outcome: str) -> None:
    """A reused secret was part of an earlier, unanswered approval: no later answer may drop it."""
    saved: list[str | None] = []

    async def on_secret_pending(secret: str | None) -> None:
        saved.append(secret)

    client, wire = _unclaimed_client(hass, on_secret_pending=on_secret_pending)
    with patch("custom_components.hearth_ai.client.CLAIM_TIMEOUT_S", 0.01), pytest.raises(RpcError):
        await client.async_claim("claim.approve", {"token": TOKEN})
    first = wire.sent[-1]["params"]["new_secret"]
    wire._answers["claim.approve"] = {"outcome": outcome}  # noqa: SLF001
    await client.async_claim("claim.approve", {"token": TOKEN})
    assert client._pending_secret == first  # noqa: SLF001
    assert saved == [first]


async def test_a_lost_reply_keeps_both_secrets(hass: HomeAssistant) -> None:
    """Whether the hub committed cannot be known from here, so neither secret may be dropped."""
    client, wire = _unclaimed_client(hass)  # the hub never answers
    with patch("custom_components.hearth_ai.client.CLAIM_TIMEOUT_S", 0.01), pytest.raises(RpcError) as err:
        await client.async_claim("claim.approve", {"token": TOKEN})
    assert err.value.code == "timeout"
    assert client._pending_secret == wire.sent[-1]["params"]["new_secret"]  # noqa: SLF001
    assert client._secret == "his_old"  # noqa: SLF001


async def test_a_secret_that_cannot_be_saved_is_never_offered(hass: HomeAssistant) -> None:
    async def on_secret_pending(secret: str | None) -> None:
        raise OSError("disk full")

    client, wire = _unclaimed_client(hass, {"claim.approve": APPROVED}, on_secret_pending=on_secret_pending)
    with pytest.raises(RpcError) as err:
        await client.async_claim("claim.approve", {"token": TOKEN})
    assert "nothing was sent" in err.value.message
    assert wire.sent == []
    assert client._pending_secret is None  # noqa: SLF001


UNCONFIRMED_HELLO = {"installation_id": "inst-u", "server_time": "t", "claimed": True, "confirmed": False, "owner_email": "owner@example.com"}


async def test_an_unconfirmed_home_is_held_like_an_unclaimed_one(hass: HomeAssistant) -> None:
    """Claimed in the panel, not yet confirmed by its owner in the app: the hub offers nothing, so
    the box starts nothing, sends nothing but hello and claim.*, and answers no request."""
    client, wire = _unclaimed_client(hass, {"hello": UNCONFIRMED_HELLO, "claim.lookup": {"outcome": "already_claimed"}})
    with patch.object(client._subscriber, "start") as sub, patch.object(client._watcher, "start") as watch:  # noqa: SLF001
        await client._hello()  # noqa: SLF001
    assert (client.claimed, client.confirmed, client.live) == (True, False, False)
    assert client.owner_email == "owner@example.com"
    sub.assert_not_called()
    watch.assert_not_called()
    assert wire.methods == ["hello"]  # no ha.started
    with pytest.raises(RpcError) as err:
        await client.async_call("account.status", {}, timeout=1)
    assert err.value.code == "unauthorized"
    await client._registry_changed()  # noqa: SLF001
    await client._push_states([{"entity_id": "light.a"}])  # noqa: SLF001
    await client._handle_request({"t": "req", "id": "r1", "method": "entities.list", "params": {}})  # noqa: SLF001
    client._dispatcher.dispatch.assert_not_called()  # noqa: SLF001
    assert wire.sent[-1] == {"t": "err", "id": "r1", "code": "unauthorized", "message": "this home has not been claimed yet"}
    assert (await client.async_claim("claim.lookup", {"token": TOKEN}))["outcome"] == "already_claimed"
    assert wire.methods == ["hello", "claim.lookup"]


# ---------------------------------------------------------------- the client's loop: 4010, fallback and 4011


class _Socket:
    """One scripted hub session for `_run`: answers hello and the claim calls, then closes with a code."""

    def __init__(self, test: _Session, hello: dict[str, Any], close_after: str | None = None, close_code: int | None = None) -> None:
        self._test = test
        self._hello = hello
        self._close_after = close_after
        self.close_code: int | None = None
        self._pending_close = close_code
        self.closed = False
        self._inbox: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        if close_after is None and close_code is not None:
            self._shut()

    def _shut(self) -> None:
        self._inbox.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, self._pending_close, ""))

    async def receive(self, timeout: float | None = None) -> aiohttp.WSMessage:
        msg = await self._inbox.get()
        if msg.type is aiohttp.WSMsgType.CLOSE:
            self.close_code = msg.data
            self.closed = True
        return msg

    async def send_str(self, data: str) -> None:
        frame = json.loads(data)
        if frame.get("t") != "req":
            return
        method = frame["method"]
        self._test.methods.append(method)
        if method == "claim.approve":
            # What the hub does in its transaction: the offered secret replaces the old one.
            self._test.accept = {frame["params"]["new_secret"]}
        result = self._hello if method == "hello" else self._test.answers[method]
        if method == self._test.drop_reply_to:
            self._inbox.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1006, ""))
            return
        self._inbox.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({"t": "res", "id": frame["id"], "result": result}), ""))
        if method == self._close_after:
            self._shut()  # the answer is on the wire first, as the hub sends it

    async def close(self, **_: Any) -> None:
        self.closed = True

    async def __aenter__(self) -> _Socket:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    """Refuses (401) any secret the scripted hub does not currently accept, like the real handshake."""

    def __init__(self, sockets: list[Any], accept: set[str], answers: dict[str, Any] | None = None, drop_reply_to: str | None = None) -> None:
        self.sockets = sockets
        self.accept = accept
        self.answers = answers or {}
        self.drop_reply_to = drop_reply_to
        self.methods: list[str] = []
        self.secrets: list[str] = []

    def ws_connect(self, url: str, headers: dict[str, str], **_: Any) -> Any:
        secret = headers["Authorization"].removeprefix("Bearer ")
        self.secrets.append(secret)
        if secret not in self.accept:
            raise aiohttp.WSServerHandshakeError(MagicMock(real_url=url), (), status=401, message="Unauthorized")
        if not self.sockets:
            raise aiohttp.ClientError("no more scripted sessions")
        return self.sockets.pop(0)


UNCLAIMED_HELLO = {"installation_id": "inst-u", "server_time": "t", "claimed": False, "unclaimed_expires_at": "2026-11-06T00:00:00Z"}
CLAIMED_HELLO = {"installation_id": "inst-u", "server_time": "t", "claimed": True}


async def _drive(hass: HomeAssistant, client: HearthClient, session: _Session, until: Any) -> None:
    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session):
        task = hass.loop.create_task(client._run())  # noqa: SLF001
        try:
            await asyncio.wait_for(until(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            client._watcher.stop()  # noqa: SLF001


def _loop_client(hass: HomeAssistant, confirmed: list[dict[str, Any]], **kw: Any) -> HearthClient:
    async def on_claimed(info: dict[str, Any]) -> None:
        confirmed.append(info)

    return HearthClient(hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=[], claimed=False, on_claimed=on_claimed, **kw)


async def test_4010_comes_back_on_the_new_secret_and_confirms_it(hass: HomeAssistant) -> None:
    confirmed: list[dict[str, Any]] = []
    session = _Session([], {"his_old"}, {"claim.approve": APPROVED, "ha.started": {}})
    client = _loop_client(hass, confirmed)
    session.sockets = [
        _Socket(session, UNCLAIMED_HELLO, close_after="claim.approve", close_code=CLAIMED_CLOSE_CODE),
        _Socket(session, CLAIMED_HELLO),
    ]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)
        assert (await client.async_claim("claim.approve", {"token": TOKEN}))["outcome"] == "approved"
        while not (client.connected and client.claimed):
            await asyncio.sleep(0)

    # A backoff of a minute would outlast the test's five seconds: the reconnect after 4010 has none.
    with patch("custom_components.hearth_ai.client.RECONNECT_MIN_S", 60):
        await _drive(hass, client, session, until)
    new = next(iter(session.accept))
    assert session.secrets == ["his_old", new]  # no backoff, straight back on the new secret
    assert confirmed == [{"install_secret": new, "owner_email": "owner@example.com", "installation_id": "inst-u"}]
    assert client._secret == new and client._pending_secret is None  # noqa: SLF001


async def test_a_lost_approval_reply_is_settled_by_the_new_secret_working(hass: HomeAssistant) -> None:
    """The hub committed and the socket died before its answer: the box never heard "approved", and
    still comes back, because it tries the secret it offered before the one it had."""
    confirmed: list[dict[str, Any]] = []
    session = _Session([], {"his_old"}, {"claim.approve": APPROVED, "ha.started": {}}, drop_reply_to="claim.approve")
    client = _loop_client(hass, confirmed)
    session.sockets = [_Socket(session, UNCLAIMED_HELLO), _Socket(session, {**CLAIMED_HELLO, "owner_email": "owner@example.com"})]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)
        with pytest.raises(RpcError):
            await client.async_claim("claim.approve", {"token": TOKEN})
        while not (client.connected and client.claimed):
            await asyncio.sleep(0)

    with patch("custom_components.hearth_ai.client.RECONNECT_MIN_S", 0):
        await _drive(hass, client, session, until)
    new = next(iter(session.accept))
    assert session.secrets == ["his_old", new]
    assert confirmed[0]["install_secret"] == new
    # Never heard from the approval; the claimed hello says whose it is.
    assert confirmed[0]["owner_email"] == "owner@example.com"
    assert client.owner_email == "owner@example.com"


async def test_an_approval_that_did_not_land_falls_back_to_the_old_secret(hass: HomeAssistant) -> None:
    """The other half of a lost reply: the hub never stored the new secret, so it 401s and the old
    one still works. Both stay kept, so a commit that lands late is still survivable."""
    confirmed: list[dict[str, Any]] = []
    client = _loop_client(hass, confirmed, pending_secret="his_offered")
    session = _Session([], {"his_old"})
    session.sockets = [_Socket(session, UNCLAIMED_HELLO)]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)

    await _drive(hass, client, session, until)
    assert session.secrets == ["his_offered", "his_old"]  # new first, old straight after
    assert client.claimed is False and client.expired is False
    assert client._pending_secret == "his_offered"  # noqa: SLF001
    assert confirmed == []


async def test_a_secret_shown_not_to_land_stops_costing_a_handshake(hass: HomeAssistant) -> None:
    """Refused, then the old secret opened an unclaimed home with no approval in flight: from then on
    the old one goes first, and the offered one is kept only as a last resort."""
    confirmed: list[dict[str, Any]] = []
    client = _loop_client(hass, confirmed, pending_secret="his_offered")
    session = _Session([], {"his_old"})
    first = _Socket(session, UNCLAIMED_HELLO)
    session.sockets = [first, _Socket(session, UNCLAIMED_HELLO)]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)
        first._pending_close = 1000  # noqa: SLF001 - an ordinary drop
        first._shut()  # noqa: SLF001
        while len(session.secrets) < 3 or not client.connected:
            await asyncio.sleep(0)

    with patch("custom_components.hearth_ai.client.RECONNECT_MIN_S", 0):
        await _drive(hass, client, session, until)
    assert session.secrets == ["his_offered", "his_old", "his_old"]
    assert client._pending_secret == "his_offered"  # noqa: SLF001 - demoted, not deleted


async def test_no_demotion_while_an_approval_is_in_flight(hass: HomeAssistant) -> None:
    """A refused pending secret and an unclaimed hello on the old one say nothing while an approval is
    still being answered: that approval may yet make the pending secret the hub's."""
    client, _ = _unclaimed_client(hass, {"hello": UNCLAIMED_HELLO})
    client._pending_secret = "his_offered"  # noqa: SLF001
    client._refused = {"his_offered"}  # noqa: SLF001
    client._presented = "his_old"  # noqa: SLF001
    async with client._approve_lock:  # noqa: SLF001 - an approval in flight
        await client._hello()  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    assert client._pending_demoted is False  # noqa: SLF001
    # The same hello with nothing in flight does demote it, so the lock is what made the difference.
    client._refused = {"his_offered"}  # noqa: SLF001
    await client._hello()  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    assert client._pending_demoted is True  # noqa: SLF001


async def test_a_demoted_secret_is_still_tried_when_the_old_one_dies(hass: HomeAssistant) -> None:
    """The commit that landed late: the old secret is refused, and the demoted one is the hub's."""
    confirmed: list[dict[str, Any]] = []
    client = _loop_client(hass, confirmed, pending_secret="his_offered")
    client._pending_demoted = True  # noqa: SLF001
    session = _Session([], {"his_offered"}, {"ha.started": {}})
    session.sockets = [_Socket(session, CLAIMED_HELLO)]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)

    await _drive(hass, client, session, until)
    assert session.secrets == ["his_old", "his_offered"]
    assert confirmed[0]["install_secret"] == "his_offered"
    assert client.expired is False


async def test_4012_comes_straight_back_on_the_same_secret_and_is_live(hass: HomeAssistant) -> None:
    session = _Session([], {"his_p"}, {"ha.started": {}})
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[])
    session.sockets = [_Socket(session, UNCONFIRMED_HELLO, close_code=None), _Socket(session, CLAIMED_HELLO)]
    first = session.sockets[0]

    async def until() -> None:
        while not client.connected:
            await asyncio.sleep(0)
        assert client.live is False
        first._pending_close = CLAIM_CONFIRMED_CLOSE_CODE  # noqa: SLF001 - the owner confirms in the app
        first._shut()  # noqa: SLF001
        while not (client.connected and client.live):
            await asyncio.sleep(0)

    # A minute's backoff would outlast the five seconds `_drive` allows.
    with patch("custom_components.hearth_ai.client.RECONNECT_MIN_S", 60):
        await _drive(hass, client, session, until)
    assert session.secrets == ["his_p", "his_p"]
    assert "ha.started" in session.methods  # live now: it says Home Assistant is running


async def test_4011_tries_an_untried_offered_secret_once_before_expiring(hass: HomeAssistant) -> None:
    """The socket the approval went out on closes as the home lapses; the approval may still have
    landed. The offered secret gets one try before the home is called forgotten."""
    confirmed: list[dict[str, Any]] = []
    expired = MagicMock()
    client = _loop_client(hass, confirmed, pending_secret="his_offered", on_expired=expired)
    client._pending_demoted = True  # noqa: SLF001 - so the old secret is the one presented first
    session = _Session([], {"his_old", "his_offered"}, {"ha.started": {}})
    session.sockets = [_Socket(session, UNCLAIMED_HELLO, close_code=UNCLAIMED_EXPIRED_CLOSE_CODE), _Socket(session, CLAIMED_HELLO)]

    async def until() -> None:
        while not (client.connected and client.claimed):
            await asyncio.sleep(0)

    await _drive(hass, client, session, until)
    assert session.secrets == ["his_old", "his_offered"]
    expired.assert_not_called()
    assert confirmed[0]["install_secret"] == "his_offered"


async def test_4011_with_the_offered_secret_refused_too_expires(hass: HomeAssistant) -> None:
    expired = MagicMock()
    client = HearthClient(
        hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=[], claimed=False, pending_secret="his_offered", on_expired=expired
    )
    client._pending_demoted = True  # noqa: SLF001
    session = _Session([], {"his_old"})
    session.sockets = [_Socket(session, UNCLAIMED_HELLO, close_code=UNCLAIMED_EXPIRED_CLOSE_CODE)]
    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session):
        await asyncio.wait_for(client._run(), 5)  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    assert session.secrets == ["his_old", "his_offered"]
    expired.assert_called_once()


@pytest.mark.parametrize("hello", ["unconfirmed", "live"])
async def test_4003_stops_and_asks_for_a_new_registration(hass: HomeAssistant, hello: str) -> None:
    """The hub deleted the installation: an owner answered "not my home" in the app (unconfirmed), or
    removed a live home. Either way the secret is dead: no retry, never confirmed, and the reauth."""
    expired = MagicMock()
    greeting = UNCONFIRMED_HELLO if hello == "unconfirmed" else {**CLAIMED_HELLO, "owner_email": "owner@example.com"}
    session = _Session([], {"his_p"}, {"ha.started": {}})
    first = _Socket(session, greeting)
    session.sockets = [first, _Socket(session, CLAIMED_HELLO)]
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[], on_expired=expired)
    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session):
        task = hass.loop.create_task(client._run())  # noqa: SLF001
        while not client.connected:
            await asyncio.sleep(0)
        assert client.live is (hello == "live")
        first._pending_close = INSTALLATION_REMOVED_CLOSE_CODE  # noqa: SLF001 - the hub deletes it
        first._shut()  # noqa: SLF001
        await asyncio.wait_for(task, 5)  # returns by itself: nothing to reconnect with
    client._watcher.stop()  # noqa: SLF001
    assert session.secrets == ["his_p"]
    expired.assert_called_once()
    assert client.expired is True and client.live is False
    if hello == "unconfirmed":
        assert client.confirmed is False


async def test_an_unconfirmed_home_refused_at_the_handshake_keeps_backing_off(hass: HomeAssistant) -> None:
    """A 401 is ambiguous: the hub answers it for any failure to authenticate, a database blip
    included. So a claimed home, confirmed or not, never stops or starts a reauth on one (that reauth
    would overwrite the secret of a home that may be fine). Only a real 4003 close stops it."""
    expired = MagicMock()
    session = _Session([], set())
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[], confirmed=False, on_expired=expired)

    async def until() -> None:
        while len(session.secrets) < 3:
            await asyncio.sleep(0)

    with patch("custom_components.hearth_ai.client.RECONNECT_MAX_S", 0):
        await _drive(hass, client, session, until)
    assert session.secrets[:3] == ["his_p", "his_p", "his_p"]
    expired.assert_not_called()
    assert client.expired is False


class _Scripted(_Session):
    """Accepts the secret only on the attempts listed (0-based), refusing it on every other."""

    def __init__(self, sockets: list[Any], secret: str, open_on: set[int]) -> None:
        super().__init__(sockets, set())
        self._secret = secret
        self._open_on = open_on

    def ws_connect(self, url: str, headers: dict[str, str], **kw: Any) -> Any:
        self.accept = {self._secret} if len(self.secrets) in self._open_on else set()
        return super().ws_connect(url, headers, **kw)


async def test_a_good_hello_earns_a_fresh_retry_before_expiry(hass: HomeAssistant) -> None:
    """Refused, then connected, then refused again: the second refusal gets its own retry, because
    the first one's was spent before a hello showed the home was still there."""
    expired = MagicMock()
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=[], claimed=False, on_expired=expired)
    session = _Scripted([], "his_old", open_on={1})
    first = _Socket(session, UNCLAIMED_HELLO)
    session.sockets = [first]

    async def drop_after_hello() -> None:
        while not client.connected:
            await asyncio.sleep(0)
        first._pending_close = 1000  # noqa: SLF001 - an ordinary drop
        first._shut()  # noqa: SLF001

    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session), patch(
        "custom_components.hearth_ai.client.RECONNECT_MIN_S", 0
    ):
        dropper = hass.loop.create_task(drop_after_hello())
        await asyncio.wait_for(client._run(), 5)  # noqa: SLF001
        await dropper
    client._watcher.stop()  # noqa: SLF001
    # refused, retried and opened, then refused, retried, refused, expired
    assert len(session.secrets) == 4
    expired.assert_called_once()


async def test_a_confirmed_home_refused_at_the_handshake_keeps_its_old_behaviour(hass: HomeAssistant) -> None:
    """Token-paired or confirmed: the long-standing "re-pair" log and backoff, unchanged."""
    expired = MagicMock()
    session = _Session([], set())
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[], on_expired=expired)

    async def until() -> None:
        while len(session.secrets) < 1:
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

    await _drive(hass, client, session, until)
    expired.assert_not_called()
    assert session.secrets == ["his_p"]  # and now waiting out a long backoff


async def test_the_confirmation_state_survives_a_restart(hass: HomeAssistant) -> None:
    """Kept in the entry, so after a restart the box is restricted from the start and the panel does
    not say "Owned by" for a home its owner has not confirmed."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    data = {**UNCLAIMED_DATA, CONF_CLAIMED: True}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=data)
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    client = entry.runtime_data.client
    client._ws = _Wire(client, {"hello": UNCONFIRMED_HELLO})  # noqa: SLF001
    await client._hello()  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    assert entry.data[CONF_CONFIRMED] is False
    assert await hass.config_entries.async_reload(entry.entry_id) is not False
    await hass.async_block_till_done()
    assert entry.runtime_data.client.confirmed is False
    # And confirming takes it back out.
    client = entry.runtime_data.client
    client._ws = _Wire(client, {"hello": CLAIMED_HELLO, "ha.started": {}})  # noqa: SLF001
    await client._hello()  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    if client._subscriber is not None:  # noqa: SLF001
        client._subscriber.stop()  # noqa: SLF001
    assert CONF_CONFIRMED not in entry.data


async def test_4011_stops_and_asks_for_a_new_registration(hass: HomeAssistant) -> None:
    expired = MagicMock()
    session = _Session([], {"his_old"})
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=[], claimed=False, on_expired=expired)
    session.sockets = [_Socket(session, UNCLAIMED_HELLO, close_code=UNCLAIMED_EXPIRED_CLOSE_CODE)]

    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session):
        await asyncio.wait_for(client._run(), 5)  # noqa: SLF001 - returns by itself: nothing to retry with
    client._watcher.stop()  # noqa: SLF001
    assert client.expired is True
    expired.assert_called_once()
    assert session.secrets == ["his_old"]  # one connect, no retry on a dead secret


async def test_a_refused_unclaimed_secret_is_an_expired_home(hass: HomeAssistant) -> None:
    """A box that was off when its home lapsed meets a 401 at the handshake, not the close code. With
    an offered secret in hand, only both being refused, twice, means that."""
    expired = MagicMock()
    session = _Session([], set())
    client = HearthClient(hass, "wss://hub.test/ws/integration", "his_old", MagicMock(), capabilities=[], claimed=False, pending_secret="his_offered", on_expired=expired)
    with patch("custom_components.hearth_ai.client.async_get_clientsession", return_value=session), patch(
        "custom_components.hearth_ai.client.RECONNECT_MIN_S", 0
    ):
        await asyncio.wait_for(client._run(), 5)  # noqa: SLF001
    assert session.secrets == ["his_offered", "his_old", "his_offered", "his_old"]
    expired.assert_called_once()


class _CommitsLate(_Session):
    """The approval commits between the box's two handshakes: the old secret dies after the new one
    was already refused. From the third attempt on, only the new one opens."""

    def ws_connect(self, url: str, headers: dict[str, str], **kw: Any) -> Any:
        if len(self.secrets) == 1:
            self.accept = {"his_offered"}
        return super().ws_connect(url, headers, **kw)


async def test_a_commit_between_the_two_handshakes_is_not_called_expired(hass: HomeAssistant) -> None:
    confirmed: list[dict[str, Any]] = []
    expired = MagicMock()
    client = _loop_client(hass, confirmed, pending_secret="his_offered", on_expired=expired)
    session = _CommitsLate([], {"his_old"}, {"ha.started": {}})
    session.accept = set()  # the new secret is not the hub's yet, and the old one is about to die
    session.sockets = [_Socket(session, CLAIMED_HELLO)]

    async def until() -> None:
        while not (client.connected and client.claimed):
            await asyncio.sleep(0)

    with patch("custom_components.hearth_ai.client.RECONNECT_MIN_S", 0):
        await _drive(hass, client, session, until)
    assert session.secrets == ["his_offered", "his_old", "his_offered"]
    expired.assert_not_called()
    assert confirmed[0]["install_secret"] == "his_offered"


async def test_expiry_starts_home_assistants_reauth(hass: HomeAssistant) -> None:
    """The wiring: `on_expired` is the entry's reauth, which is the repair Home Assistant shows."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA)
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    client = entry.runtime_data.client
    assert client.claimed is False
    client._expire()  # noqa: SLF001
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [config_entries.SOURCE_REAUTH]


# ---------------------------------------------------------------- the store, and a restart before the entry saved


async def test_a_restart_before_confirmation_tries_the_offered_secret_first(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Home Assistant stopped after offering a secret and before the entry's delayed save of it (or
    before any reply). The entry comes back with the old secret; setup hands the client the new one too."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA)
    entry.add_to_hass(hass)
    await async_save_pending(hass, entry, "his_offered")
    assert hass_storage[STORAGE_KEY]["data"][entry.entry_id] == {"installation_id": "inst-u", "new_secret": "his_offered"}
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    client = entry.runtime_data.client
    assert client._pending_secret == "his_offered"  # noqa: SLF001
    assert client._secret == "his_old"  # noqa: SLF001


async def test_confirming_makes_the_entry_claimed(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA)
    entry.add_to_hass(hass)
    await async_confirm(hass, entry, {"install_secret": "his_new", "owner_email": "owner@example.com", "installation_id": "inst-u"})
    assert entry.data[CONF_INSTALL_SECRET] == "his_new"
    assert entry.data[CONF_CLAIMED] is True
    assert entry.data[CONF_OWNER_EMAIL] == "owner@example.com"
    assert CONF_UNCLAIMED_EXPIRES_AT not in entry.data


async def test_a_record_is_kept_until_a_later_run_sees_the_entry_saved(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA)
    entry.add_to_hass(hass)
    await async_save_pending(hass, entry, "his_new")
    await async_confirm(hass, entry, {"install_secret": "his_new", "installation_id": "inst-u"})
    # Same run (an options reload): the entry's own save may not have landed, so the record stays.
    assert await async_pending_secret(hass, entry) is None
    assert entry.entry_id in hass_storage[STORAGE_KEY]["data"]
    # A later run read the entry back claimed: now the record goes.
    hass.data.pop("hearth_ai_claimed_this_run", None)
    assert await async_pending_secret(hass, entry) is None
    assert entry.entry_id not in hass_storage[STORAGE_KEY]["data"]


async def test_two_entries_saving_at_once_keep_both_records(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """One store file for every entry: two read-modify-writes must not interleave."""
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    a = MockConfigEntry(domain=DOMAIN, unique_id="inst-a", data={**UNCLAIMED_DATA, CONF_INSTALLATION_ID: "inst-a"})
    b = MockConfigEntry(domain=DOMAIN, unique_id="inst-b", data={**UNCLAIMED_DATA, CONF_INSTALLATION_ID: "inst-b"})
    a.add_to_hass(hass)
    b.add_to_hass(hass)
    real_load = Store.async_load

    async def slow_load(self: Store) -> Any:
        data = await real_load(self)
        await asyncio.sleep(0.01)  # a disk read yields; the other entry's save runs here without the lock
        return data

    with patch.object(Store, "async_load", slow_load):
        await asyncio.gather(async_save_pending(hass, a, "his_a"), async_save_pending(hass, b, "his_b"))
    records = hass_storage[STORAGE_KEY]["data"]
    assert {records[a.entry_id]["new_secret"], records[b.entry_id]["new_secret"]} == {"his_a", "his_b"}


async def test_a_stale_record_is_never_offered(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """After the entry re-registered under a new id, a secret offered for the old home is worthless."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA)
    entry.add_to_hass(hass)
    await async_save_pending(hass, entry, "his_offered")
    hass.config_entries.async_update_entry(entry, data={**UNCLAIMED_DATA, CONF_INSTALLATION_ID: "inst-v"}, unique_id="inst-v")
    assert await async_pending_secret(hass, entry) is None
    assert entry.entry_id not in hass_storage[STORAGE_KEY]["data"]


# ---------------------------------------------------------------- the panel's claim card


@pytest.fixture
async def ws_panel(hass: HomeAssistant) -> HomeAssistant:
    assert await async_setup_component(hass, "websocket_api", {})
    panel_mod._register_commands(hass)  # noqa: SLF001
    await hass.async_block_till_done()
    return hass


def _loaded_entry(hass: HomeAssistant, client: HearthClient) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=UNCLAIMED_DATA, title="Azka AI")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(client=client)
    return entry


async def _send(ws: Any, payload: dict[str, Any]) -> dict[str, Any]:
    await ws.send_json_auto_id(payload)
    return await ws.receive_json()


@pytest.mark.parametrize("command", ["status", "lookup", "approve", "decline"])
async def test_every_claim_command_is_admin_only(ws_panel: HomeAssistant, hass_ws_client, hass_admin_user, command: str) -> None:
    hass_admin_user.groups = []
    ws = await hass_ws_client(ws_panel)
    extra = {"status": {}, "lookup": {"entry_id": "x", "link": TOKEN}}.get(command, {"entry_id": "x", "token": TOKEN})
    msg = await _send(ws, {"type": f"hearth_ai/panel/claim/{command}", **extra})
    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"


async def test_lookup_approve_and_decline_round_trip(ws_panel: HomeAssistant, hass_ws_client, hass_admin_user, hass_storage) -> None:
    hass = ws_panel
    answers: dict[str, Any] = {
        "claim.lookup": {"outcome": "ok", "email": "owner@example.com", "expires_at": "2026-11-05T03:10:00Z"},
        "claim.decline": {"outcome": "declined"},
        "claim.approve": APPROVED,
    }
    entry_ref: dict[str, MockConfigEntry] = {}
    stored_when_sent: list[Any] = []

    def approve(params: dict[str, Any]) -> dict[str, Any]:
        stored_when_sent.append(hass_storage[STORAGE_KEY]["data"][entry_ref["e"].entry_id]["new_secret"])
        return APPROVED

    answers["claim.approve"] = approve
    answers["hello"] = {"installation_id": "inst-u", "server_time": "t", "claimed": True, "owner_email": "owner@example.com"}
    answers["ha.started"] = {}
    client, wire = _unclaimed_client(
        hass,
        answers,
        on_secret_pending=lambda secret: async_save_pending(hass, entry_ref["e"], secret),
        on_claimed=lambda info: async_confirm(hass, entry_ref["e"], info),
    )
    client.connected = True
    client.unclaimed_expires_at = "2026-11-05T03:30:00Z"  # 30 minutes after registering, from hello
    entry = entry_ref["e"] = _loaded_entry(hass, client)
    ws = await hass_ws_client(hass)

    msg = await _send(ws, {"type": "hearth_ai/panel/claim/status"})
    assert msg["result"]["homes"] == [
        {"entry_id": entry.entry_id, "title": "Azka AI", "claimed": False, "confirmed": True, "connected": True, "expired": False, "expires_at": "2026-11-05T03:30:00Z", "owner_email": None}
    ]

    msg = await _send(ws, {"type": "hearth_ai/panel/claim/lookup", "entry_id": entry.entry_id, "link": f" {CLAIM_LINK_PREFIX}{TOKEN} "})
    assert msg["result"] == {"outcome": "ok", "email": "owner@example.com", "expires_at": "2026-11-05T03:10:00Z", "token": TOKEN}
    assert wire.sent[-1]["params"] == {"token": TOKEN}  # a lookup names nobody
    # A live token moved the hub's deadline, and the box hears the new one only on its next hello:
    # the 30-minute one must not be shown as final meanwhile.
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/status"})
    assert msg["result"]["homes"][0]["expires_at"] is None

    msg = await _send(ws, {"type": "hearth_ai/panel/claim/decline", "entry_id": entry.entry_id, "token": TOKEN})
    assert msg["result"]["outcome"] == "declined"
    # Decline carries no secret: there is nothing to rotate.
    assert wire.sent[-1]["params"] == {"token": TOKEN, "ha_user_id": hass_admin_user.id, "ha_user_name": hass_admin_user.name}

    msg = await _send(ws, {"type": "hearth_ai/panel/claim/approve", "entry_id": entry.entry_id, "token": TOKEN})
    assert msg["success"]
    assert msg["result"] == {"outcome": "approved", "owner_email": "owner@example.com", "installation_id": "inst-u", "token": TOKEN}
    offered = wire.sent[-1]["params"]
    assert set(offered) == {"token", "ha_user_id", "ha_user_name", "new_secret"}
    assert re.fullmatch(INSTALL_SECRET_PATTERN, offered["new_secret"]) and offered["new_secret"] != "his_old"
    assert stored_when_sent == [offered["new_secret"]]  # on disk before it went out
    assert offered["new_secret"] not in json.dumps(msg)  # the browser never sees the secret
    assert wire.methods == ["claim.lookup", "claim.decline", "claim.approve"]
    assert entry.data[CONF_INSTALL_SECRET] == "his_old"  # not until a hello proves the new one

    # The hub closes with 4010 and the box comes back on the new secret; the card says whose it is.
    client._presented = offered["new_secret"]  # noqa: SLF001 - what `_connect_once` presented
    await client._hello()  # noqa: SLF001
    client._watcher.stop()  # noqa: SLF001
    await hass.async_block_till_done()
    assert entry.data[CONF_INSTALL_SECRET] == offered["new_secret"]
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/status"})
    assert msg["result"]["homes"][0]["claimed"] is True
    assert msg["result"]["homes"][0]["owner_email"] == "owner@example.com"


@pytest.mark.parametrize("link", ["hhc_short", f"{CLAIM_LINK_PREFIX}hhc_short", "https://example.com/claim", "hcl_" + "A" * 22])
async def test_a_malformed_link_never_reaches_the_hub(ws_panel: HomeAssistant, hass_ws_client, link: str) -> None:
    client, wire = _unclaimed_client(ws_panel)
    client.connected = True
    entry = _loaded_entry(ws_panel, client)
    ws = await hass_ws_client(ws_panel)
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/lookup", "entry_id": entry.entry_id, "link": link})
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/approve", "entry_id": entry.entry_id, "token": link})
    assert msg["error"]["code"] == "invalid_format"
    assert wire.sent == []


async def test_an_offline_home_says_so(ws_panel: HomeAssistant, hass_ws_client) -> None:
    client, wire = _unclaimed_client(ws_panel)
    entry = _loaded_entry(ws_panel, client)  # never connected
    ws = await hass_ws_client(ws_panel)
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/lookup", "entry_id": entry.entry_id, "link": TOKEN})
    assert msg["error"]["code"] == "offline"
    assert wire.sent == []


async def test_owned_by_comes_from_the_hubs_hello_too(ws_panel: HomeAssistant, hass_ws_client) -> None:
    """A home claimed while this box missed the reply, or paired with a token, still names its owner."""
    client = HearthClient(ws_panel, "wss://hub.test/ws/integration", "his_p", MagicMock(), capabilities=[])
    wire = _Wire(client, {"hello": {"installation_id": "inst-u", "server_time": "t", "claimed": True, "owner_email": "o@example.com"}, "ha.started": {}})
    client._ws = wire  # noqa: SLF001
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data={k: v for k, v in UNCLAIMED_DATA.items() if k not in (CONF_CLAIMED, CONF_UNCLAIMED_EXPIRES_AT)})
    entry.add_to_hass(ws_panel)
    entry.mock_state(ws_panel, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(client=client)
    try:
        await client._hello()  # noqa: SLF001
    finally:
        client._watcher.stop()  # noqa: SLF001
    ws = await hass_ws_client(ws_panel)
    msg = await _send(ws, {"type": "hearth_ai/panel/claim/status"})
    assert msg["result"]["homes"][0]["owner_email"] == "o@example.com"


async def test_the_card_says_whose_confirmation_it_waits_for(ws_panel: HomeAssistant, hass_ws_client) -> None:
    client, _ = _unclaimed_client(ws_panel, {"hello": UNCONFIRMED_HELLO})
    entry = _loaded_entry(ws_panel, client)
    try:
        await client._hello()  # noqa: SLF001
    finally:
        client._watcher.stop()  # noqa: SLF001
    ws = await hass_ws_client(ws_panel)
    home = (await _send(ws, {"type": "hearth_ai/panel/claim/status"}))["result"]["homes"][0]
    assert home["entry_id"] == entry.entry_id
    assert (home["claimed"], home["confirmed"], home["owner_email"]) == (True, False, "owner@example.com")


async def test_diagnostics_never_carry_the_owner_or_a_secret(hass: HomeAssistant) -> None:
    from custom_components.hearth_ai.diagnostics import async_get_config_entry_diagnostics  # noqa: PLC0415

    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    data = {**UNCLAIMED_DATA, CONF_CLAIMED: True, CONF_INSTALL_SECRET: "his_secretsecret", CONF_OWNER_EMAIL: "owner@example.com"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-u", data=data)
    entry.add_to_hass(hass)
    with patch("custom_components.hearth_ai.HearthClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data.client._pending_secret = "his_pendingpending"  # noqa: SLF001
    out = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for secret in ("owner@example.com", "his_secretsecret", "his_pendingpending", "his_"):
        assert secret not in out


async def test_the_card_does_not_trust_a_confirmation_from_an_earlier_connection(ws_panel: HomeAssistant, hass_ws_client) -> None:
    """Before a hello on the current connection, the panel goes by what the entry kept."""
    client, _ = _unclaimed_client(ws_panel)
    client.claimed = True
    client.confirmed = True  # stale: not from a hello on this connection
    entry = _loaded_entry(ws_panel, client)
    ws_panel.config_entries.async_update_entry(entry, data={**entry.data, CONF_CLAIMED: True, CONF_CONFIRMED: False})
    ws = await hass_ws_client(ws_panel)
    home = (await _send(ws, {"type": "hearth_ai/panel/claim/status"}))["result"]["homes"][0]
    assert home["confirmed"] is False
