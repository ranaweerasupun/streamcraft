"""
tests/test_webrtc.py — Tests for streamcraft.WebRTCSession.

WebRTCSession is the hardest module to test because a complete WebRTC
connection requires a real browser on the other end. However, a large
fraction of the session's behavior — construction, validation, state
management, and the critical ICE buffering logic — can be tested without
any actual WebRTC negotiation happening.

Test strategy
-------------
We test in three layers of increasing realism:

  1. Construction & validation — does it correctly identify a missing
     "webrtc" element? Does it start in IDLE state?

  2. State machine & lifecycle — does stop() transition correctly?
     Does the on_state_change callback fire?

  3. ICE candidate buffering — this is the subtle logic that prevents
     race conditions in real-world usage. We test it by calling
     handle_message() with ICE candidates before sending an offer,
     and verifying the candidates were buffered rather than dropped.

For the ICE buffering test we use a minimal pipeline with a real webrtcbin
element (if available), because the buffering logic inside WebRTCSession
depends on the _remote_set flag which is only modified by the actual
offer-handling code path.
"""

import asyncio
import pytest
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from streamcraft import PipelineBuilder
from streamcraft.webrtc import WebRTCSession
from helpers import skip_if_missing, element_available


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_minimal_webrtc_pipeline() -> Gst.Pipeline:
    """
    Build the simplest possible pipeline that contains a 'webrtc' element.
    We use videotestsrc + fakesink alongside a standalone webrtcbin,
    without actually linking them — we just need webrtcbin to be present
    in the pipeline so WebRTCSession can find it.
    """
    pipeline = Gst.Pipeline.new("test-webrtc-pipeline")
    webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
    if webrtc is None:
        pytest.skip("webrtcbin element not available on this system")
    pipeline.add(webrtc)
    return pipeline


def make_pipeline_without_webrtc() -> Gst.Pipeline:
    """A pipeline that deliberately does NOT contain a 'webrtc' element."""
    pipeline, _ = (
        PipelineBuilder()
        .element("videotestsrc")
        .element("fakesink")
        .build()
    )
    return pipeline


async def noop_send_json(data: dict) -> None:
    """A no-op send_json callable for tests that don't care about outbound messages."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# TestWebRTCSessionConstruction
# ─────────────────────────────────────────────────────────────────────────────

class TestWebRTCSessionConstruction:
    """
    Tests for WebRTCSession.__init__() — does it validate its inputs
    correctly and start in the right state?
    """

    @skip_if_missing("webrtcbin")
    def test_constructs_with_valid_pipeline(self):
        """
        A pipeline containing an element named 'webrtc' should allow
        construction without raising.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session is not None
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_raises_if_no_webrtc_element(self):
        """
        If the pipeline doesn't contain an element named 'webrtc',
        the constructor should raise ValueError with a clear message.
        """
        pipeline = make_pipeline_without_webrtc()
        try:
            with pytest.raises(ValueError):
                WebRTCSession(pipeline)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_error_message_explains_the_naming_requirement(self):
        """
        The ValueError message should explain that the webrtcbin element
        needs to be explicitly named 'webrtc', since that's the non-obvious
        part of the requirement.
        """
        pipeline = make_pipeline_without_webrtc()
        try:
            with pytest.raises(ValueError, match="webrtc"):
                WebRTCSession(pipeline)
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_initial_state_is_idle(self):
        """
        A freshly created session should be in IDLE state,
        waiting for a browser offer.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session.state == "IDLE"
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_optional_callbacks_default_to_none(self):
        """
        The on_state_change and on_incoming_stream callbacks should be
        optional — constructing without them should not raise.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session._on_state_change_cb is None
            assert session._on_incoming_stream_cb is None
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_state_change_callback_is_stored(self):
        """The on_state_change callback should be stored for later use."""
        pipeline = make_minimal_webrtc_pipeline()
        try:
            callback = lambda state: None
            session = WebRTCSession(pipeline, on_state_change=callback)
            assert session._on_state_change_cb is callback
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_incoming_stream_callback_is_stored(self):
        """The on_incoming_stream callback should be stored for later use."""
        pipeline = make_minimal_webrtc_pipeline()
        try:
            callback = lambda pad: None
            session = WebRTCSession(pipeline, on_incoming_stream=callback)
            assert session._on_incoming_stream_cb is callback
        finally:
            pipeline.set_state(Gst.State.NULL)


