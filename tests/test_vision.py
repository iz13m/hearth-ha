"""Cameras as stills, for a person in the app (AgDR-0032).

Two halves, as with doors: that a person can now see a camera, and that everything keeping the
*model* away from one still does. The picture checks use real JPEGs, because "scaled and stripped"
is a claim about bytes, and a mocked encoder would prove nothing about it.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from typing import Any

import pytest
from PIL import Image as PILImage

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai.const import CAPABILITY_FOR_METHOD, OPT_IN_CAPABILITIES
from custom_components.hearth_ai.handlers import vision
from custom_components.hearth_ai.handlers.registry import HIDDEN_DOMAINS
from custom_components.hearth_ai.policy import DENIED_ACTION_DOMAINS
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher


def _camera(core: HomeAssistant, object_id: str, *, state: str = "idle") -> str:
    entry = er.async_get(core).async_get_or_create("camera", "test", f"cam-{object_id}", suggested_object_id=object_id)
    core.states.async_set(entry.entity_id, state, {"friendly_name": object_id.title()})
    return entry.entity_id


def _jpeg(width: int, height: int, *, gps: bool = False) -> bytes:
    img = PILImage.new("RGB", (width, height), (120, 160, 200))
    out = BytesIO()
    if gps:
        exif = PILImage.Exif()
        exif[0x010F] = "PorchCam Inc"  # Make
        # GPSInfo IFD: a latitude, so there is something a leak would carry.
        exif[0x8825] = {1: "N", 2: (51.0, 30.0, 0.0), 3: "W", 4: (0.0, 7.0, 0.0)}
        img.save(out, format="JPEG", exif=exif)
    else:
        img.save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def serves(monkeypatch: pytest.MonkeyPatch):
    """Make the camera hand back chosen bytes, and record what it was asked for."""
    asked: list[dict] = []

    def _serve(content: bytes, content_type: str = "image/jpeg", error: Exception | None = None) -> list[dict]:
        async def fake(hass, entity_id, width, height):  # noqa: ANN001, ANN202
            asked.append({"entity_id": entity_id, "width": width, "height": height})
            if error is not None:
                raise error
            return content

        monkeypatch.setattr(vision, "_get_image", fake)
        return asked

    return _serve


class _FakeStream:
    """Home Assistant's `Stream`, as far as a still is concerned.

    `frames` is keyed by `wait_for_next_keyframe`, which is the whole point: a camera with no keyframe
    already buffered answers `None` to the impatient call and a picture to the patient one.
    """

    def __init__(self, frames: dict[bool, bytes | None]) -> None:
        self.frames = frames
        self.asked: list[dict] = []

    async def async_get_image(self, width=None, height=None, wait_for_next_keyframe=False):  # noqa: ANN001, ANN201
        self.asked.append({"width": width, "height": height, "wait_for_next_keyframe": wait_for_next_keyframe})
        return self.frames[wait_for_next_keyframe]


class _FakeCamera:
    """A camera entity, with the two properties a still depends on."""

    def __init__(
        self,
        *,
        still: bytes | None = None,
        stream: _FakeStream | None = None,
        use_stream_for_stills: bool = False,
        webrtc_provider: Any = None,
    ) -> None:
        self.still = still
        self.stream: _FakeStream | None = None
        self._created = stream
        self.use_stream_for_stills = use_stream_for_stills
        self.webrtc_provider = webrtc_provider
        self.asked: list[dict] = []
        self.streams_created = 0

    async def async_camera_image(self, width=None, height=None):  # noqa: ANN001, ANN201
        self.asked.append({"width": width, "height": height})
        return self.still

    async def async_create_stream(self):  # noqa: ANN201
        self.streams_created += 1
        self.stream = self._created
        return self._created


@pytest.fixture
def camera_is(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the camera component, which the test venv cannot import (it needs `turbojpeg`)."""

    def _use(cam: _FakeCamera) -> _FakeCamera:
        async def fake(hass, entity_id):  # noqa: ANN001, ANN202
            return cam

        monkeypatch.setattr(vision, "_lookup_camera", fake)
        return cam

    return _use


async def test_the_model_still_cannot_see_a_camera() -> None:
    assert "camera" in HIDDEN_DOMAINS
    assert "camera" in DENIED_ACTION_DOMAINS
    assert CAPABILITY_FOR_METHOD["vision.snapshot"] == "vision.view"
    assert "vision.view" in OPT_IN_CAPABILITIES


