"""`ha.started`: when the box tells the hub Home Assistant has finished starting (#235).

The hub builds no voice projection until it hears this, because before HA starts an entity whose
integration is still setting up has no state and `entities.list` answers short. The hub must hear it
once, and only after HA has started. Each row is one way to get that wrong:

| test                                   | mutant                                                             |
| -------------------------------------- | ------------------------------------------------------------------ |
| older hub refuses, connection stays up | delete `async_ha_started`'s own `except RpcError`                  |
| waits for hello, not for the socket    | gate on the socket being open instead of `connected`               |
| hello on a starting HA                 | send from `_hello` whatever `hass.state` says                      |
| HA starts while hello is in flight     | read `hass.state` before awaiting `hello`, not after               |
| HA starts between the read and connect | an `await` between reading `hass.state` and `_set_connected(True)` |
| setup registers the at-started half    | drop the `async_at_started` line from `async_setup_entry`          |

`_hello` runs against a stand-in socket and a scripted `async_call`, so the frames the box sends are
recorded rather than inferred.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.client import HearthClient
from custom_components.hearth_ai.const import DOMAIN
from custom_components.hearth_ai.rpc import RpcError

from .test_conversation import ENTRY_DATA


class _Socket:
    """The part of aiohttp's socket `_hello` touches: whether it is open, and closing it."""

    def __init__(self) -> None:
        self.closed = False
        self.close = AsyncMock()


class _Hub:
    """Answers `hello`, records every method the box sends, and refuses what it is told to."""

    def __init__(self, refuse: dict[str, RpcError] | None = None) -> None:
        self.sent: list[str] = []
        self._refuse = refuse or {}

    async def __call__(self, method: str, params: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
        self.sent.append(method)
        if method in self._refuse:
            raise self._refuse[method]
        return {"installation_id": "inst-1"} if method == "hello" else {}


def _client(hass: HomeAssistant, hub: _Hub) -> tuple[HearthClient, _Socket]:
    # No capabilities, so no state subscriber is started by a successful hello.
    client = HearthClient(hass, "ws://hub.invalid/ws/integration", "secret", MagicMock(), capabilities=[])
    sock = _Socket()
    client._ws = sock  # noqa: SLF001
    client.async_call = hub  # type: ignore[method-assign]
    return client, sock


async def test_an_older_hubs_refusal_of_ha_started_keeps_the_connection(hass: HomeAssistant) -> None:
    """The twin of `test_labels.py`'s older-hub test, for a sender with no watcher to swallow for it.

    The send happens inside `_hello`'s `try`, whose `except RpcError` closes the socket. Without the
    sender's own `except`, an older hub's `method_not_allowed` would end a connection that is fine.
    """
    assert hass.state is CoreState.running
    hub = _Hub(refuse={"ha.started": RpcError("method_not_allowed", "unknown method ha.started")})
    client, sock = _client(hass, hub)
    try:
        await client._hello()  # noqa: SLF001
        assert hub.sent == ["hello", "ha.started"]
        sock.close.assert_not_awaited()
        assert client.connected is True
        assert client.last_error is None
    finally:
        client._watcher.stop()  # noqa: SLF001


async def test_ha_started_waits_for_hello_not_for_the_socket(hass: HomeAssistant) -> None:
    """Before `hello` the hub answers `unauthorized`, so an open socket is not enough to send on.

    This is the `async_at_started` callback firing during the window between connecting and the
    handshake finishing, which is also what every options reload looks like: HA is already running,
    so the callback fires at registration, before the client has a socket at all.
    """
    hub = _Hub()
    client, _ = _client(hass, hub)
    assert client.connected is False
    await client.async_ha_started(hass)
    assert hub.sent == []


async def test_hello_on_a_starting_ha_leaves_ha_started_to_at_started(hass: HomeAssistant) -> None:
    """Connected before HA finished starting: `hello` must not claim it has, and `at_started` must."""
    hub = _Hub()
    client, _ = _client(hass, hub)
    hass.set_state(CoreState.starting)
    try:
        await client._hello()  # noqa: SLF001
        assert hub.sent == ["hello"]
        assert client.connected is True

        hass.set_state(CoreState.running)
        await client.async_ha_started(hass)
        assert hub.sent == ["hello", "ha.started"]
    finally:
        hass.set_state(CoreState.running)
        client._watcher.stop()  # noqa: SLF001


async def test_ha_starting_while_hello_is_in_flight_is_still_reported(hass: HomeAssistant) -> None:
    """The one interleaving that could lose the send, so the order of two lines is pinned here.

    HA reaches running while `hello` is awaiting the hub. The at-started callback fires then, sees
    `connected` false, and leaves the send to `_hello`. `_hello` must therefore read `hass.state`
    after its await, or it acts on the "starting" it saw before and neither side sends.
    """
    hub = _Hub()
    client, _ = _client(hass, hub)
    answer_hello = hub.__call__

    async def call(method: str, params: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
        if method == "hello":
            hass.set_state(CoreState.running)
            await client.async_ha_started(hass)  # the callback, firing while hello is in flight
        return await answer_hello(method, params, timeout)

    client.async_call = call  # type: ignore[method-assign]
    hass.set_state(CoreState.starting)
    try:
        await client._hello()  # noqa: SLF001
        assert hub.sent == ["hello", "ha.started"]
    finally:
        hass.set_state(CoreState.running)
        client._watcher.stop()  # noqa: SLF001


async def test_ha_starting_between_the_state_read_and_connected(hass: HomeAssistant) -> None:
    """The no-await rule in `_hello`, which the in-flight test above cannot see (Code Reviewer, #235).

    The callback is queued, not run: `eager_start=False` holds it until `_hello` first yields. Under
    the head there is no yield between the `hass.state` read and `_set_connected(True)`, so it runs
    after and sees `connected` true. With an `await` there it runs in the gap, sees `connected`
    false and declines, while `_hello` acts on the "starting" it already read, and neither sends.
    HA starts tasks eagerly by default, and an eager task runs before `_hello` begins and passes
    under the mutant.
    """
    hub = _Hub()
    client, _ = _client(hass, hub)
    hass.set_state(CoreState.starting)

    async def at_started() -> None:  # the callback, at _hello's first yield
        hass.set_state(CoreState.running)
        await client.async_ha_started(hass)

    task = hass.async_create_task(at_started(), eager_start=False)
    try:
        await client._hello()  # noqa: SLF001
        await task
        assert hub.sent == ["hello", "ha.started"]
    finally:
        hass.set_state(CoreState.running)
        client._watcher.stop()  # noqa: SLF001

async def test_setup_sends_ha_started_when_home_assistant_finishes_starting(hass: HomeAssistant) -> None:
    """The wiring: `async_setup_entry` hands `async_ha_started` to `async_at_started`."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="inst-1", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    try:
        with patch("custom_components.hearth_ai.HearthClient.start"), patch("custom_components.hearth_ai.HearthClient.async_ha_started") as sent:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            sent.assert_not_called()

            hass.set_state(CoreState.running)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await hass.async_block_till_done()
            sent.assert_called_once()
    finally:
        hass.set_state(CoreState.running)
