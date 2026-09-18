"""Cameras, as still pictures, for a person in the Hearth app (AgDR-0032).

The same shape as doors (AgDR-0025), for the same reason: a camera was refused to everyone because
the rule was written when the only thing on the other end was a model. A person looking at their own
porch through an authenticated app is not that. So:

- **The model still cannot see a camera.** `camera` stays in `HIDDEN_DOMAINS`, so it never reaches
  `entities.list` or an assistant's inventory, and it stays in the policy's denied domains, so it
  cannot be written into an automation, scene or script.
- **A person can**, through this module only, and only once the owner switches on `vision.view` — its
  own toggle, separate from device control and from doors.

**Stills, not video.** The link to the hub is one JSON WebSocket per home, shared with state pushes,
with a 1 MB frame cap and no binary frames. A still that fits in one frame is the whole of what that
transport can honestly carry; live video is WebRTC with its own signalling and a relay, and a
different project.

**The picture is made here, not trusted from the camera.** The width and height we ask a camera for
are a request it may ignore, and plenty return full size. So every still is re-encoded with Pillow:
scaled to fit `MAX_WIDTH` x `MAX_HEIGHT`, written as JPEG, and — this matters — without the camera's
EXIF block, which on some cameras carries GPS coordinates. A picture of the porch must not also be a
note of where the porch is. If Pillow is missing — it is a core Home Assistant requirement, so this
is a safeguard rather than a case — the still is refused. Passing the camera's own bytes on "when
they are small" was the first draft, and it was wrong: a small JPEG can carry a GPS block just as
well as a large one, and without Pillow there is nothing to remove it with.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from contextlib import suppress
from io import BytesIO
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..rpc import Dispatcher, RpcError
from .common import require_str
from .registry import _device_name, _entity_area

DOMAIN = "camera"
DEFAULT_LIMIT = 500

# Big enough to recognise a person at the door on a phone, small enough to leave the home's one
# socket free for everything else. 640x480 at q70 is typically 40-80 KB.
MAX_WIDTH = 640
MAX_HEIGHT = 480
MIN_WIDTH = 160
JPEG_QUALITY = 70
# Raw bytes; base64 adds a third. Leaves a wide margin under the 1 MB frame for the envelope.
MAX_BYTES = 450_000
# Inside the hub's 15 s read timeout, so a slow camera fails as "the camera did not answer" here
# rather than as an RPC timeout that looks like the home went offline. Eight seconds was too tight
# for a camera being woken from cold: opening an RTSP stream and then waiting out a group of pictures
# can spend most of it, and the one still this home ever managed took 4.3 s with the stream already
# starting.
SNAPSHOT_TIMEOUT_S = 11
# Asking Home Assistant to open the camera's video. A camera it cannot reach at all fails here.
STREAM_START_TIMEOUT_S = 4
# Waiting for the camera's next keyframe once the video is open. A group of pictures is usually one
# to four seconds; a camera that never sends one waits out the rest of the budget.
KEYFRAME_TIMEOUT_S = 8
# One direct still from a camera that has an endpoint for it. Short: its fallback needs the time.
STILL_TIMEOUT_S = 3
# go2rtc answering out of the video it already holds. No decode here, so this is quick or not at all.
PROVIDER_TIMEOUT_S = 4


def _left(deadline: float, cap: float) -> float:
    """What one stage may take: the rest of the budget, never more than the stage's own ceiling.

    The stage ceilings deliberately over-sum. Each one is there so a single hung step cannot eat the
    time the step after it needs; the deadline is what actually bounds the whole.
    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("the camera did not send a picture in time")
    return min(cap, remaining)


def _camera_state(hass: HomeAssistant, entity_id: str) -> Any:
    """The camera this id names, or a refusal.

    Assist exposure is not the gate, for the reason `access._entity` gives for locks: exposure means
    "what the assistant may see", a camera is exactly what it may never see, and asking the owner to
    expose one would be a ritual that reveals it to nothing. The owner's consent is `vision.view`.
    """
    if entity_id.split(".", 1)[0] != DOMAIN:
        raise RpcError("invalid_params", "not a camera")
    state = hass.states.get(entity_id)
    if state is None:
        raise RpcError("not_found", "no such camera")
    return state


