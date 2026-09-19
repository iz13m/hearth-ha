"""Live video from a camera, for a person in the Hearth app (AgDR-0045).

**Only the negotiation happens here.** An SDP offer, an answer, and a handful of ICE candidates —
a few kilobytes of text — cross the home's one WebSocket. The picture never does: it goes from the
camera's video, held by go2rtc on this box, straight to the phone, over whatever path the two of
them find between themselves. That is the whole reason this is possible at all on a transport with a
1 MB frame cap and no binary frames.

**Its own capability.** `vision.live` is separate from `vision.view` and off until the owner says
otherwise, because the screen where they granted `vision.view` told them, in those words, that
pictures are "never as live video". Nothing here may be reachable from that consent.

**Three things Home Assistant leaves to us.**

- *Nobody closes a session.* HA's own frontend closes one when the browser's WebSocket goes away
  (`camera/webrtc.py:274-276`); we have no such signal, and go2rtc keeps a live consumer — and the
  camera's upstream bandwidth — for every session never closed. So each one here holds a deadline,
  the hub closes what a viewer abandons, and losing the hub closes all of them.
- *Order matters, and the messages arrive on a callback.* `send_message` is synchronous, called from
  go2rtc's reader, and a candidate that reaches the phone before the answer is discarded by the
  browser. So each session drains through **one** task: a task per message would let two `await`s on
  the socket interleave and reorder them.
- *The session id is the caller's to invent.* The hub mints it before asking, so an answer can never
  arrive for a session the hub has not yet recorded — on a warm camera it comes back that fast.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from ..const import DOMAIN, PUSH_TIMEOUT_S
from ..rpc import Dispatcher, RpcError
from .common import require_str
from .vision import _lookup_camera

_LOGGER = logging.getLogger(__name__)

DATA_SESSIONS = "hearth_ai_live_sessions"
# A phone left face up on a table must not stream all night. The app renews by opening a new session.
MAX_SESSION_S = 600
# One home, one socket: a handful of viewers is a household, more is a mistake somewhere.
MAX_SESSIONS = 8


@dataclass
class _Session:
    """One negotiation in flight, and the single queue that keeps its messages in order."""

    entity_id: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)


def _sessions(hass: HomeAssistant) -> dict[str, _Session]:
    return hass.data.setdefault(DATA_SESSIONS, {})


async def _push(hass: HomeAssistant, session_id: str, message: dict[str, Any]) -> None:
    """Hand one of Home Assistant's signalling messages to the hub, for one viewer.

    Best effort, like a state push: a viewer whose home has just dropped off has no picture coming
    either way, and a queue of stale candidates delivered after a reconnect would only confuse the
    negotiation it arrived too late for.
    """
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if getattr(e, "runtime_data", None) is not None]
    if not entries:
        return
    client = entries[0].runtime_data.client
    try:
        await client.async_call("vision.webrtc_signal", {"session_id": session_id, "message": message}, timeout=PUSH_TIMEOUT_S)
    except RpcError as err:
        # An older hub does not know the method; a busy one may not answer. Neither is ours to retry.
        _LOGGER.debug("live: the hub did not take a %s for %s: %s", message.get("type"), session_id, err)


async def _drain(hass: HomeAssistant, session_id: str, session: _Session) -> None:
    """Send this session's messages, one at a time, in the order the camera produced them."""
    while True:
        message = await session.queue.get()
        await _push(hass, session_id, message)


async def _expire(hass: HomeAssistant, session_id: str) -> None:
    await asyncio.sleep(MAX_SESSION_S)
    _LOGGER.debug("live: %s ran its full %ss and is being closed", session_id, MAX_SESSION_S)
    await close(hass, session_id)


async def close(hass: HomeAssistant, session_id: str) -> None:
    """Let go of a session: stop its tasks, and tell the camera to drop the viewer."""
    session = _sessions(hass).pop(session_id, None)
    if session is None:
        return
    for task in session.tasks:
        task.cancel()
    try:
        cam = await _lookup_camera(hass, session.entity_id)
    except HomeAssistantError:
        return  # The camera went away; there is nothing left to release.
    cam.close_webrtc_session(session_id)


