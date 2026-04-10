#!/usr/bin/env python3
"""
examples/streaming_server.py — Bidirectional video and audio streaming server.

Run this on a Raspberry Pi 5 (or any Linux machine with a camera).
On any other device in the same Tailscale network, open a browser and
navigate to https://<your-device-name>.<tailnet>.ts.net:8443 to connect.

What this does
──────────────
  • Streams live video (H.264) and audio (Opus) from the Pi's camera
    to the browser over WebRTC — the browser sees the Pi's camera.
  • Receives the browser's camera and microphone and plays them back
    on the Pi — the Pi sees and hears whoever is at the browser.
  • Exposes a WebSocket endpoint for PTZ camera control — if the Pi's
    camera supports pan, tilt, and zoom, the browser UI controls them.

What streamcraft provides
────────────────────────
  PipelineBuilder  — builds the GStreamer pipeline in 12 readable lines
                     instead of 130 lines of ElementFactory boilerplate.
  WebRTCSession    — manages the entire SDP offer/answer exchange, ICE
                     candidate buffering, and GLib→asyncio thread bridging.
  V4L2PTZCamera    — auto-detects camera PTZ ranges from v4l2-ctl and
                     exposes a clean set_ptz() API.
  require_elements — checks all needed GStreamer plugins are installed
                     at startup with a clear apt install hint if not.
  check_v4l2_device — verifies the camera device actually opens before
                     committing to starting the server.

What this file contains (your application logic)
─────────────────────────────────────────────────
  build_pipeline()       — describes the pipeline topology
  on_incoming_stream()   — routes incoming browser media to local output
  handle_ws()            — WebRTC signaling over WebSocket
  handle_camera_ws()     — PTZ camera control over WebSocket
  handle_index()         — serves the browser interface HTML file
  main()                 — startup checks and server configuration

Dependencies
────────────
  pip install aiohttp streamcraft
  sudo apt install gstreamer1.0-plugins-{base,good,bad,ugly} \
                   gstreamer1.0-libav gstreamer1.0-alsa v4l-utils

Setup (Tailscale HTTPS)
───────────────────────
  tailscale cert <your-device-fqdn>   # generates the TLS certificate
  python3 streaming_server.py         # run the server
"""

import asyncio
import json
import ssl
import sys
import traceback
from pathlib import Path

from aiohttp import web

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from streamcraft import (
    PipelineBuilder,
    WebRTCSession,
    V4L2PTZCamera,
    require_elements,
    check_v4l2_device,
)

Gst.init(None)

# BASE_DIR is the directory that contains this script (examples/).
# All file paths that need to be resolved at runtime are anchored here,
# so the server works correctly regardless of which directory you launch
# it from — `python examples/streaming_server.py` and `cd examples &&
# python streaming_server.py` both resolve interface.html correctly.
BASE_DIR = Path(__file__).parent


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# Change these four lines to match your device and network.
# Everything else is derived from them automatically.
# ─────────────────────────────────────────────────────────────────────────────

TAILSCALE_HOSTNAME = "device"          # your device name in Tailscale
TAILSCALE_DOMAIN   = "tail123ff1.ts.net:8443"  # your tailnet domain
VIDEO_DEV          = "/dev/video0"          # camera device
AUDIO_DEV          = "default"              # ALSA input device (microphone)
OUTPUT_AUDIO_DEV   = "default"              # ALSA output device (speaker)

# Derived from config — no need to change these
TAILSCALE_FQDN = f"{TAILSCALE_HOSTNAME}.{TAILSCALE_DOMAIN}"
CERT_FILE      = f"/var/lib/tailscale/certs/{TAILSCALE_FQDN}.crt"
KEY_FILE       = f"/var/lib/tailscale/certs/{TAILSCALE_FQDN}.key"
HTTP_PORT      = 8080   # plain HTTP — only used to redirect to HTTPS
TLS_PORT       = 8443   # HTTPS / WSS

# Video capture settings — adjust to match your camera's capabilities.
# Run `v4l2-ctl --list-formats-ext` to see what yours supports.
WIDTH, HEIGHT, FPS = 1280, 720, 30


