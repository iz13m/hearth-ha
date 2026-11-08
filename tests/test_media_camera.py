"""A camera reached through a media player (#536).

`media_player.play_media` takes a free-text `media_content_id`, and a player hands it to Home
Assistant to resolve. These tests measure what Home Assistant 2026.9.3 does with a camera named
there, against its real `camera`, `media_source` and `http` components, and then that
`devices.call` refuses it before any player is told.

The player below does exactly what Cast's `async_play_media` does with an id
(`cast/media_player.py`): resolve a `media-source://` id, then sign a URL that points at Home
Assistant with `async_process_play_media_url`. Nothing more is stubbed, so what the player ends up
holding is what a television would be sent.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component

from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

PLAYER = "media_player.tv"
FRAME = b"\xff\xd8\xff\xe0 a frame from the porch \xff\xd9"


@pytest.fixture
def real_camera() -> Iterator[None]:
    """Let Home Assistant's own camera component load in this venv, and leave no trace of it.

    The component imports `turbojpeg` at module level and uses it only to scale a still, which
    nothing here asks for, so a stand-in module is enough. Every module imported while the test runs
    is dropped afterwards: `test_vision.py` depends on the camera component *not* importing, and a
    cached module would make that depend on test order.
    """
    before = set(sys.modules)
    stub = types.ModuleType("turbojpeg")
    stub.TurboJPEG = object  # type: ignore[attr-defined]
    sys.modules["turbojpeg"] = stub
    try:
        yield
    finally:
        for name in set(sys.modules) - before:
            del sys.modules[name]


async def _home_with_a_camera_and_a_player(hass: HomeAssistant) -> list[str]:
    """Set up a camera and a cast-like player; return the list the player records what it plays in."""
    from homeassistant.components.camera import Camera  # noqa: PLC0415
    from homeassistant.components.camera.const import DATA_COMPONENT as CAMERAS  # noqa: PLC0415
    from homeassistant.components import media_source  # noqa: PLC0415
    from homeassistant.components.media_player import (  # noqa: PLC0415
        DATA_COMPONENT as PLAYERS,
        MediaPlayerEntity,
        MediaPlayerEntityFeature,
        MediaPlayerState,
        async_process_play_media_url,
    )

    await async_process_ha_core_config(hass, {"internal_url": "http://ha.local:8123"})
    assert await async_setup_component(hass, "homeassistant", {})
    for component in ("http", "media_source", "camera", "media_player"):
        assert await async_setup_component(hass, component, {}), component

    class Porch(Camera):
        _attr_name = "Porch"
        _attr_unique_id = "porch"

        async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes:
            return FRAME

    played: list[str] = []

    class Tv(MediaPlayerEntity):
        _attr_name = "TV"
        _attr_unique_id = "tv"
        _attr_state = MediaPlayerState.IDLE
        _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA

        async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
            # Cast's own two steps, in Cast's order.
            if media_source.is_media_source_id(media_id):
                media_id = (await media_source.async_resolve_media(self.hass, media_id, self.entity_id)).url
            played.append(async_process_play_media_url(self.hass, media_id))

    await hass.data[CAMERAS].async_add_entities([Porch()])
    await hass.data[PLAYERS].async_add_entities([Tv()])
    await hass.async_block_till_done()
    assert hass.states.get("camera.porch") is not None
    # The owner shared the television with Assist; the camera stays unshared and off-limits.
    async_expose_entity(hass, "conversation", PLAYER, True)
    return played


# Every spelling measured below reaches the camera on 2026.9.3 when the call is not refused.
CAMERA_IDS = [
    "media-source://camera/camera.porch",
    "/api/camera_proxy_stream/camera.porch",
    "/api/camera_proxy/camera.porch",
    "/api/camera_%70roxy/camera.porch",
    "http://ha.local:8123/api/camera_proxy/camera.porch",
]


async def test_home_assistant_turns_a_camera_media_id_into_a_signed_camera_url(hass: HomeAssistant, real_camera, hass_client_no_auth) -> None:
    """The Home Assistant half of #536, measured rather than read.

    A camera id given to a player comes back as a URL Home Assistant has **signed**, so whatever
    fetches it (a television on the LAN) gets the camera's picture with no credentials of its own.
    The camera was never shared with Assist: the media source looks the entity up directly.
    """
    played = await _home_with_a_camera_and_a_player(hass)
    for media_id in CAMERA_IDS:
        await hass.services.async_call(
            "media_player", "play_media", {"entity_id": PLAYER, "media_content_id": media_id, "media_content_type": "video"}, blocking=True
        )
    assert played[0].startswith("http://ha.local:8123/api/camera_proxy_stream/camera.porch?authSig="), played[0]
    assert all("authSig=" in url for url in played), played

    # The still proxy answers the signed URL with the frame, and the same path unsigned is refused.
    client = await hass_client_no_auth()
    for url in played[2:]:
        signed = url.removeprefix("http://ha.local:8123")
        response = await client.get(signed)
        assert response.status == 200, (url, response.status)
        assert await response.read() == FRAME
    assert (await client.get("/api/camera_proxy/camera.porch")).status == 403


@pytest.mark.parametrize("media_id", CAMERA_IDS)
async def test_devices_call_refuses_a_camera_as_a_media_id(hass: HomeAssistant, real_camera, media_id: str) -> None:
    played = await _home_with_a_camera_and_a_player(hass)
    d = build_dispatcher(hass)
    with pytest.raises(RpcError) as ei:
        await d.dispatch(
            "devices.call",
            {"domain": "media_player", "service": "play_media", "entity_id": [PLAYER], "data": {"media_content_id": media_id, "media_content_type": "video"}},
        )
    assert ei.value.code == "method_not_allowed", ei.value.message
    assert "camera or image feed" in ei.value.message
    assert played == []


async def test_devices_call_refuses_a_camera_in_the_nested_media_field(hass: HomeAssistant, real_camera) -> None:
    """`play_media` also takes `media: {media_content_id, media_content_type}` and lifts it out."""
    played = await _home_with_a_camera_and_a_player(hass)
    d = build_dispatcher(hass)
    with pytest.raises(RpcError) as ei:
        await d.dispatch(
            "devices.call",
            {
                "domain": "media_player",
                "service": "play_media",
                "entity_id": [PLAYER],
                "data": {"media": {"media_content_id": "media-source://camera/camera.porch", "media_content_type": "video"}},
            },
        )
    assert ei.value.code == "method_not_allowed"
    assert played == []


async def test_a_model_starts_no_media_at_all(hass: HomeAssistant, real_camera) -> None:
    """Option A, the owner's ruling: music and radio are refused on the model path too.

    A templated id is the reason: Home Assistant renders it after every check has run, so no content
    filter can say what it will name.
    """
    played = await _home_with_a_camera_and_a_player(hass)
    d = build_dispatcher(hass)
    for media_id in ("https://stream.example.org/radio.mp3", "/local/camera.mp3", "{{ 'media-source://cam' ~ 'era/camera.porch' }}"):
        with pytest.raises(RpcError) as ei:
            await d.dispatch(
                "devices.call",
                {"domain": "media_player", "service": "play_media", "entity_id": [PLAYER], "data": {"media_content_id": media_id, "media_content_type": "music"}},
            )
        assert (ei.value.code, ei.value.message) == ("method_not_allowed", "media_player.play_media: not allowed"), media_id
    assert played == []