async def test_lists_cameras_but_entities_list_stays_blind_to_them(core: HomeAssistant) -> None:
    porch = _camera(core, "porch")
    _camera(core, "garden", state="unavailable")
    d = build_dispatcher(core)

    listed = await d.dispatch("vision.list", {})
    assert [c["entity_id"] for c in listed] == ["camera.garden", "camera.porch"]
    assert listed[1] == {"entity_id": porch, "name": "Porch", "area_id": None, "device_name": None, "state": "idle", "hearth_labels": {"entity": [], "device": []}}
    everything = await d.dispatch("entities.list", {"limit": 500})
    assert not any(e["entity_id"].startswith("camera.") for e in everything)


async def test_a_snapshot_is_scaled_to_fit_whatever_the_camera_sends(core: HomeAssistant, serves) -> None:
    porch = _camera(core, "porch")
    # A camera that ignores the requested size and sends full HD.
    asked = serves(_jpeg(1920, 1080))
    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert asked == [{"entity_id": porch, "width": 640, "height": 480}]
    assert out["content_type"] == "image/jpeg"
    assert (out["width"], out["height"]) == (640, 360)  # aspect kept, inside 640x480
    with PILImage.open(BytesIO(base64.b64decode(out["data"]))) as img:
        assert img.format == "JPEG" and img.size == (640, 360)


async def test_a_snapshot_carries_no_exif_so_no_location(core: HomeAssistant, serves) -> None:
    porch = _camera(core, "porch")
    source = _jpeg(800, 600, gps=True)
    with PILImage.open(BytesIO(source)) as original:
        assert original.getexif().get(0x8825) is not None  # the fixture really has GPS

    serves(source)
    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})
    raw = base64.b64decode(out["data"])
    with PILImage.open(BytesIO(raw)) as img:
        assert len(img.getexif()) == 0
    assert b"Exif" not in raw and b"PorchCam" not in raw


async def test_a_png_camera_still_comes_back_as_a_small_jpeg(core: HomeAssistant, serves) -> None:
    porch = _camera(core, "porch")
    png = BytesIO()
    PILImage.new("RGBA", (1000, 1000), (0, 0, 0, 128)).save(png, format="PNG")
    serves(png.getvalue(), content_type="image/png")
    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch, "width": 320})
    assert out["content_type"] == "image/jpeg"
    assert (out["width"], out["height"]) == (240, 240)  # square fits inside 320x240


async def test_refusals_say_what_went_wrong(core: HomeAssistant, serves) -> None:
    porch = _camera(core, "porch")
    dark = _camera(core, "dark", state="unavailable")
    d = build_dispatcher(core)

    for params, code in [
        ({"entity_id": "light.kitchen"}, "invalid_params"),
        ({"entity_id": "camera.nowhere"}, "not_found"),
        ({"entity_id": porch, "width": 4000}, "invalid_params"),
        ({"entity_id": porch, "width": 10}, "invalid_params"),
        ({"entity_id": dark}, "ha_error"),
    ]:
        with pytest.raises(RpcError) as err:
            await d.dispatch("vision.snapshot", params)
        assert err.value.code == code, params

    serves(b"", error=TimeoutError())
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "timeout"

    serves(b"", error=HomeAssistantError("Camera is off"))
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "ha_error" and "off" in err.value.message

    serves(b"not a picture at all")
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "ha_error"


async def test_without_pillow_nothing_is_sent_not_even_a_small_jpeg(core: HomeAssistant, serves, monkeypatch: pytest.MonkeyPatch) -> None:
    """Size is not what makes a picture safe to send; its metadata is, and only Pillow can remove it."""
    porch = _camera(core, "porch")

    def no_pillow(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise ImportError("No module named 'PIL'")

    monkeypatch.setattr(vision, "_normalise", no_pillow)
    serves(_jpeg(320, 240, gps=True))
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "ha_error"


async def test_cameras_are_off_until_the_owner_turns_them_on(core: HomeAssistant) -> None:
    from custom_components.hearth_ai.options import HearthOptions

    caps = frozenset(HearthOptions().capabilities)
    assert "vision.view" not in caps
    porch = _camera(core, "porch")
    d = build_dispatcher(core, caps)
    for method, params in [("vision.list", {}), ("vision.snapshot", {"entity_id": porch})]:
        with pytest.raises(RpcError) as err:
            await d.dispatch(method, params)
        assert err.value.code == "method_not_allowed"


async def test_a_camera_that_makes_stills_from_its_stream_waits_for_the_next_keyframe(core: HomeAssistant, camera_is) -> None:
    """The front door bug (September 2026): every still failed, for both cameras in the home.

    `camera.async_get_image` asks such a camera with `wait_for_next_keyframe=False`, which answers
    only when a keyframe is already buffered — so the picture arrived once, ever, and after that the
    app showed an empty tile. Asking for the next keyframe is what Home Assistant's own snapshot
    service does, and it is what this fake refuses to answer any other way.
    """
    porch = _camera(core, "porch")
    stream = _FakeStream({False: None, True: _jpeg(1280, 720)})
    cam = camera_is(_FakeCamera(stream=stream, use_stream_for_stills=True))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch, "width": 320})

    assert (out["width"], out["height"]) == (320, 180)
    # Asked the stream, patiently, and never bothered with a still endpoint it does not have.
    assert stream.asked == [{"width": 320, "height": 240, "wait_for_next_keyframe": True}]
    assert cam.asked == [] and cam.streams_created == 1