# ─────────────────────────────────────────────────────────────────────────────
# PTZ camera
#
# V4L2PTZCamera queries v4l2-ctl at startup to discover the camera's valid
# pan/tilt/zoom ranges automatically — no hardcoded values per camera model.
# If the connected camera has no PTZ controls, all operations are silent
# no-ops that return False, so the server starts regardless.
# ─────────────────────────────────────────────────────────────────────────────

camera = V4L2PTZCamera(VIDEO_DEV)


def log(tag: str, *msg) -> None:
    print(f"[{tag}]", *msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# GStreamer pipeline
#
# The pipeline has two parallel chains that both feed into one webrtcbin:
#
#   camera → MJPEG caps → queue → jpegdec → videoconvert
#          → x264enc → h264parse → H.264 caps → queue → rtph264pay ──┐
#                                                                      ├→ webrtcbin
#   alsasrc → queue → audioresample → audioconvert → 48kHz caps        │
#           → opusenc → rtpopuspay ─────────────────────────────────────┘
#
# The video chain is built with PipelineBuilder (12 lines instead of ~130).
# The audio chain extends the same pipeline using the raw GStreamer API,
# which is the correct pattern when two parallel chains share one pipeline.
# webrtcbin is added last because its sink pads must be explicitly requested —
# a dynamic operation that sits outside the builder's linear model.
# ─────────────────────────────────────────────────────────────────────────────

def build_pipeline() -> Gst.Pipeline:
    """
    Builds a fresh pipeline for each browser session.
    Called once per connection inside handle_ws(); torn down when the
    browser disconnects via session.stop() → pipeline.set_state(NULL).
    """

    # ── Video chain via PipelineBuilder ──────────────────────────────────────
    pipeline, elems = (
        PipelineBuilder(name="streaming-pipeline")
        .element("v4l2src",    name="src",      device=VIDEO_DEV, do_timestamp=True)
        .caps(f"image/jpeg,width={WIDTH},height={HEIGHT},framerate={FPS}/1")
        .element("queue",      name="queue_v1", max_size_buffers=3, leaky=2)
        .element("jpegdec")
        .element("videoconvert")
        .element("x264enc",    name="encoder",  tune="zerolatency",
                               speed_preset="ultrafast", bitrate=2000, key_int_max=30)
        .element("h264parse",                   config_interval=1)
        .caps("video/x-h264,stream-format=byte-stream,alignment=au")
        .element("queue",      name="queue_v2", max_size_buffers=5, leaky=2)
        .element("rtph264pay", name="payv",     pt=96, config_interval=1, mtu=1200)
        .build()
    )

    # ── Audio chain — raw GStreamer API on the same pipeline ──────────────────
    # A small inline helper keeps the repetition manageable without hiding
    # what's happening from a reader who doesn't know streamcraft.
    def make(factory, name=None, **props):
        elem = Gst.ElementFactory.make(factory, name)
        if elem is None:
            raise RuntimeError(f"Could not create GStreamer element: {factory}")
        for k, v in props.items():
            elem.set_property(k.replace("_", "-"), v)
        pipeline.add(elem)
        return elem

    alsasrc    = make("alsasrc",      "alsasrc",
                      device=AUDIO_DEV, do_timestamp=True,
                      buffer_time=20000, latency_time=10000)
    queue_a1   = make("queue",        "queue_a1",  max_size_buffers=4, leaky=2)
    audioresamp = make("audioresample", "audioresample")
    audioconv  = make("audioconvert",  "audioconvert")
    opusenc    = make("opusenc",       "opusenc",
                      bitrate=128000, complexity=0,
                      inband_fec=True, frame_size=10, max_payload_size=1200)
    paya       = make("rtpopuspay",   "paya", pt=111, mtu=1200)

    audio_caps = Gst.Caps.from_string("audio/x-raw,channels=1,rate=48000")
    if not alsasrc.link(queue_a1):                          raise RuntimeError("alsasrc → queue_a1")
    if not queue_a1.link(audioresamp):                      raise RuntimeError("queue_a1 → audioresample")
    if not audioresamp.link(audioconv):                     raise RuntimeError("audioresample → audioconvert")
    if not audioconv.link_filtered(opusenc, audio_caps):    raise RuntimeError("audioconvert → opusenc")
    if not opusenc.link(paya):                              raise RuntimeError("opusenc → paya")

    # ── webrtcbin — added manually, sink pads requested explicitly ───────────
    webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
    webrtc.set_property("bundle-policy", 3)   # max-bundle: video + audio share one UDP port
    webrtc.set_property("latency", 0)
    pipeline.add(webrtc)

    elems["payv"].get_static_pad("src").link(webrtc.get_request_pad("sink_0"))
    paya.get_static_pad("src").link(webrtc.get_request_pad("sink_1"))

    # Attach bus logging so GStreamer errors appear in the terminal
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _on_bus_message)

    log("PIPELINE", "Built successfully")
    return pipeline


