"""Outbound WebSocket client to the Hearth hub.

- Connects to `ws_url` with `Authorization: Bearer <install secret>`.
- Sends `hello`, then answers hub requests via the allowlisted dispatcher.
- Reconnects with exponential backoff + jitter; never accepts inbound connections.
- An **unclaimed** home (AgDR-0064) sends only `UNCLAIMED_HA_TO_HUB_METHODS`, answers no hub
  request, and starts nothing that pushes. Approving offers a box-made secret (`claim.py`); until a
  `hello` on it succeeds the client keeps both and tries the new one first. `CLAIMED_CLOSE_CODE` means
  "come back on the new secret"; `UNCLAIMED_EXPIRED_CLOSE_CODE` means the hub forgot the home.
- A home claimed but not yet **confirmed** by its owner in the app (`hello` says `confirmed: false`) is
  held exactly as restricted as an unclaimed one, until `CLAIM_CONFIRMED_CLOSE_CODE` says to come back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import random
import time
from typing import Any
import uuid

import aiohttp

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CAPABILITIES,
    PUSH_TIMEOUT_S,
    CAPABILITIES_VERSION,
    CHAT_TIMEOUT_S,
    CLAIM_CONFIRMED_CLOSE_CODE,
    CLAIM_TIMEOUT_S,
    CLAIMED_CLOSE_CODE,
    HEARTBEAT_TIMEOUT_S,
    HELLO_TIMEOUT_S,
    INSTALLATION_REMOVED_CLOSE_CODE,
    INTEGRATION_VERSION,
    MAX_FRAME_BYTES,
    RECONNECT_MAX_S,
    RECONNECT_MIN_S,
    UNCLAIMED_EXPIRED_CLOSE_CODE,
    UNCLAIMED_HA_TO_HUB_METHODS,
)
from .claim import new_install_secret
from .rpc import Dispatcher, RpcError
from .labels import DATA_NOTIFY, RegistryWatcher
from .subscriber import StateSubscriber

_LOGGER = logging.getLogger(__name__)


class HearthClient:
    """One persistent connection per config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        ws_url: str,
        install_secret: str,
        dispatcher: Dispatcher,
        on_status: Callable[[bool], None] | None = None,
        capabilities: list[str] | None = None,
        claimed: bool = True,
        confirmed: bool = True,
        on_confirmed: Callable[[bool], None] | None = None,
        pending_secret: str | None = None,
        on_secret_pending: Callable[[str | None], Awaitable[None]] | None = None,
        on_claimed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_expired: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._ws_url = ws_url
        self._secret = install_secret
        self._dispatcher = dispatcher
        self.capabilities: list[str] = list(capabilities) if capabilities is not None else list(CAPABILITIES)
        self._on_status = on_status
        self._task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # Pushes exposed-entity state so the app does not have to poll for it. Only runs while the
        # socket is up, and only when the owner left the capability on.
        self._subscriber: StateSubscriber | None = (
            StateSubscriber(hass, self._push_states) if "entities.subscribe" in self.capabilities else None
        )
        # Tells the hub when an owner changes a Hearth label (AgDR-0034). Not capability-gated: it
        # carries nothing, and the hub can only re-read what the owner already lets it read.
        self._watcher = RegistryWatcher(hass, self._registry_changed)
        # So a local change with no label behind it — a Hearth panel save (AgDR-0046) — reaches the
        # same "this home looks different" path the labels already use.
        hass.data[DATA_NOTIFY] = lambda: hass.loop.create_task(self._registry_changed())
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._stopping = False
        self.connected = False
        self.installation_id: str | None = None
        self.last_error: str | None = None
        self.last_frame_at: float = 0.0
        self.inflight: set[asyncio.Task[Any]] = set()
        # AgDR-0064. Starts as the entry says and is then whatever `hello` says: the hub decides.
        self.claimed = claimed
        # False while the owner has not confirmed a claimed home in the app. Absent from hello means true.
        self.confirmed = confirmed
        self._on_confirmed = on_confirmed
        # A handshake refusal of every secret in hand is retried once, after a pause, before an
        # unclaimed home is called expired: an approval can commit between the two handshakes.
        self._refusal_retried = False
        self.unclaimed_expires_at: str | None = None
        # The hub forgot this unclaimed home (or refused its secret): nothing to reconnect with.
        self.expired = False
        self._on_secret_pending = on_secret_pending
        self._on_claimed = on_claimed
        self._on_expired = on_expired
        # The secret offered in `claim.approve` and not yet proven by a `hello`. Already written down
        # (`claim.py`) before it was sent. It is never thrown away here: only a hello on it (which
        # makes it the secret) or a definite no to the call that minted it ends it.
        self._pending_secret = pending_secret
        # Tried first while it may be the hub's. Demoted once it has been refused and the old secret's
        # hello then said "unclaimed" with no approval in flight: from then on it is tried only as a
        # last resort, when the old secret is refused, so it costs no handshake on a normal reconnect.
        self._pending_demoted = False
        # Secrets the hub refused (401, or 4011 for the old) since the last successful hello. The next
        # attempt takes one not in here; when none is left, an unclaimed home has expired.
        self._refused: set[str] = set()
        # The secret the current or last attempt presented.
        self._presented: str | None = None
        # Set only by a successful hello on the current connection (defence in depth: `live`).
        self._hello_ok = False
        # Who owns the home: from an approval's reply, then from every claimed `hello` (which a hub
        # from the claim flow on reports, so a box that missed the reply still learns it).
        self.owner_email: str | None = None
        # One approval at a time: two at once would offer two secrets, and only one can win.
        self._approve_lock = asyncio.Lock()

    @property
    def hello_ok(self) -> bool:
        """A hello on the current connection succeeded: what the hub said in it is current."""
        return self._hello_ok

    @property
    def live(self) -> bool:
        """Claimed, confirmed, and said so by a hello on **this** connection: the only state in which
        anything but `hello` and `claim.*` is sent or answered."""
        return self._hello_ok and self.claimed and self.confirmed

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = self._hass.loop.create_task(self._run(), name="hearth_ai.client")

    async def stop(self) -> None:
        self._stopping = True
        self._hass.data.pop(DATA_NOTIFY, None)
        if self._ws is not None and not self._ws.closed:
            await self._ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"unloading")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RpcError("offline", "client stopped"))
        self._pending.clear()

    async def _run(self) -> None:
        delay = RECONNECT_MIN_S
        session = async_get_clientsession(self._hass)
        while not self._stopping:
            close_code: int | None = None
            try:
                close_code = await self._connect_once(session)
                delay = RECONNECT_MIN_S  # clean session → reset backoff
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                self.last_error = f"handshake {err.status}: {err.message}"
                if err.status == 401 and self._presented is not None:
                    self._refused.add(self._presented)
                    if self._next_secret() is not None:
                        # One of the two secrets is refused and the other has not been tried since the
                        # last good hello: whichever way an approval went, exactly one of them works.
                        continue
                if err.status == 401 and not self.claimed and not self._refusal_retried:
                    # Both refused at the handshake. An approval may have committed between the two
                    # (the old secret died after the new one was tried), and calling that "expired"
                    # would have the admin register a new home over a claimed one. Once more, both.
                    self._refusal_retried = True
                    self._refused.clear()
                    delay = RECONNECT_MIN_S
                elif err.status == 401 and not self.claimed:
                    # A lapsed unclaimed home is refused at the handshake once the hub has swept it,
                    # which is what a box that was off when it lapsed sees instead of the close code.
                    close_code = UNCLAIMED_EXPIRED_CLOSE_CODE
                elif err.status == 401:
                    # Claimed, confirmed or not: a 401 is ambiguous (the hub answers it for any failure
                    # to authenticate, a database blip included), so it never stops the client or
                    # starts a reauth that would overwrite the secret. Only a real 4003 close does.
                    _LOGGER.error("Hearth hub rejected the install secret (401); re-pair the integration")
                    delay = RECONNECT_MAX_S
                else:
                    _LOGGER.warning("Hearth hub handshake failed: %s", self.last_error)
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.warning("Hearth hub connection error: %s", self.last_error)
            except Exception as err:  # noqa: BLE001
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("Hearth client crashed; will reconnect")
            finally:
                self._set_connected(False)
            if self._stopping:
                break
            if close_code == INSTALLATION_REMOVED_CLOSE_CODE:
                # Deleted, in whatever state it was: live, or claimed and refused by its owner. Never a
                # reconnect (the secret is gone) and never read as confirmed: register again.
                self._expire("Azka removed this home")
                break
            if close_code == UNCLAIMED_EXPIRED_CLOSE_CODE:
                # The secret this socket used is dead. An offered secret not yet tried may be the one
                # the hub holds now (an approval that landed while this socket was closing): once.
                if self._presented is not None:
                    self._refused.add(self._presented)
                # The hub forgot this home: no approval can land after that, so a refusal of the
                # other secret is final, with no second round.
                self._refusal_retried = True
                if self._next_secret() is not None:
                    continue
                self._expire()
                break
            if close_code in (CLAIMED_CLOSE_CODE, CLAIM_CONFIRMED_CLOSE_CODE):
                # 4010: the hub took the new secret and closed the old one's socket. 4012: the owner
                # confirmed and the home is live on the same secret. Either way, straight back.
                delay = RECONNECT_MIN_S
                continue
            sleep_for = min(delay, RECONNECT_MAX_S) * (0.8 + random.random() * 0.4)
            _LOGGER.debug("reconnecting in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, RECONNECT_MAX_S)

    def _next_secret(self) -> str | None:
        """The secret to present next, or None when every one has been refused since the last hello."""
        pending = self._pending_secret
        order = [self._secret, pending] if (pending is None or self._pending_demoted) else [pending, self._secret]
        return next((s for s in order if s is not None and s not in self._refused), None)

    async def _connect_once(self, session: aiohttp.ClientSession) -> int | None:
        """One session. Returns the hub's close code, if it sent one."""
        secret = self._next_secret() or self._secret
        self._presented = secret
        self._hello_ok = False
        headers = {
            "Authorization": f"Bearer {secret}",
            "User-Agent": f"hearth_ai/{INTEGRATION_VERSION} HomeAssistant/{HA_VERSION}",
        }
        async with session.ws_connect(
            self._ws_url,
            headers=headers,
            heartbeat=None,
            max_msg_size=MAX_FRAME_BYTES,
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
            autoping=True,
        ) as ws:
            self._ws = ws
            self.last_frame_at = time.monotonic()
            hello_task = self._hass.loop.create_task(self._hello())
            try:
                await self._read_loop(ws)
                return ws.close_code
            finally:
                if not hello_task.done():
                    hello_task.cancel()
                if self._subscriber is not None:
                    self._subscriber.stop()
                self._watcher.stop()
                self._ws = None
                self._hello_ok = False  # nothing is live on a socket that is gone
                self._fail_pending("connection closed")
                # Nobody can be watching a camera through a hub we have lost, and a live session left
                # open holds a go2rtc consumer and the camera's upstream for as long as it lasts
                # (AgDR-0045).
                from .handlers.live import close_all  # noqa: PLC0415

                await close_all(self._hass)
                for t in list(self.inflight):
                    t.cancel()

    async def _hello(self) -> None:
        try:
            result = await self.async_call(
                "hello",
                {
                    "ha_version": HA_VERSION,
                    "integration_version": INTEGRATION_VERSION,
                    "capabilities": self.capabilities,
                    "capabilities_version": CAPABILITIES_VERSION,
                },
                timeout=HELLO_TIMEOUT_S,
            )
            self.installation_id = result.get("installation_id")
            self._hello_ok = True
            if (
                self._pending_secret is not None
                and self._presented == self._secret
                and self._pending_secret in self._refused
                and result.get("claimed") is False
                and not self._approve_lock.locked()
            ):
                # Shown not to have landed: the hub refused it, and the old secret still opens an
                # unclaimed home with no approval in flight. Demoted, not deleted (see `_pending_demoted`).
                self._pending_demoted = True
            self._refused.clear()
            self._refusal_retried = False
            if self._pending_secret is not None and self._presented == self._pending_secret:
                if isinstance(result.get("owner_email"), str):
                    self.owner_email = result["owner_email"]
                await self._confirm(result)
            # Absent from a hub older than the claim flow, which only authenticates claimed homes.
            self.claimed = result.get("claimed") is not False
            confirmed = result.get("confirmed") is not False
            if confirmed != self.confirmed and self._on_confirmed is not None:
                self._on_confirmed(confirmed)  # synchronous: no await before `_set_connected(True)`
            self.confirmed = confirmed
            if self.claimed and isinstance(result.get("owner_email"), str):
                self.owner_email = result["owner_email"]
            # Informational only, and never final while the socket is open: a lookup that finds a live
            # token moves it (30 minutes after registering, then a day), and the next hello says so.
            # Expiry is the hub's to decide and arrives as `UNCLAIMED_EXPIRED_CLOSE_CODE`.
            self.unclaimed_expires_at = None if self.claimed else result.get("unclaimed_expires_at")
            self.last_error = None
            # Read with no await between it and `_set_connected(True)`. `async_at_started`'s callback
            # either ran before this line, saw `connected` false and left the send to us, or runs after
            # it and sees `connected` true. There is no moment in which both of them decline.
            started = self._hass.state is CoreState.running
            self._set_connected(True)
            if not self.live:
                # Nothing pushes from a home nobody owns, or whose owner has not confirmed it: the hub
                # would refuse it. The panel's claim card is the only thing this socket is for.
                if self.claimed:
                    _LOGGER.info("connected to Azka; waiting for the owner to confirm installation %s in the app", self.installation_id)
                else:
                    _LOGGER.info("connected to Azka as unclaimed installation %s; claim it in the Azka panel", self.installation_id)
                return
            # After hello, never before: the hub rejects anything else until the handshake is done.
            if self._subscriber is not None:
                self._subscriber.start()
            self._watcher.start()
            _LOGGER.info("connected to Hearth hub as installation %s", self.installation_id)
            if started:
                await self.async_ha_started()
        except RpcError as err:
            self.last_error = f"hello failed: {err.message}"
            _LOGGER.warning("hello failed: %s", err.message)
            if self._ws is not None:
                await self._ws.close()

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            try:
                msg = await ws.receive(timeout=HEARTBEAT_TIMEOUT_S)
            except TimeoutError:
                _LOGGER.warning("no frame from hub for %ss; reconnecting", HEARTBEAT_TIMEOUT_S)
                await ws.close()
                return
            self.last_frame_at = time.monotonic()
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                _LOGGER.debug("hub closed connection: %s %s", ws.close_code, msg.extra)
                return
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise aiohttp.ClientError(f"websocket error: {ws.exception()}")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except ValueError:
                _LOGGER.warning("malformed frame from hub; ignoring")
                continue
            if not isinstance(frame, dict):
                continue
            t = frame.get("t")
            if t == "ping":
                await self._send({"t": "pong"})
            elif t == "req":
                task = self._hass.loop.create_task(self._handle_request(frame))
                self.inflight.add(task)
                task.add_done_callback(self.inflight.discard)
            elif t in ("res", "err"):
                self._resolve(frame)
            # pong / status: nothing to do

    # ----------------------------------------------------------------- inbound (hub -> HA)
    async def _handle_request(self, frame: dict[str, Any]) -> None:
        req_id = str(frame.get("id", ""))
        method = str(frame.get("method", ""))
        if not self.live:
            # The hub calls nothing on an unclaimed or unconfirmed home; if something does, it is not answered.
            await self._send({"t": "err", "id": req_id, "code": "unauthorized", "message": "this home has not been claimed yet"})
            return
        try:
            result = await self._dispatcher.dispatch(method, frame.get("params"))
            await self._send({"t": "res", "id": req_id, "result": result})
        except RpcError as err:
            await self._send({"t": "err", "id": req_id, "code": err.code, "message": err.message, "data": err.data})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("unexpected error handling %s", method)
            await self._send({"t": "err", "id": req_id, "code": "internal", "message": "internal error"})

    # ----------------------------------------------------------------- outbound (HA -> hub)
    async def async_call(self, method: str, params: dict[str, Any], timeout: float = CHAT_TIMEOUT_S) -> dict[str, Any]:
        return await self._request(method, params, timeout)

    async def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.live and method not in UNCLAIMED_HA_TO_HUB_METHODS:
            # Refused here, not left to the hub: an unclaimed home has no business asking.
            raise RpcError("unauthorized", "this home has not been claimed yet")
        ws = self._ws
        if ws is None or ws.closed:
            raise RpcError("offline", "not connected to Hearth hub")
        req_id = uuid.uuid4().hex
        fut: asyncio.Future[Any] = self._hass.loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send({"t": "req", "id": req_id, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as err:
            raise RpcError("timeout", f"hub did not answer {method} within {timeout}s") from err
        finally:
            self._pending.pop(req_id, None)

    # ----------------------------------------------------------------- claiming (AgDR-0064)
    async def async_claim(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """`claim.lookup`, `claim.approve` or `claim.decline`, for the panel."""
        if method not in ("claim.lookup", "claim.approve", "claim.decline"):
            raise RpcError("method_not_allowed", f"{method} is not a claim method")
        if method != "claim.approve":
            result = _outcome(await self._request(method, params, CLAIM_TIMEOUT_S))
            if method == "claim.lookup" and result["outcome"] == "ok":
                # The hub has just moved this home's deadline (to a day from registering) and says
                # when only on the next hello; the old 30-minute one is no longer true.
                self.unclaimed_expires_at = None
            return result
        async with self._approve_lock:
            return await self._approve(params)

    async def _approve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Offer a new install secret with the approval, written down first.

        The old secret stays too: if the reply is lost, nothing here can know whether the hub
        committed, so both are kept until a `hello` on the new one settles it.
        """
        # An unproven secret from an earlier approval is offered again, never replaced: that earlier
        # call may have committed, and then it is the only secret the hub will ever accept.
        minted = self._pending_secret is None
        secret = self._pending_secret or new_install_secret(self._secret)
        if minted:
            try:
                if self._on_secret_pending is not None:
                    await self._on_secret_pending(secret)
            except Exception as err:  # noqa: BLE001 - never offer a secret this box has not kept
                _LOGGER.exception("could not save the new install secret; the approval was not sent")
                raise RpcError("internal", "could not save the new install secret, so nothing was sent; see the log") from err
            self._pending_secret = secret
        self._pending_demoted = False  # offered again: it may be the hub's from here on
        result = _outcome(await self._request("claim.approve", {**params, "new_secret": secret}, CLAIM_TIMEOUT_S))
        if result["outcome"] == "approved":
            if isinstance(result.get("owner_email"), str):
                self.owner_email = result["owner_email"]
        elif result["outcome"] == "already_claimed" and self._pending_secret is not None:
            # An earlier approval of this box's committed, so the hub holds the pending secret and
            # this socket's secret is the stale one. Drop the socket so the next connect settles it
            # (pending first, or as the fallback once the old one is refused) rather than idling
            # restricted until the hub's own expiry.
            ws = self._ws
            if ws is not None and not ws.closed:
                await ws.close()
        elif minted and result["outcome"] in ("declined", "expired") and self._pending_secret == secret:
            # A definite no to the only offer this secret was ever part of: the hub stored nothing.
            # Never on `already_claimed`, which from this box means an earlier approval committed, and
            # never for a reused secret, which an earlier call may have landed. A hello settles those.
            self._pending_secret = None
            if self._on_secret_pending is not None:
                try:
                    await self._on_secret_pending(None)
                except Exception:  # noqa: BLE001 - a stale record costs one refused handshake, no more
                    _LOGGER.debug("could not forget an unused install secret", exc_info=True)
        return result

    async def _confirm(self, hello: dict[str, Any]) -> None:
        """A `hello` on the new secret worked: it is the only one now."""
        secret = self._pending_secret
        assert secret is not None
        self._secret = secret
        self._pending_secret = None
        if self._on_claimed is not None:
            try:
                await self._on_claimed(
                    {"install_secret": secret, "owner_email": self.owner_email, "installation_id": hello.get("installation_id")}
                )
            except Exception:  # noqa: BLE001 - the claim store still holds it, and the next setup tries it first
                _LOGGER.exception("could not record the confirmed install secret in the config entry")

    def _expire(self, reason: str = "nobody claimed this home in time, and Azka forgot it") -> None:
        self.expired = True
        self.last_error = reason
        _LOGGER.error("%s; set the integration up again to register it anew", reason)
        if self._on_expired is not None:
            self._on_expired()

    async def _push_states(self, entities: list[dict[str, Any]]) -> None:
        """
        Deliver a batch of changed entities. Best effort by design: if the socket is down or the hub
        is slow, the change is dropped rather than queued — the app's fallback poll is the floor, and
        a backlog of stale states delivered after a reconnect would fight it.
        """
        ws = self._ws
        if ws is None or ws.closed or not self.live:
            return
        await self.async_call("entities.changed", {"entities": entities}, timeout=PUSH_TIMEOUT_S)

    async def _registry_changed(self) -> None:
        """Best effort, like a state push. An older hub answers `method_not_allowed`, and the caller
        swallows it: the app's poll still catches up, just not within a second or two."""
        ws = self._ws
        if ws is None or ws.closed or not self.live:
            return
        await self.async_call("registry.changed", {}, timeout=PUSH_TIMEOUT_S)

    async def async_ha_started(self, _hass: HomeAssistant | None = None) -> None:
        """Tell the hub Home Assistant has finished starting (#235).

        Until it has, an entity whose integration is still setting up has no state, so `entities.list`
        answers short, and the hub builds no voice projection from a box that has not said this.
        Called after `hello` when HA is already running, and as the `async_at_started` callback when
        it is not. Gated on `hello` having completed, not on the socket being open: before `hello` the
        hub answers `unauthorized`.

        Best effort, with its own `except`: nothing else catches for it. Inside `_hello` an `RpcError`
        would reach the handler that closes the socket, and an older hub answers `method_not_allowed`.
        That hub builds no projection from it either way, so the refusal costs nothing.
        """
        if not self.connected or not self.live:
            return
        try:
            await self.async_call("ha.started", {}, timeout=PUSH_TIMEOUT_S)
        except RpcError as err:
            _LOGGER.debug("hub did not take ha.started: %s", err)

    def _resolve(self, frame: dict[str, Any]) -> None:
        req_id = str(frame.get("id", ""))
        fut = self._pending.get(req_id)
        if fut is None or fut.done():
            return
        if frame.get("t") == "res":
            fut.set_result(frame.get("result"))
        else:
            fut.set_exception(RpcError(str(frame.get("code", "internal")), str(frame.get("message", "")), frame.get("data")))

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RpcError("offline", reason))
        self._pending.clear()

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        await ws.send_str(json.dumps(frame, default=str))

    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            if not value:
                self.installation_id = self.installation_id  # keep last known id for diagnostics
            if self._on_status is not None:
                self._on_status(value)


def _outcome(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("outcome"), str):
        raise RpcError("internal", "the hub's answer was not understood")
    # A hub that still returned a secret would not get it past here: the panel never sees one.
    return {k: v for k, v in result.items() if k not in ("install_secret", "new_secret")}