async def vision_list(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every camera in the home. Its own method, like `access.list`, so `entities.list` stays blind to them."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    from ..labels import hearth_labels  # noqa: PLC0415

    limit = params.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise RpcError("invalid_params", "limit must be an integer between 1 and 500")
    after = params.get("after")
    if after is not None and not isinstance(after, str):
        raise RpcError("invalid_params", "after must be a string")

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    rows: list[dict[str, Any]] = []
    for state in sorted(hass.states.async_all(DOMAIN), key=lambda s: s.entity_id):
        if after is not None and state.entity_id <= after:
            continue
        ent = ent_reg.async_get(state.entity_id)
        if ent is not None and (ent.disabled_by or ent.hidden_by):
            continue
        rows.append(
            {
                "entity_id": state.entity_id,
                "name": state.name,
                "area_id": _entity_area(ent, dev_reg),
                "device_name": _device_name(ent, dev_reg),
                # idle / recording / streaming / unavailable — Home Assistant's own words.
                "state": state.state,
                # Only `hide` means anything on a camera; the hub decides (AgDR-0034).
                "hearth_labels": hearth_labels(hass, ent, dev_reg),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _normalise(content: bytes, width: int, height: int) -> tuple[bytes, int, int]:
    """Scale to fit, re-encode as JPEG, and drop every metadata block. Runs in the executor."""
    from PIL import Image, ImageOps  # noqa: PLC0415

    with Image.open(BytesIO(content)) as img:
        # Honour the camera's rotation before discarding the EXIF that says what it was.
        upright = ImageOps.exif_transpose(img)
        upright.thumbnail((width, height))
        rgb = upright.convert("RGB")
        out = BytesIO()
        # No `exif=`, no `icc_profile=`: a fresh JPEG carries only pixels.
        rgb.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue(), rgb.width, rgb.height


async def _video_still(cam: Any, width: int, height: int, deadline: float) -> bytes | None:
    """A still from the camera's video, by either of the two routes Home Assistant has to one.

    **go2rtc first**, as `camera._async_get_stream_image` does. When a home runs it — Home Assistant
    ships it — go2rtc already holds the camera's video and will return a JPEG over its own REST API,
    with no packet for us to decode and no keyframe to wait for. It is a wholly separate route from
    the one below, which matters: a camera whose video opens but sends this house no keyframe may
    still have a picture here.

    **Then the stream's next keyframe.** Creating the stream starts a decode worker, which is why
    this is the second choice — but it is what Home Assistant's own `camera.snapshot` service does,
    and for a camera with no still endpoint it is the only thing left.

    Never the impatient call. `KeyFrameConverter._generate_image` leaves the last decoded frame in
    place when no new packet is waiting, and `async_get_image` returns it either way, while the
    `Stream` object outlives the 30 s idle stop — so `wait_for_next_keyframe=False` can hand back a
    frame from a viewing half an hour ago, and we would stamp it `taken_at` now. For a camera,
    yesterday's porch presented as this moment's is worse than waiting a second for the real one.
    """
    # Best effort: a provider that cannot reach the camera raises or answers nothing, and the stream
    # below is still worth trying. `HomeAssistantError` here is go2rtc saying it has no source.
    provider = getattr(cam, "webrtc_provider", None)
    if provider is not None:
        with suppress(TimeoutError, HomeAssistantError):
            async with asyncio.timeout(_left(deadline, PROVIDER_TIMEOUT_S)):
                if image := await provider.async_get_image(cam, width=width, height=height):
                    return image

    stream = cam.stream
    if stream is None:
        allowance = _left(deadline, STREAM_START_TIMEOUT_S)
        try:
            async with asyncio.timeout(allowance):
                stream = await cam.async_create_stream()
        except TimeoutError as err:
            raise RpcError("timeout", "Home Assistant could not open the camera's video in time") from err
    if stream is None:
        return None
    allowance = _left(deadline, KEYFRAME_TIMEOUT_S)
    try:
        async with asyncio.timeout(allowance):
            return await stream.async_get_image(width=width, height=height, wait_for_next_keyframe=True)
    except TimeoutError as err:
        raise RpcError("timeout", "the camera's video carried no new frame in time") from err


async def _lookup_camera(hass: HomeAssistant, entity_id: str) -> Any:
    """The camera entity itself, in the camera component's own words when it will not give one.

    Imported here, not at module level: the camera component pulls in its own requirements
    (`turbojpeg`), which a home with no cameras may never have installed — and such a home must still
    be able to load this integration and list that it has none. It is the whole of this module's
    dependency on that component, which is what lets the tests stand in for it.

    Raises `HomeAssistantError`: "Camera not found", "Camera is off", "Camera integration not set up".
    """
    from homeassistant.components import camera  # noqa: PLC0415

    return camera.get_camera_from_entity_id(hass, entity_id)


async def _get_image(hass: HomeAssistant, entity_id: str, width: int, height: int) -> bytes:
    """Home Assistant's own in-process still. No HTTP, no token, no LAN address ever leaves the house.

    **Not `camera.async_get_image`.** That helper asks a stream-backed camera for its still with
    `wait_for_next_keyframe=False`, which answers only if a keyframe happens to be sitting in the
    buffer already, and otherwise returns nothing — instantly, or after the whole budget. For an ONVIF
    doorbell, a Nest, a Shelly, or a `generic` camera configured with only a stream URL — which is
    most front doors — that is every time but the lucky one. It also swallows `TimeoutError` and
    re-raises everything as a flat `HomeAssistantError("Unable to get image")`, so a camera that never
    answered reached the app as a refusal rather than as a wait that ran out.

    So we do what `camera.async_handle_snapshot_service` does: a camera that makes its stills from its
    stream is asked for the *next* keyframe, and any other camera is asked for a still directly —
    falling back to the stream when it answers with nothing, since `async_create_stream` gives back
    nothing of its own for a camera that has no stream to give.

    The budget is ours to impose: the keyframe wait is on a bare `asyncio.Event`, so a camera that
    never sends another one would wait forever. The decode runs in the executor, so waiting here never
    blocks the event loop.
    """
    cam = await _lookup_camera(hass, entity_id)
    deadline = monotonic() + SNAPSHOT_TIMEOUT_S
    # A backstop, a moment after the deadline the stages hold themselves to, so that whichever stage
    # ran out is the one that says so. Expiring together would leave the generic message and lose the
    # only part worth reading — which of the three faults it was.
    async with asyncio.timeout(SNAPSHOT_TIMEOUT_S + 1):
        # `getattr`: an older Home Assistant than this was written against still gets the direct
        # still and the fallback below, rather than an AttributeError.
        stream_backed = getattr(cam, "use_stream_for_stills", False)
        content = None
        # A still endpoint that hangs is not the end of it: a camera can have a dead still URL and
        # perfectly good video, which is this home's `camera.living_room`. Give it its short slice,
        # then go to the video exactly as an empty answer does — but remember, so that if the video
        # has nothing either, the refusal names the endpoint that actually hung.
        still_hung = False
        if not stream_backed:
            try:
                allowance = _left(deadline, STILL_TIMEOUT_S)
                async with asyncio.timeout(allowance):
                    content = await cam.async_camera_image(width=width, height=height)
            except TimeoutError:
                still_hung = True
        if not content:
            try:
                content = await _video_still(cam, width, height, deadline)
            except TimeoutError:
                # The still hung long enough that the video never got a turn. Its own stages raise
                # `RpcError` and those are the better words, so only the bare "out of budget" lands
                # here — and then the endpoint that hung is the whole story.
                if still_hung:
                    raise RpcError("timeout", "the camera did not answer with a picture in time") from None
                raise
    if not content:
        if still_hung:
            raise RpcError("timeout", "the camera did not answer with a picture in time")
        raise RpcError("ha_error", "the camera did not send a picture")
    return content


async def vision_snapshot(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """One still from one camera, scaled, re-encoded and stripped here — see the module docstring."""

    entity_id = require_str(params, "entity_id")
    width = params.get("width", MAX_WIDTH)
    if not isinstance(width, int) or isinstance(width, bool) or not MIN_WIDTH <= width <= MAX_WIDTH:
        raise RpcError("invalid_params", f"width must be an integer {MIN_WIDTH}..{MAX_WIDTH}")
    height = min(MAX_HEIGHT, round(width * MAX_HEIGHT / MAX_WIDTH))
    state = _camera_state(hass, entity_id)
    if state.state == "unavailable":
        raise RpcError("ha_error", "the camera is unavailable")

    try:
        raw = await _get_image(hass, entity_id, width, height)
    except TimeoutError as err:
        raise RpcError("timeout", "the camera did not send a picture in time") from err
    except HomeAssistantError as err:
        raise RpcError("ha_error", str(err) or "the camera could not send a picture") from err

    try:
        content, out_w, out_h = await hass.async_add_executor_job(_normalise, raw, width, height)
    except ImportError:
        raise RpcError("ha_error", "this Home Assistant cannot prepare camera pictures safely") from None
    except Exception as err:  # noqa: BLE001 — a camera can send anything, and a bad frame is not our crash
        raise RpcError("ha_error", "the camera sent a picture that could not be read") from err

    if len(content) > MAX_BYTES:
        raise RpcError("ha_error", "the picture is too large to send")
    return {
        "entity_id": entity_id,
        "content_type": "image/jpeg",
        "data": base64.b64encode(content).decode("ascii"),
        "width": out_w,
        "height": out_h,
        "taken_at": datetime.now(UTC).isoformat(),
    }


def register(d: Dispatcher) -> None:
    d.register("vision.list", vision_list)
    d.register("vision.snapshot", vision_snapshot)
