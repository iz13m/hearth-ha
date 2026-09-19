"""Live video, for a person in the app (AgDR-0045).

The picture itself is never here — it goes from go2rtc to the phone — so what these tests are about
is the three things Home Assistant leaves to us: that the messages reach the hub **in the order the
camera made them**, that a session is always let go of, and that none of it is reachable without the
owner's separate yes.

The camera component cannot be imported in this venv (it wants `turbojpeg`), and neither can
`webrtc_models` (a broken `orjson` wheel on this architecture), which is why `live` keeps both behind
`_lookup_camera` and `_candidate_from` — two seams these fakes stand in at.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.hearth_ai.const import CAPABILITIES, CAPABILITY_FOR_METHOD, OPT_IN_CAPABILITIES
from custom_components.hearth_ai.handlers import live
from custom_components.hearth_ai.rpc import RpcError, build_dispatcher

SESSION = "sess-00000001"


def _camera(core: HomeAssistant, object_id: str = "porch") -> str:
    entry = er.async_get(core).async_get_or_create("camera", "test", f"cam-{object_id}", suggested_object_id=object_id)
    core.states.async_set(entry.entity_id, "idle", {"friendly_name": object_id.title()})
    return entry.entity_id


class _Answer:
    """What Home Assistant hands the callback. `as_dict()` is its own; we never rewrite it."""

    def __init__(self, sdp: str) -> None:
        self.sdp = sdp

    def as_dict(self) -> dict[str, Any]:
        return {"type": "answer", "answer": self.sdp}


class _Candidate:
    def __init__(self, candidate: str) -> None:
        self.candidate = candidate

    def as_dict(self) -> dict[str, Any]:
        return {"type": "candidate", "candidate": {"candidate": self.candidate}}


class _FakeCamera:
    """A camera that negotiates, as far as this module can tell one from a real one."""

    def __init__(self, *, error: Exception | None = None, burst: list[Any] | None = None) -> None:
        self.error = error
        self.burst = burst or []
        self.offers: list[tuple[str, str]] = []
        self.candidates: list[tuple[str, Any]] = []
        self.closed: list[str] = []

    async def async_handle_async_webrtc_offer(self, offer_sdp, session_id, send_message):  # noqa: ANN001, ANN201
        if self.error is not None:
            raise self.error
        self.offers.append((session_id, offer_sdp))
        for message in self.burst:
            send_message(message)

    async def async_on_webrtc_candidate(self, session_id, candidate):  # noqa: ANN001, ANN201
        self.candidates.append((session_id, candidate))

    def close_webrtc_session(self, session_id) -> None:  # noqa: ANN001
        self.closed.append(session_id)


@pytest.fixture
def camera_is(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the camera component, and collect what would have gone to the hub."""
    sent: list[dict[str, Any]] = []

    async def _use(cam: _FakeCamera) -> tuple[_FakeCamera, list[dict[str, Any]]]:
        async def lookup(hass, entity_id):  # noqa: ANN001, ANN202
            return cam

        async def push(hass, session_id, message):  # noqa: ANN001, ANN202
            sent.append({"session_id": session_id, **message})

        monkeypatch.setattr(live, "_lookup_camera", lookup)
        monkeypatch.setattr(live, "_push", push)
        monkeypatch.setattr(live, "_candidate_from", lambda data: data)
        return cam, sent

    return _use


