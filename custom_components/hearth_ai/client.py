"""Outbound WebSocket client to the Hearth hub.

- Connects to `ws_url` with `Authorization: Bearer <install secret>`.
- Sends `hello`, then answers hub requests via the allowlisted dispatcher.
- Reconnects with exponential backoff + jitter; never accepts inbound connections.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import random
import time
from typing import Any
import uuid

import aiohttp

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CAPABILITIES,
    CAPABILITIES_VERSION,
    CHAT_TIMEOUT_S,
    HEARTBEAT_TIMEOUT_S,
    HELLO_TIMEOUT_S,
    INTEGRATION_VERSION,
    MAX_FRAME_BYTES,
    RECONNECT_MAX_S,
    RECONNECT_MIN_S,
)
from .rpc import Dispatcher, RpcError

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
    ) -> None:
        self._hass = hass
        self._ws_url = ws_url
        self._secret = install_secret
        self._dispatcher = dispatcher
        self.capabilities: list[str] = list(capabilities) if capabilities is not None else list(CAPABILITIES)
        self._on_status = on_status
        self._task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._stopping = False
        self.connected = False
        self.installation_id: str | None = None
        self.last_error: str | None = None
        self.last_frame_at: float = 0.0
        self.inflight: set[asyncio.Task[Any]] = set()

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = self._hass.loop.create_task(self._run(), name="hearth_ai.client")

    async def stop(self) -> None:
        self._stopping = True
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
            try:
                await self._connect_once(session)
                delay = RECONNECT_MIN_S  # clean session → reset backoff
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                self.last_error = f"handshake {err.status}: {err.message}"
                if err.status == 401:
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
            sleep_for = min(delay, RECONNECT_MAX_S) * (0.8 + random.random() * 0.4)
            _LOGGER.debug("reconnecting in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _connect_once(self, session: aiohttp.ClientSession) -> None:
        headers = {
            "Authorization": f"Bearer {self._secret}",
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
            finally:
                if not hello_task.done():
                    hello_task.cancel()
                self._ws = None
                self._fail_pending("connection closed")
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
            self.last_error = None
            self._set_connected(True)
            _LOGGER.info("connected to Hearth hub as installation %s", self.installation_id)
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

    def _resolve(self, frame: dict[str, Any]) -> None:
        fut = self._pending.get(str(frame.get("id", "")))
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
