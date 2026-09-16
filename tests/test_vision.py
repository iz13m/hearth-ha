"""Cameras as stills, for a person in the app (AgDR-0032).

Two halves, as with doors: that a person can now see a camera, and that everything keeping the
*model* away from one still does. The picture checks use real JPEGs, because "scaled and stripped"
is a claim about bytes, and a mocked encoder would prove nothing about it.
"""

from __future__ import annotations

import base64
from io import BytesIO

from dataclasses import dataclass

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


@dataclass
class _Image:
    """The shape of Home Assistant's `camera.Image`, without importing the camera component."""

    content_type: str
    content: bytes


@pytest.fixture
def serves(monkeypatch: pytest.MonkeyPatch):
    """Make the camera hand back chosen bytes, and record what it was asked for."""
    asked: list[dict] = []

    def _serve(content: bytes, content_type: str = "image/jpeg", error: Exception | None = None) -> list[dict]:
        async def fake(hass, entity_id, width, height):  # noqa: ANN001, ANN202
            asked.append({"entity_id": entity_id, "width": width, "height": height})
            if error is not None:
                raise error
            return _Image(content_type=content_type, content=content)

        monkeypatch.setattr(vision, "_get_image", fake)
        return asked

    return _serve


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
    assert listed[1] == {"entity_id": porch, "name": "Porch", "area_id": None, "device_name": None, "state": "idle"}
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
