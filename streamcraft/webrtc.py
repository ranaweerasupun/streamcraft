"""
streamcraft.webrtc — WebRTC session management for GStreamer pipelines.

The problem this solves
-----------------------
Wiring a GStreamer pipeline to a WebRTC browser session involves a
well-known sequence of steps that every WebRTC server author has to
implement, get subtly wrong, and then debug:

  1. Browser sends an SDP offer over a WebSocket.
  2. Server sets that as the remote description on webrtcbin.
  3. Server calls create-answer, gets the answer via a GStreamer Promise.
  4. Server sets the answer as the local description.
  5. Server sends the answer SDP back to the browser.
  6. ICE candidates flow in both directions, but they may arrive BEFORE
     the remote description is set — those must be buffered and replayed.
  7. When a pipeline state change needs to happen (e.g. going to PLAYING
     after negotiation), it must be scheduled on the asyncio event loop,
     not from a GStreamer callback thread.

Steps 6 and 7 are where almost every tutorial example is wrong or fragile.
The ICE buffering issue in particular causes intermittent connection failures
that are very hard to debug.

WebRTCSession encapsulates all of this. You give it a GStreamer pipeline
(already built, with a "webrtcbin" element named "webrtc"), and a callable
that sends JSON to the browser. It manages the entire signaling dance.

Framework independence
----------------------
The session is deliberately decoupled from aiohttp, FastAPI, or any other
web framework. The send_json callable can be any async function that accepts
a dict — a WebSocket send method, a queue.put(), whatever you need.

This means WebRTCSession works with aiohttp, FastAPI, Starlette, raw
websockets, or any async framework, without modification.

Example (aiohttp):
    async def handle_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        pipeline, elems = build_my_pipeline()
        session = WebRTCSession(pipeline)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                await session.handle_message(data, send_json=ws.send_json)

        session.stop()

Example (FastAPI / websockets):
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        pipeline, elems = build_my_pipeline()
        session = WebRTCSession(pipeline)

        async def send(data: dict):
            await websocket.send_text(json.dumps(data))

        try:
            while True:
                data = json.loads(await websocket.receive_text())
                await session.handle_message(data, send_json=send)
        except WebSocketDisconnect:
            session.stop()
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Awaitable, Callable, Dict, List, Optional

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp

Gst.init(None)

# Type alias for the send_json callable that WebRTCSession expects.
# It's any async function that accepts a dict and sends it as JSON to the browser.
SendJsonFn = Callable[[Dict[str, Any]], Awaitable[None]]

# ─────────────────────────────────────────────────────────────────────────────
# The main class
# ─────────────────────────────────────────────────────────────────────────────

class WebRTCSession:
    """
    Manages the WebRTC signaling lifecycle for a GStreamer pipeline.

    One WebRTCSession corresponds to one browser connection. When the browser
    disconnects, call stop() and discard the session. For the next connection,
    create a new pipeline and a new WebRTCSession.

    The pipeline must contain a webrtcbin element named "webrtc". Build it
    with PipelineBuilder (or manually), connect your RTP payloaders to
    webrtcbin's sink pads, then pass the pipeline here.

    State model
    -----------
    The session moves through these states:

        IDLE → NEGOTIATING → CONNECTED → STOPPED

    IDLE:         Created, waiting for an offer from the browser.
    NEGOTIATING:  Offer received, answer sent, waiting for ICE to complete.
    CONNECTED:    ICE connected, pipeline is PLAYING, media is flowing.
    STOPPED:      stop() was called or an unrecoverable error occurred.

    Incoming pad handling
    ---------------------
    If the browser is also sending media (video/audio from the operator's
    camera), webrtcbin will emit "pad-added" when it's ready to deliver
    decoded data. You can handle this by connecting to the on_incoming_stream
    callback — or by subclassing and overriding _on_pad_added().
    """

    def __init__(
        self,
        pipeline: Gst.Pipeline,
        *,
        on_state_change: Optional[Callable[[str], None]] = None,
        on_incoming_stream: Optional[Callable[[Gst.Pad], None]] = None,
    ):
        """
        Args:
            pipeline:
                A GStreamer pipeline containing a webrtcbin element named "webrtc".
                The pipeline should already have RTP payloaders linked to webrtcbin's
                sink pads, and should NOT yet be in PLAYING state — the session
                will manage state transitions during negotiation.

            on_state_change:
                Optional callback called whenever the session state changes.
                Receives the new state name as a string: "IDLE", "NEGOTIATING",
                "CONNECTED", or "STOPPED". Useful for logging or UI updates.

            on_incoming_stream:
                Optional callback called when the browser sends media to the robot
                (i.e. when webrtcbin adds a new SRC pad). Receives the Gst.Pad
                that was added. You're responsible for linking it to a decoder
                and sink. If not provided, incoming streams are ignored.
        """
        self.pipeline = pipeline
        self._on_state_change_cb = on_state_change
        self._on_incoming_stream_cb = on_incoming_stream

        # Get the webrtcbin element — this is the heart of the session
        self._webrtc: Gst.Element = pipeline.get_by_name("webrtc")
        if self._webrtc is None:
            raise ValueError(
                "Pipeline does not contain a 'webrtc' element. "
                "Make sure your webrtcbin element is named 'webrtc' when building "
                "the pipeline: .element('webrtcbin', name='webrtc', ...)"
            )

        # ICE candidates that arrived before the remote description was set.
        # These are buffered here and replayed once set-remote-description completes.
        # This is the subtle part: in real-world conditions, ICE candidates from the
        # browser often arrive over the WebSocket BEFORE the offer processing is done,
        # because they're sent in parallel. Without this buffer, those candidates are
        # silently dropped and the connection may fail to establish.
        self._pending_ice: List[Dict[str, Any]] = []
        self._remote_set: bool = False

        # Capture the running event loop, or create one if we're being
        # constructed outside of an async context (common in tests).
        try:
            self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._state: str = "IDLE"

        # Attach the signal handlers that GStreamer will call
        self._webrtc.connect("on-ice-candidate", self._on_local_ice_candidate)
        self._webrtc.connect("pad-added", self._on_pad_added)

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current session state: IDLE, NEGOTIATING, CONNECTED, or STOPPED."""
        return self._state

    async def handle_message(
        self,
        message: Dict[str, Any],
        send_json: SendJsonFn,
    ) -> None:
        """
        Process one WebRTC signaling message from the browser.

        This is the main entry point. Call it from your WebSocket message
        handler for every message the browser sends. The message format
        follows the standard WebRTC signaling convention:

            Offer:     {"type": "offer", "sdp": "<SDP string>"}
            Candidate: {"type": "candidate", "candidate": {"candidate": "...", "sdpMLineIndex": N}}

        Args:
            message:   The parsed JSON dict from the browser.
            send_json: An async callable that sends a dict as JSON to the browser.
                       Typically ws.send_json (aiohttp) or a lambda wrapping
                       websocket.send_text(json.dumps(...)).

        The session will call send_json with:
            Answer:    {"type": "answer", "sdp": "<SDP string>"}
            Candidate: {"type": "candidate", "candidate": {...}}
        """
        msg_type = message.get("type")

        if msg_type == "offer" and message.get("sdp"):
            await self._handle_offer(message["sdp"], send_json)

        elif msg_type == "candidate" and message.get("candidate"):
            await self._handle_ice_candidate(message["candidate"])

    def stop(self) -> None:
        """
        Stop the session and the underlying pipeline.

        Call this when the browser disconnects. After calling stop(),
        the session and pipeline should be discarded — create new ones
        for the next connection.
        """
        if self._state == "STOPPED":
            return
        self._set_state("STOPPED")
        try:
            self.pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

    # ── Signaling handlers ────────────────────────────────────────────────────

    async def _handle_offer(self, sdp_text: str, send_json: SendJsonFn) -> None:
        """
        Process a WebRTC offer from the browser.

        The sequence here is:
          1. Parse the SDP text into a GstWebRTC offer structure
          2. Pause the pipeline (it must be at least PAUSED to negotiate)
          3. Set the offer as the remote description on webrtcbin
          4. Replay any ICE candidates that arrived before the offer
          5. Create an answer via a GStreamer Promise
          6. Set the answer as the local description
          7. Send the answer SDP back to the browser
          8. After a short delay, move the pipeline to PLAYING
        """
        self._set_state("NEGOTIATING")

        # Step 1: Parse the SDP text
        result, sdp = GstSdp.SDPMessage.new_from_text(sdp_text)
        if result != GstSdp.SDPResult.OK:
            raise RuntimeError(
                f"Failed to parse SDP offer (result={result}). "
                f"The browser may have sent a malformed SDP."
            )
        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp
        )

        # Step 2: Bring the pipeline to PAUSED so webrtcbin can start working.
        # We must be at least PAUSED before setting descriptions — PLAYING would
        # work too, but PAUSED is safer here since we haven't negotiated yet.
        ret = self.pipeline.set_state(Gst.State.PAUSED)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set pipeline to PAUSED before negotiation.")

        # Step 3: Set remote description
        # We try the two-argument form first (newer GStreamer), fall back to
        # the three-argument form (older versions that require a Promise).
        try:
            self._webrtc.emit("set-remote-description", offer)
        except TypeError:
            self._webrtc.emit("set-remote-description", offer, None)
        self._remote_set = True

        # Step 4: Replay any ICE candidates that arrived before the offer.
        # In real network conditions, the browser often sends its ICE candidates
        # immediately after (or even during) the offer, so they arrive at the
        # WebSocket handler before our offer processing above has completed.
        # Without this replay, those candidates are lost.
        for candidate in self._pending_ice:
            self._add_ice_candidate(candidate)
        self._pending_ice.clear()

        # Step 5 & 6 & 7: Create answer and send it.
        # create-answer is asynchronous — it uses a GStreamer Promise that fires
        # a callback on the GLib thread. We bridge that back to asyncio using
        # run_coroutine_threadsafe so the answer is sent correctly.
        answer_future: asyncio.Future = self._loop.create_future()

        def on_answer_created(promise: Gst.Promise, _user_data: Any) -> None:
            """Called by GStreamer on the GLib thread when the answer is ready."""
            try:
                if promise.wait() != Gst.PromiseResult.REPLIED:
                    asyncio.run_coroutine_threadsafe(
                        _reject_future(answer_future, "create-answer: no reply"),
                        self._loop
                    )
                    return

                reply = promise.get_reply()
                answer = reply.get_value("answer") if reply else None
                if not answer or not getattr(answer, "sdp", None):
                    asyncio.run_coroutine_threadsafe(
                        _reject_future(answer_future, f"create-answer: invalid answer -> {answer}"),
                        self._loop
                    )
                    return

                # Set local description (also from the GLib thread — this is fine)
                try:
                    self._webrtc.emit("set-local-description", answer)
                except TypeError:
                    self._webrtc.emit("set-local-description", answer, None)

                # Schedule the answer SDP send back on the asyncio loop
                asyncio.run_coroutine_threadsafe(
                    _resolve_future(answer_future, answer.sdp.as_text()),
                    self._loop
                )
            except Exception as exc:
                asyncio.run_coroutine_threadsafe(
                    _reject_future(answer_future, str(exc)),
                    self._loop
                )

        promise = Gst.Promise.new_with_change_func(on_answer_created, None)
        self._webrtc.emit("create-answer", None, promise)

        # Await the answer (this is the asyncio side of the bridge above)
        try:
            answer_sdp = await asyncio.wait_for(answer_future, timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError("WebRTC answer creation timed out after 10 seconds.")

        # Step 7: Send the answer back to the browser
        await send_json({"type": "answer", "sdp": answer_sdp})

        # Step 8: Transition to PLAYING shortly after sending the answer.
        # We give a brief delay to let the browser process the answer before
        # the pipeline starts pushing data. Without this, some browsers drop
        # the first few frames while they're still processing the SDP.
        asyncio.ensure_future(self._start_pipeline_delayed(0.2))

    async def _handle_ice_candidate(self, candidate: Dict[str, Any]) -> None:
        """
        Process an ICE candidate from the browser.

        If the remote description isn't set yet, buffer the candidate.
        Otherwise, add it to webrtcbin immediately.
        """
        if not self._remote_set:
            # Buffer it — will be replayed in _handle_offer after remote is set
            self._pending_ice.append(candidate)
        else:
            self._add_ice_candidate(candidate)

    def _add_ice_candidate(self, candidate: Dict[str, Any]) -> None:
        """Add a single ICE candidate to webrtcbin."""
        try:
            mline_index = int(candidate.get("sdpMLineIndex", 0))
            candidate_str = candidate.get("candidate", "")
            self._webrtc.emit("add-ice-candidate", mline_index, candidate_str)
        except Exception as exc:
            # ICE failures are usually non-fatal — log and continue
            _log("ICE", f"Failed to add candidate: {exc}")

    # ── GStreamer signal callbacks ─────────────────────────────────────────────

    def _on_local_ice_candidate(
        self,
        _webrtcbin: Gst.Element,
        mline_index: int,
        candidate: str,
    ) -> None:
        """
        Called by GStreamer (on the GLib thread) when webrtcbin has gathered
        a local ICE candidate to send to the browser.

        We must NOT send anything from this callback directly, because it runs
        on GLib's thread, not asyncio's. Instead, we'd need a reference to
        send_json here. Since we don't have it at construction time, the
        current approach is to expose this as a signal that the caller can
        connect to.

        For the aiohttp/websockets use case, the typical pattern is:

            def on_ice(send_json):
                def handler(_bin, mline, cand):
                    asyncio.run_coroutine_threadsafe(
                        send_json({"type": "candidate",
                                   "candidate": {"candidate": cand,
                                                 "sdpMLineIndex": mline}}),
                        loop
                    )
                return handler

            session._webrtc.connect("on-ice-candidate", on_ice(ws.send_json))

        We provide a helper method connect_ice_sender() below for this pattern.
        """
        pass  # Overridden by connect_ice_sender()

    def connect_ice_sender(
        self,
        send_json: SendJsonFn,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """
        Connect a local ICE candidate sender to the webrtcbin element.

        Call this after creating the session to enable outbound ICE candidates
        (robot → browser). Without this, the browser will not receive the
        robot's ICE candidates and the connection will likely fail.

        Args:
            send_json: The same async callable you pass to handle_message().
            loop:      The asyncio event loop. Defaults to the loop captured
                       at session creation time.

        Example:
            session = WebRTCSession(pipeline)
            session.connect_ice_sender(ws.send_json)

            async for msg in ws:
                data = json.loads(msg.data)
                await session.handle_message(data, send_json=ws.send_json)
        """
        _loop = loop or self._loop

        def _ice_handler(
            _webrtcbin: Gst.Element,
            mline_index: int,
            candidate: str,
        ) -> None:
            """Runs on the GLib thread — bridge to asyncio."""
            if not candidate:
                return
            payload = {
                "type": "candidate",
                "candidate": {
                    "candidate": candidate,
                    "sdpMLineIndex": int(mline_index),
                },
            }
            asyncio.run_coroutine_threadsafe(send_json(payload), _loop)

        # Disconnect the placeholder and connect the real handler
        self._webrtc.connect("on-ice-candidate", _ice_handler)

    def _on_pad_added(self, _webrtcbin: Gst.Element, pad: Gst.Pad) -> None:
        """
        Called by GStreamer when webrtcbin exposes a new SRC pad.

        This happens when the browser is sending media to us (e.g. the
        operator's camera and microphone). We delegate to the user's callback
        if one was provided, otherwise we do nothing (ignoring incoming streams).
        """
        if pad.get_direction() != Gst.PadDirection.SRC:
            return  # Only care about source pads (incoming media)
        if self._on_incoming_stream_cb is not None:
            try:
                self._on_incoming_stream_cb(pad)
            except Exception as exc:
                _log("WebRTC", f"Error in on_incoming_stream callback: {exc}")
                traceback.print_exc()

    # ── State management ──────────────────────────────────────────────────────

    def _set_state(self, new_state: str) -> None:
        self._state = new_state
        if self._on_state_change_cb is not None:
            try:
                self._on_state_change_cb(new_state)
            except Exception:
                pass

    async def _start_pipeline_delayed(self, delay_seconds: float = 0.2) -> None:
        """
        Set the pipeline to PLAYING after a short delay.

        The delay gives the browser time to process the SDP answer before
        data starts flowing. Without it, the first frames are sometimes
        dropped by the browser's RTP jitter buffer.
        """
        await asyncio.sleep(delay_seconds)
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            _log("WebRTC", "Failed to set pipeline to PLAYING")
            self._set_state("STOPPED")
        else:
            self._set_state("CONNECTED")


# ─────────────────────────────────────────────────────────────────────────────
# Tiny helpers — these are module-private
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_future(future: asyncio.Future, value: Any) -> None:
    """Safely resolve an asyncio Future from any thread."""
    if not future.done():
        future.set_result(value)


async def _reject_future(future: asyncio.Future, reason: str) -> None:
    """Safely reject an asyncio Future from any thread."""
    if not future.done():
        future.set_exception(RuntimeError(reason))


def _log(tag: str, *msg: Any) -> None:
    print(f"[{tag}]", *msg, flush=True)