async def close_all(hass: HomeAssistant) -> None:
    """Every session, because the hub is gone — nobody is watching anything through it."""
    for session_id in list(_sessions(hass)):
        await close(hass, session_id)


def _candidate_from(data: dict[str, Any]) -> Any:
    """The browser's spelling of an ICE candidate, in Home Assistant's.

    Imported here, not at module level, for the reason the camera component is: this integration must
    load on a box that has neither.
    """
    from webrtc_models import RTCIceCandidateInit  # noqa: PLC0415

    return RTCIceCandidateInit(
        candidate=data["candidate"],
        sdp_mid=data.get("sdpMid"),
        sdp_m_line_index=data.get("sdpMLineIndex"),
    )


async def vision_webrtc_config(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """What the *home* says to use to find a path — its own TURN if it has one, a STUN server if not.

    Home Assistant answers this for itself (`async_get_webrtc_client_configuration`), and go2rtc is
    given the same list, so both ends of the connection look for each other the same way.
    """
    cam = await _lookup_camera(hass, require_str(params, "entity_id"))
    config = cam.async_get_webrtc_client_configuration()
    servers: list[dict[str, Any]] = []
    for server in config.configuration.ice_servers:
        urls = server.urls if isinstance(server.urls, list) else [server.urls]
        # Allow-list the schemes: this is the one place a URL from the home crosses the wire, and it
        # is only ever allowed to be a way of finding a path, never a way of reaching the camera.
        urls = [u for u in urls if u.split(":", 1)[0] in ("stun", "stuns", "turn", "turns")]
        if not urls:
            continue
        row: dict[str, Any] = {"urls": urls}
        if server.username is not None:
            row["username"] = server.username
        if server.credential is not None:
            row["credential"] = server.credential
        servers.append(row)
    return {"ice_servers": servers[:8]}


async def vision_webrtc_offer(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Take one offer. Answers nothing but "taken" — the answer comes back the other way."""
    entity_id = require_str(params, "entity_id")
    session_id = require_str(params, "session_id")
    offer_sdp = params.get("offer_sdp")
    if not isinstance(offer_sdp, str) or not offer_sdp:
        raise RpcError("invalid_params", "offer_sdp must be a non-empty string")

    sessions = _sessions(hass)
    if session_id in sessions:
        raise RpcError("invalid_params", "that session is already open")
    if len(sessions) >= MAX_SESSIONS:
        raise RpcError("ha_error", "too many people are watching cameras in this home")

    cam = await _lookup_camera(hass, entity_id)
    session = _Session(entity_id=entity_id)
    sessions[session_id] = session
    session.tasks.append(hass.async_create_task(_drain(hass, session_id, session)))
    session.tasks.append(hass.async_create_task(_expire(hass, session_id)))

    @callback
    def send_message(message: Any) -> None:
        """Home Assistant's own words for the answer, verbatim — none of it is ours to interpret."""
        session.queue.put_nowait(message.as_dict())

    try:
        await cam.async_handle_async_webrtc_offer(offer_sdp, session_id, send_message)
    except HomeAssistantError as err:
        await close(hass, session_id)
        raise RpcError("ha_error", str(err) or "this camera cannot send live video") from err
    return {}


async def vision_webrtc_candidate(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """One of the viewer's candidates, on its way to the camera."""
    session_id = require_str(params, "session_id")
    candidate = params.get("candidate")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate"), str):
        raise RpcError("invalid_params", "candidate must be an ICE candidate")
    session = _sessions(hass).get(session_id)
    if session is None:
        raise RpcError("not_found", "no such live session")
    cam = await _lookup_camera(hass, session.entity_id)
    await cam.async_on_webrtc_candidate(session_id, _candidate_from(candidate))
    return {}


async def vision_webrtc_close(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Done watching. Idempotent: the hub also closes a session whose viewer simply vanished."""
    await close(hass, require_str(params, "session_id"))
    return {}


def register(d: Dispatcher) -> None:
    d.register("vision.webrtc_config", vision_webrtc_config)
    d.register("vision.webrtc_offer", vision_webrtc_offer)
    d.register("vision.webrtc_candidate", vision_webrtc_candidate)
    d.register("vision.webrtc_close", vision_webrtc_close)