# ─────────────────────────────────────────────────────────────────────────────
# TestWebRTCSessionLifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestWebRTCSessionLifecycle:
    """
    Tests for the session's state transitions throughout its lifecycle.
    """

    @skip_if_missing("webrtcbin")
    def test_stop_transitions_to_stopped(self):
        """
        Calling stop() on an IDLE session should transition it to STOPPED.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session.state == "IDLE"
            session.stop()
            assert session.state == "STOPPED"
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_stop_is_idempotent(self):
        """
        Calling stop() twice should not raise an error. Once a session
        is stopped, subsequent stop() calls are no-ops.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            session.stop()
            session.stop()  # Should not raise
            assert session.state == "STOPPED"
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_on_state_change_callback_fires_on_stop(self):
        """
        When stop() is called, the on_state_change callback should be
        invoked with "STOPPED" as the argument.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            observed_states = []
            session = WebRTCSession(
                pipeline,
                on_state_change=lambda s: observed_states.append(s)
            )
            session.stop()
            assert "STOPPED" in observed_states
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_on_state_change_callback_receives_correct_sequence(self):
        """
        The state change callback should be called in the correct order.
        Stopping from IDLE should produce exactly ["STOPPED"].
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            observed = []
            session = WebRTCSession(
                pipeline,
                on_state_change=lambda s: observed.append(s)
            )
            session.stop()
            # From IDLE, stop() should go directly to STOPPED
            assert observed == ["STOPPED"]
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("webrtcbin")
    def test_stop_sets_pipeline_to_null(self):
        """
        After stop(), the underlying GStreamer pipeline should be in NULL state.
        This is important to release hardware resources.
        """
        pipeline = make_minimal_webrtc_pipeline()
        session = WebRTCSession(pipeline)
        session.stop()

        state_result = pipeline.get_state(timeout=Gst.SECOND)
        _, current_state, _ = state_result
        assert current_state == Gst.State.NULL


# ─────────────────────────────────────────────────────────────────────────────
# TestIceCandidateBuffering
# ─────────────────────────────────────────────────────────────────────────────

class TestIceCandidateBuffering:
    """
    Tests for the ICE candidate race condition fix.

    In real WebRTC usage, the browser sends its ICE candidates in the same
    burst of WebSocket messages as the offer. They often arrive at the server
    BEFORE the server has finished processing the offer (i.e. before
    set-remote-description is called). Without buffering, those candidates
    are lost, which causes intermittent connection failures.

    WebRTCSession buffers incoming ICE candidates until the remote
    description is set, then replays them. These tests verify that behavior.
    """

    @skip_if_missing("webrtcbin")
    def test_ice_candidates_before_offer_are_buffered(self):
        """
        ICE candidates received before an offer should be stored in
        _pending_ice, not immediately applied.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)

            candidate = {
                "candidate": "candidate:1 1 UDP 2122252543 192.168.1.100 12345 typ host",
                "sdpMLineIndex": 0
            }

            # Run the coroutine — no offer has been received yet
            asyncio.get_event_loop().run_until_complete(
                session.handle_message(
                    {"type": "candidate", "candidate": candidate},
                    noop_send_json
                )
            )

            # The candidate should be buffered, not dropped
            assert len(session._pending_ice) == 1
            assert session._pending_ice[0] == candidate

        finally:
            session.stop()

    @skip_if_missing("webrtcbin")
    def test_multiple_ice_candidates_before_offer_all_buffered(self):
        """
        Multiple ICE candidates arriving before the offer should all be
        buffered — not just the first one.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            loop = asyncio.get_event_loop()

            candidates = [
                {"candidate": f"candidate:{i} 1 UDP 2122252543 192.168.1.{i} 1234{i} typ host",
                 "sdpMLineIndex": 0}
                for i in range(3)
            ]

            for c in candidates:
                loop.run_until_complete(
                    session.handle_message(
                        {"type": "candidate", "candidate": c},
                        noop_send_json
                    )
                )

            assert len(session._pending_ice) == 3

        finally:
            session.stop()

    @skip_if_missing("webrtcbin")
    def test_remote_set_flag_starts_false(self):
        """
        The _remote_set flag should be False at the start of a session,
        which is what causes pre-offer ICE candidates to be buffered.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session._remote_set is False
        finally:
            session.stop()

    @skip_if_missing("webrtcbin")
    def test_pending_ice_starts_empty(self):
        """The ICE buffer should be empty at session creation."""
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            assert session._pending_ice == []
        finally:
            session.stop()

    @skip_if_missing("webrtcbin")
    def test_unknown_message_type_is_ignored(self):
        """
        A message with an unrecognized type should be silently ignored,
        not raise an exception. The browser might send other message types
        that we don't need to handle.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            # Should not raise
            asyncio.get_event_loop().run_until_complete(
                session.handle_message(
                    {"type": "ping", "data": "hello"},
                    noop_send_json
                )
            )
        finally:
            session.stop()

    @skip_if_missing("webrtcbin")
    def test_malformed_candidate_message_is_ignored(self):
        """
        A 'candidate' message with no 'candidate' key should be ignored
        rather than raising a KeyError or AttributeError.
        """
        pipeline = make_minimal_webrtc_pipeline()
        try:
            session = WebRTCSession(pipeline)
            asyncio.get_event_loop().run_until_complete(
                session.handle_message(
                    {"type": "candidate"},  # missing the 'candidate' field
                    noop_send_json
                )
            )
            # Nothing should be buffered
            assert len(session._pending_ice) == 0
        finally:
            session.stop()