async def _settle() -> None:
    """Let the one drain task per session run: ordering is the thing being tested, not timing."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_live_is_its_own_yes_and_needs_the_still_one_too(core: HomeAssistant) -> None:
    """The owner who allowed stills was told, in that screen, "never as live video"."""
    assert "vision.live" in CAPABILITIES
    assert "vision.live" in OPT_IN_CAPABILITIES
    for method in ("vision.webrtc_config", "vision.webrtc_offer", "vision.webrtc_candidate", "vision.webrtc_close"):
        assert CAPABILITY_FOR_METHOD[method] == "vision.live"

    porch = _camera(core)
    # Nothing switched on at all.
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core, frozenset()).dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0"})
    assert err.value.code == "method_not_allowed"
    # Live without stills: refused rather than left to mean something stranger further in.
    with pytest.raises(RpcError) as err:
        await build_dispatcher(core, frozenset({"vision.live"})).dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0"})
    assert err.value.code == "method_not_allowed" and "vision.view" in err.value.message


async def test_the_answer_goes_back_before_the_candidates_that_followed_it(core: HomeAssistant, camera_is) -> None:
    """A candidate that reaches the phone before the answer is thrown away by the browser.

    Home Assistant hands them over on a synchronous callback, in a burst; one task per message would
    let two sends interleave, so a session drains through exactly one.
    """
    porch = _camera(core)
    cam, sent = await camera_is(_FakeCamera(burst=[_Answer("v=0 answer"), _Candidate("cand-1"), _Candidate("cand-2")]))
    d = build_dispatcher(core, frozenset({"vision.view", "vision.live"}))

    assert await d.dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0 offer"}) == {}
    await _settle()

    assert cam.offers == [(SESSION, "v=0 offer")]
    assert [m["type"] for m in sent] == ["answer", "candidate", "candidate"]
    assert sent[0]["answer"] == "v=0 answer"
    assert [m["candidate"]["candidate"] for m in sent[1:]] == ["cand-1", "cand-2"]
    assert all(m["session_id"] == SESSION for m in sent)

    await live.close_all(core)


async def test_closing_lets_the_camera_go(core: HomeAssistant, camera_is) -> None:
    """go2rtc holds a consumer per session, so one never closed is one held forever."""
    porch = _camera(core)
    cam, _ = await camera_is(_FakeCamera())
    d = build_dispatcher(core, frozenset({"vision.view", "vision.live"}))
    await d.dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0"})

    await d.dispatch("vision.webrtc_close", {"session_id": SESSION})
    assert cam.closed == [SESSION]
    assert live._sessions(core) == {}
    # The hub closes a session whose viewer vanished, and may not know we already have.
    assert await d.dispatch("vision.webrtc_close", {"session_id": SESSION}) == {}


async def test_losing_the_hub_closes_every_session(core: HomeAssistant, camera_is) -> None:
    """Nobody is watching through a hub that is gone."""
    porch = _camera(core)
    cam, _ = await camera_is(_FakeCamera())
    d = build_dispatcher(core, frozenset({"vision.view", "vision.live"}))
    await d.dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0"})
    await d.dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": "sess-00000002", "offer_sdp": "v=0"})

    await live.close_all(core)
    assert sorted(cam.closed) == ["sess-00000001", "sess-00000002"]
    assert live._sessions(core) == {}


async def test_a_camera_that_cannot_do_this_says_so_and_leaves_nothing_behind(core: HomeAssistant, camera_is) -> None:
    porch = _camera(core)
    cam, _ = await camera_is(_FakeCamera(error=HomeAssistantError("Camera does not support WebRTC")))
    d = build_dispatcher(core, frozenset({"vision.view", "vision.live"}))

    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.webrtc_offer", {"entity_id": porch, "session_id": SESSION, "offer_sdp": "v=0"})
    assert err.value.code == "ha_error" and "WebRTC" in err.value.message
    assert live._sessions(core) == {} and cam.closed == [SESSION]


async def test_a_candidate_for_a_session_nobody_opened_is_refused(core: HomeAssistant, camera_is) -> None:
    """The session id names a live negotiation; one that is not ours reaches no camera."""
    _camera(core)
    cam, _ = await camera_is(_FakeCamera())
    d = build_dispatcher(core, frozenset({"vision.view", "vision.live"}))

    with pytest.raises(RpcError) as err:
        await d.dispatch("vision.webrtc_candidate", {"session_id": "sess-not-mine", "candidate": {"candidate": "cand"}})
    assert err.value.code == "not_found"
    assert cam.candidates == []


async def test_only_ways_of_finding_a_path_are_passed_on(core: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one place a URL from the home crosses the wire, and never one that reaches the camera."""

    class _Server:
        def __init__(self, urls, username=None, credential=None):  # noqa: ANN001
            self.urls, self.username, self.credential = urls, username, credential

    class _Config:
        class configuration:  # noqa: N801
            ice_servers = [
                _Server("stun:stun.home-assistant.io:3478"),
                _Server(["turns:turn.example:5349", "rtsp://camera.local/stream"], username="u", credential="c"),
                _Server(["http://camera.local/snapshot"]),
            ]

    class _Cam:
        def async_get_webrtc_client_configuration(self):  # noqa: ANN201
            return _Config()

    async def lookup(hass, entity_id):  # noqa: ANN001, ANN202
        return _Cam()

    monkeypatch.setattr(live, "_lookup_camera", lookup)
    porch = _camera(core)
    out = await build_dispatcher(core, frozenset({"vision.view", "vision.live"})).dispatch("vision.webrtc_config", {"entity_id": porch})

    assert out["ice_servers"] == [
        {"urls": ["stun:stun.home-assistant.io:3478"]},
        {"urls": ["turns:turn.example:5349"], "username": "u", "credential": "c"},
    ]