def _on_bus_message(_bus, msg) -> None:
    """Logs GStreamer errors and warnings to the terminal."""
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        log("GST", "ERROR:", err, "|", dbg or "")
    elif msg.type == Gst.MessageType.WARNING:
        w, dbg = msg.parse_warning()
        log("GST", "WARNING:", w, "|", dbg or "")


# ─────────────────────────────────────────────────────────────────────────────
# Incoming stream from the browser
#
# When the browser sends its own camera and microphone to the Pi, webrtcbin
# emits "pad-added" for each arriving track. WebRTCSession captures this
# and calls on_incoming_stream() — your code decides what to do with the
# stream. Here we decode it and play it through the Pi's output devices
# so whoever is physically at the Pi can see and hear the browser user.
# ─────────────────────────────────────────────────────────────────────────────

def on_incoming_stream(pad: Gst.Pad) -> None:
    """Routes an incoming browser media track to the Pi's local output."""
    pipeline = pad.get_parent_element().get_parent()
    caps      = pad.get_current_caps()
    media     = caps.get_structure(0).get_name() if caps else ""
    log("INCOMING", f"Browser stream arriving: {media}")

    # decodebin handles any incoming codec — H264, VP8, VP9, AV1 —
    # without us needing to know in advance which one the browser chose.
    decode = Gst.ElementFactory.make("decodebin", None)
    if not decode:
        log("GST", "Failed to create decodebin"); return

    pipeline.add(decode)
    decode.sync_state_with_parent()
    decode.connect("pad-added", _on_decoded_pad, pipeline)
    pad.link(decode.get_static_pad("sink"))


def _on_decoded_pad(_, pad: Gst.Pad, pipeline: Gst.Pipeline) -> None:
    """Connects a decoded pad to the appropriate local output sink."""
    caps  = pad.get_current_caps()
    media = caps.to_string() if caps else ""

    if media.startswith("video/"):
        # Display the browser's video on the Pi's connected screen.
        # Remove autovideosink and replace with fakesink if the Pi is headless.
        elems = [
            Gst.ElementFactory.make("queue",        None),
            Gst.ElementFactory.make("videoconvert", None),
            Gst.ElementFactory.make("autovideosink", None),
        ]
        elems[-1].set_property("sync", False)
        for e in elems: pipeline.add(e); e.sync_state_with_parent()
        pad.link(elems[0].get_static_pad("sink"))
        elems[0].link(elems[1]); elems[1].link(elems[2])
        log("INCOMING", "Browser video → display")

    elif media.startswith("audio/"):
        # Play the browser's audio through the Pi's speaker.
        queue   = Gst.ElementFactory.make("queue",         None)
        convert = Gst.ElementFactory.make("audioconvert",  None)
        resamp  = Gst.ElementFactory.make("audioresample", None)
        sink    = Gst.ElementFactory.make("alsasink",      None)
        sink.set_property("device", OUTPUT_AUDIO_DEV)
        sink.set_property("sync", False)
        elems = [queue, convert, resamp, sink]
        for e in elems: pipeline.add(e); e.sync_state_with_parent()
        pad.link(queue.get_static_pad("sink"))
        queue.link(convert); convert.link(resamp); resamp.link(sink)
        log("INCOMING", f"Browser audio → {OUTPUT_AUDIO_DEV}")


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket handlers
# ─────────────────────────────────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """
    WebRTC signaling endpoint — /ws

    WebRTCSession handles the entire SDP offer/answer exchange,
    ICE candidate buffering, and the GLib→asyncio thread bridge.
    This handler's only job is to pass messages back and forth.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log("SIGNAL", "Browser connected")

    pipeline = build_pipeline()
    session  = WebRTCSession(
        pipeline,
        on_state_change      = lambda s: log("WebRTC", s),
        on_incoming_stream   = on_incoming_stream,
    )
    session.connect_ice_sender(ws.send_json)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await session.handle_message(json.loads(msg.data), ws.send_json)
    except Exception as e:
        log("SIGNAL", f"Session error: {e}"); traceback.print_exc()
    finally:
        # Always release hardware when the browser disconnects.
        # Without this, the camera stays open and the next connection fails.
        session.stop()
        log("SIGNAL", "Browser disconnected — ready for next session")

    return ws