async def test_a_still_endpoint_that_answers_with_nothing_falls_back_to_the_stream(core: HomeAssistant, camera_is) -> None:
    """Not every camera that behaves this way says so: `use_stream_for_stills` is false and the still is empty."""
    porch = _camera(core, "porch")
    stream = _FakeStream({False: None, True: _jpeg(640, 480)})
    cam = camera_is(_FakeCamera(still=None, stream=stream))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert (out["width"], out["height"]) == (640, 480)
    assert cam.asked == [{"width": 640, "height": 480}]  # the direct still was tried first
    assert stream.asked == [{"width": 640, "height": 480, "wait_for_next_keyframe": True}]


async def test_a_camera_with_a_still_of_its_own_is_never_made_to_start_a_stream(core: HomeAssistant, camera_is) -> None:
    """A stream is a decode worker: never started for a camera that simply hands over a picture."""
    porch = _camera(core, "porch")
    cam = camera_is(_FakeCamera(still=_jpeg(800, 600)))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert (out["width"], out["height"]) == (640, 480)
    assert cam.streams_created == 0


async def test_a_camera_that_never_answers_is_a_wait_that_ran_out_not_a_refusal(
    core: HomeAssistant, camera_is, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keyframe wait has no timeout of its own, and `ha_error` would read as "your home said no"."""

    class _Silent(_FakeStream):
        async def async_get_image(self, width=None, height=None, wait_for_next_keyframe=False):  # noqa: ANN001, ANN201
            await asyncio.Event().wait()  # a camera that never sends another keyframe

    porch = _camera(core, "porch")
    camera_is(_FakeCamera(stream=_Silent({}), use_stream_for_stills=True))
    monkeypatch.setattr(vision, "SNAPSHOT_TIMEOUT_S", 0.05)

    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "timeout"


async def test_a_camera_with_no_picture_and_no_stream_says_so(core: HomeAssistant, camera_is) -> None:
    """`async_create_stream` gives back nothing when there is no stream source, and that is not a crash."""
    porch = _camera(core, "porch")
    camera_is(_FakeCamera(still=None, stream=None))

    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "ha_error" and "did not send a picture" in err.value.message


async def test_the_camera_components_own_refusal_is_passed_on(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """"Camera is off" is a better thing for a person to read than anything we could invent."""
    porch = _camera(core, "porch")

    async def off(hass, entity_id):  # noqa: ANN001, ANN202
        raise HomeAssistantError("Camera is off")

    monkeypatch.setattr(vision, "_lookup_camera", off)
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "ha_error" and "off" in err.value.message


def test_a_still_is_given_less_time_than_the_two_waits_that_contain_it() -> None:
    """The budget is not this file's to choose alone; it sits inside two ceilings it cannot see.

    The app gives a still `SNAPSHOT_TIMEOUT_MS` (`apps/mobile/src/cameras.ts`) and the hub gives up on
    the home's socket at 15 s (`RPC_TIMEOUT_MS`). Overrun either and a slow camera stops being "the
    camera did not answer" and starts looking like the whole home went quiet — and the re-encode
    still has to happen inside what is left.
    """
    assert vision.SNAPSHOT_TIMEOUT_S <= 12
    # Each stage may spend the budget, but none may promise more than there is.
    assert max(vision.STREAM_START_TIMEOUT_S, vision.KEYFRAME_TIMEOUT_S, vision.STILL_TIMEOUT_S) <= vision.SNAPSHOT_TIMEOUT_S


async def test_a_timeout_says_which_stage_ran_out(core: HomeAssistant, camera_is, monkeypatch: pytest.MonkeyPatch) -> None:
    """"The camera did not answer" is true of three different faults, and they are fixed differently.

    A camera Home Assistant cannot open a video for at all is a network or a credentials problem; one
    whose video opens but carries no new frame is a camera that is not sending. The app shows one
    sentence either way, but the hub's audit log keeps these words, and they are what someone reads
    when a household says a camera has gone dark.
    """
    porch = _camera(core, "porch")
    monkeypatch.setattr(vision, "SNAPSHOT_TIMEOUT_S", 0.1)
    d = build_dispatcher(core)

    class _SlowToOpen(_FakeCamera):
        async def async_create_stream(self):  # noqa: ANN201
            await asyncio.Event().wait()

    camera_is(_SlowToOpen(use_stream_for_stills=True))
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "timeout" and "open the camera's video" in err.value.message

    class _Silent(_FakeStream):
        async def async_get_image(self, width=None, height=None, wait_for_next_keyframe=False):  # noqa: ANN001, ANN201
            await asyncio.Event().wait()

    camera_is(_FakeCamera(stream=_Silent({}), use_stream_for_stills=True))
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "timeout" and "no new frame" in err.value.message

    class _Mute(_FakeCamera):
        async def async_camera_image(self, width=None, height=None):  # noqa: ANN001, ANN201
            await asyncio.Event().wait()

    camera_is(_Mute())
    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.snapshot", {"entity_id": porch})
    assert err.value.code == "timeout" and "answer with a picture" in err.value.message


class _FakeProvider:
    """go2rtc, which answers out of the video it already holds — no packet here to decode."""

    def __init__(self, image: bytes | None = None, error: Exception | None = None) -> None:
        self.image, self.error = image, error
        self.asked: list[dict] = []

    async def async_get_image(self, cam, width=None, height=None):  # noqa: ANN001, ANN201
        self.asked.append({"width": width, "height": height})
        if self.error is not None:
            raise self.error
        return self.image


async def test_go2rtc_answers_before_anyone_waits_for_a_keyframe(core: HomeAssistant, camera_is) -> None:
    """A separate route to the same video, and the one Home Assistant reaches for first.

    It matters for a camera whose video opens but sends this house no keyframe: go2rtc holds the
    stream already and returns a JPEG over its own REST API, with nothing to wait for.
    """
    porch = _camera(core, "porch")
    provider = _FakeProvider(_jpeg(1920, 1080))
    stream = _FakeStream({False: None, True: _jpeg(640, 480)})
    cam = camera_is(_FakeCamera(stream=stream, use_stream_for_stills=True, webrtc_provider=provider))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert (out["width"], out["height"]) == (640, 360)
    assert provider.asked == [{"width": 640, "height": 480}]
    # Nothing was decoded and no worker was started: that is the whole point of asking it first.
    assert stream.asked == [] and cam.streams_created == 0


async def test_a_provider_with_nothing_to_say_does_not_stop_the_stream(core: HomeAssistant, camera_is) -> None:
    """go2rtc that cannot reach the camera raises `HomeAssistantError`; the keyframe is still there."""
    porch = _camera(core, "porch")
    stream = _FakeStream({False: None, True: _jpeg(640, 480)})
    camera_is(_FakeCamera(stream=stream, use_stream_for_stills=True, webrtc_provider=_FakeProvider(error=HomeAssistantError("Camera has no stream source"))))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert (out["width"], out["height"]) == (640, 480)
    assert stream.asked == [{"width": 640, "height": 480, "wait_for_next_keyframe": True}]


async def test_a_still_endpoint_that_hangs_falls_back_to_the_video(core: HomeAssistant, camera_is, monkeypatch: pytest.MonkeyPatch) -> None:
    """This home's `camera.living_room`: a dead still URL and video that works.

    0.24.1 gave the still its own short slice and then refused outright when it ran out, so the
    camera's video was never tried at all. A still that hangs has to mean the same as one that comes
    back empty.
    """
    porch = _camera(core, "porch")
    monkeypatch.setattr(vision, "STILL_TIMEOUT_S", 0.05)
    stream = _FakeStream({False: None, True: _jpeg(800, 600)})

    class _DeadStillUrl(_FakeCamera):
        async def async_camera_image(self, width=None, height=None):  # noqa: ANN001, ANN201
            await asyncio.Event().wait()

    camera_is(_DeadStillUrl(stream=stream))

    out = await build_dispatcher(core).dispatch("vision.snapshot", {"entity_id": porch})

    assert (out["width"], out["height"]) == (640, 480)
    assert stream.asked == [{"width": 640, "height": 480, "wait_for_next_keyframe": True}]