async def handle_camera_ws(request: web.Request) -> web.WebSocketResponse:
    """
    PTZ camera control endpoint — /camera

    On connect: sends the current camera position so the browser UI
    can initialize its sliders to the correct values.

    On each message: applies the requested pan/tilt/zoom values and
    sends the resulting actual position back (the camera may have clamped
    the requested value to its hardware limits).
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        # Push current position immediately so the browser's sliders are correct
        await ws.send_json({"type": "status", "data": camera.status.to_dict()})

        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
                camera.set_ptz(
                    pan  = data.get("pan",  camera.pan),
                    tilt = data.get("tilt", camera.tilt),
                    zoom = data.get("zoom", camera.zoom),
                )
                await ws.send_json({"type": "result", "data": camera.status.to_dict()})
            except Exception:
                pass  # Camera errors don't interrupt the video session

    except Exception:
        pass

    return ws


async def handle_index(request: web.Request) -> web.Response:
    """Serves the browser interface."""
    return web.FileResponse(BASE_DIR / "interface.html")


# ─────────────────────────────────────────────────────────────────────────────
# Server startup
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    log("STARTUP", "Starting streaming server...")

    # Fail fast with a clear message if GStreamer plugins are missing.
    # require_elements() prints the exact apt install command needed.
    require_elements(
        "webrtcbin", "v4l2src", "jpegdec", "videoconvert",
        "x264enc", "h264parse", "rtph264pay",
        "alsasrc", "audioresample", "audioconvert",
        "opusenc", "rtpopuspay", "alsasink", "decodebin",
    )

    # Verify the camera actually opens — catches "device busy" and permission
    # errors before we try to start the pipeline for a real connection.
    ok, msg = check_v4l2_device(VIDEO_DEV)
    if not ok:
        log("ERROR", f"Camera not available: {msg}")
        sys.exit(1)
    log("CAMERA", msg)

    # Tailscale certificates — run `tailscale cert <fqdn>` if these don't exist.
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)

    # Three routes: the page, the WebRTC signaling, the camera control.
    app = web.Application()
    app.router.add_get("/",       handle_index)
    app.router.add_get("/ws",     handle_ws)
    app.router.add_get("/camera", handle_camera_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", TLS_PORT, ssl_context=ssl_ctx).start()

    # HTTP on port 8080 only exists to redirect to HTTPS — nothing else.
    async def redirect(req):
        return web.HTTPFound(f"https://{req.host.split(':')[0]}:{TLS_PORT}{req.rel_url}")
    redir = web.Application()
    redir.router.add_route("*", "/{tail:.*}", redirect)
    redir_runner = web.AppRunner(redir)
    await redir_runner.setup()
    await web.TCPSite(redir_runner, "0.0.0.0", HTTP_PORT).start()

    log("READY", "=" * 50)
    log("READY", f"  Open in browser: https://{TAILSCALE_FQDN}:{TLS_PORT}")
    log("READY", f"  Camera PTZ: {'available' if camera.available else 'not detected'}")
    log("READY", "=" * 50)

    await asyncio.Future()  # run until Ctrl+C


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("SHUTDOWN", "Server stopped")
    except Exception as e:
        log("ERROR", e); traceback.print_exc(); sys.exit(1)