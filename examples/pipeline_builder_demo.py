"""
examples/pipeline_builder_demo.py

Demonstrates how PipelineBuilder simplifies real-world GStreamer pipeline
construction. Each example shows the *before* (raw GStreamer API) alongside
the *after* (PipelineBuilder) so the value is immediately clear.

Run this on a Linux machine with GStreamer installed:
    python examples/pipeline_builder_demo.py
"""

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

# Our library
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from streamcraft import PipelineBuilder

Gst.init(None)


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 1: Video capture → H.264 encode → RTP payload
#
# This is the core video chain from a robot teleoperation server.
# Before: ~40 lines of boilerplate. After: 10 lines that read like a diagram.
# ─────────────────────────────────────────────────────────────────────────────

def build_video_chain_BEFORE() -> Gst.Pipeline:
    """The original way — explicit, verbose, but correct."""

    pipeline = Gst.Pipeline.new("video-pipeline")

    v4l2src = Gst.ElementFactory.make("v4l2src", "v4l2src")
    v4l2src.set_property("device", "/dev/video0")
    v4l2src.set_property("do-timestamp", True)

    mjpeg_caps = Gst.Caps.from_string("image/jpeg,width=1280,height=720,framerate=30/1")

    queue_v1 = Gst.ElementFactory.make("queue", "queue_v1")
    queue_v1.set_property("max-size-buffers", 3)
    queue_v1.set_property("max-size-time", 0)
    queue_v1.set_property("max-size-bytes", 0)
    queue_v1.set_property("leaky", 2)

    jpegdec = Gst.ElementFactory.make("jpegdec", "jpegdec")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")

    x264enc = Gst.ElementFactory.make("x264enc", "x264enc")
    x264enc.set_property("tune", "zerolatency")
    x264enc.set_property("speed-preset", "ultrafast")
    x264enc.set_property("bitrate", 2000)
    x264enc.set_property("key-int-max", 30)

    h264parse = Gst.ElementFactory.make("h264parse", "h264parse")
    h264parse.set_property("config-interval", 1)

    parse_caps = Gst.Caps.from_string("video/x-h264,stream-format=byte-stream,alignment=au")

    queue_v2 = Gst.ElementFactory.make("queue", "queue_v2")
    queue_v2.set_property("max-size-buffers", 5)
    queue_v2.set_property("max-size-time", 0)
    queue_v2.set_property("max-size-bytes", 0)
    queue_v2.set_property("leaky", 2)

    rtph264pay = Gst.ElementFactory.make("rtph264pay", "payv")
    rtph264pay.set_property("pt", 96)
    rtph264pay.set_property("config-interval", 1)
    rtph264pay.set_property("mtu", 1200)

    elements = [v4l2src, queue_v1, jpegdec, videoconvert, x264enc,
                h264parse, queue_v2, rtph264pay]
    for e in elements:
        pipeline.add(e)

    if not v4l2src.link_filtered(queue_v1, mjpeg_caps):
        raise RuntimeError("Failed to link v4l2src -> queue_v1")
    if not queue_v1.link(jpegdec):
        raise RuntimeError("Failed to link queue_v1 -> jpegdec")
    if not jpegdec.link(videoconvert):
        raise RuntimeError("Failed to link jpegdec -> videoconvert")
    if not videoconvert.link(x264enc):
        raise RuntimeError("Failed to link videoconvert -> x264enc")
    if not x264enc.link(h264parse):
        raise RuntimeError("Failed to link x264enc -> h264parse")
    if not h264parse.link_filtered(queue_v2, parse_caps):
        raise RuntimeError("Failed to link h264parse -> queue_v2")
    if not queue_v2.link(rtph264pay):
        raise RuntimeError("Failed to link queue_v2 -> rtph264pay")

    return pipeline


def build_video_chain_AFTER() -> tuple:
    """The PipelineBuilder way — same result, but the chain is immediately readable."""

    return (
        PipelineBuilder(name="video-pipeline")
        .element("v4l2src", name="src",
                 device="/dev/video0", do_timestamp=True)
        .caps("image/jpeg,width=1280,height=720,framerate=30/1")
        .element("queue", name="queue_v1",
                 max_size_buffers=3, max_size_time=0,
                 max_size_bytes=0, leaky=2)
        .element("jpegdec")
        .element("videoconvert")
        .element("x264enc", name="encoder",
                 tune="zerolatency", speed_preset="ultrafast",
                 bitrate=2000, key_int_max=30)
        .element("h264parse", config_interval=1)
        .caps("video/x-h264,stream-format=byte-stream,alignment=au")
        .element("queue", name="queue_v2",
                 max_size_buffers=5, max_size_time=0,
                 max_size_bytes=0, leaky=2)
        .element("rtph264pay", name="pay",
                 pt=96, config_interval=1, mtu=1200)
        .build()
    )


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 2: ALSA audio → Opus encode → RTP payload
#
# Same idea for the audio chain. Notice how readable the chain is —
# it maps directly to how you'd draw it on a whiteboard.
# ─────────────────────────────────────────────────────────────────────────────

def build_audio_chain() -> tuple:
    return (
        PipelineBuilder(name="audio-pipeline")
        .element("alsasrc", name="src",
                 device="default", do_timestamp=True,
                 buffer_time=20000, latency_time=10000)
        .element("queue", name="queue_a1",
                 max_size_buffers=4, max_size_time=0,
                 max_size_bytes=0, leaky=2)
        .element("audioresample")
        .element("audioconvert")
        .caps("audio/x-raw,channels=1,rate=48000")
        .element("opusenc", name="encoder",
                 bitrate=128000, complexity=0,
                 inband_fec=True, frame_size=10, max_payload_size=1200)
        .element("rtpopuspay", name="pay", pt=111, mtu=1200)
        .build()
    )


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 3: Accessing elements after build()
#
# build() returns a dict of elements so you don't have to call
# pipeline.get_by_name() everywhere. Both approaches work — the builder
# just makes the common case more convenient.
# ─────────────────────────────────────────────────────────────────────────────

def demonstrate_element_access():
    pipeline, elems = (
        PipelineBuilder()
        .element("videotestsrc", name="src", pattern=0)
        .element("videoconvert")
        .element("x264enc", name="encoder", bitrate=1000, tune="zerolatency")
        .element("fakesink", name="sink", sync=False)
        .build()
    )

    # Method 1: use the elements dict returned by build()
    encoder = elems["encoder"]
    print(f"Encoder bitrate: {encoder.get_property('bitrate')} kbps")

    # Method 2: standard GStreamer API — nothing is hidden
    src = pipeline.get_by_name("src")
    print(f"Source pattern: {src.get_property('pattern')}")

    # You can change properties at any time before or during PLAYING
    encoder.set_property("bitrate", 2000)
    print(f"Encoder bitrate after change: {encoder.get_property('bitrate')} kbps")

    pipeline.set_state(Gst.State.NULL)  # clean up
    return pipeline


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 4: What happens with bad input (clear error messages)
#
# The builder fails fast with actionable error messages rather than
# mysterious segfaults or None returns deep in the GStreamer internals.
# ─────────────────────────────────────────────────────────────────────────────

def demonstrate_error_messages():
    # Bad factory name
    try:
        PipelineBuilder().element("this_does_not_exist").build()
    except RuntimeError as e:
        print("=== Bad factory name ===")
        print(e)

    # Bad property
    try:
        PipelineBuilder().element("videotestsrc", not_a_real_property=42).build()
    except RuntimeError as e:
        print("\n=== Bad property ===")
        print(e)

    # Bad caps string
    try:
        PipelineBuilder().element("videotestsrc").caps("not valid caps!!!").build()
    except RuntimeError as e:
        print("\n=== Bad caps string ===")
        print(e)

    # Link failure (incompatible elements)
    try:
        PipelineBuilder() \
            .element("audiotestsrc") \
            .caps("video/x-raw")           # asking audio source to produce video caps
            # Note: this will fail during link, not caps parsing
    except RuntimeError as e:
        print("\n=== Link failure ===")
        print(e)


# ─────────────────────────────────────────────────────────────────────────────
# EXAMPLE 5: Going beyond the builder — extending a built pipeline
#
# For things the builder doesn't handle (dynamic pads, branching),
# you just use the standard GStreamer API on the returned pipeline.
# The builder and the raw API compose naturally.
# ─────────────────────────────────────────────────────────────────────────────

def build_pipeline_with_dynamic_pads():
    """
    decodebin has dynamic pads — its "pad-added" signal fires at runtime
    when it figures out what format it's dealing with. The builder can't
    wire that up at build() time, so we do it the normal GStreamer way.
    """
    pipeline, elems = (
        PipelineBuilder(name="decode-pipeline")
        .element("filesrc", name="src", location="/tmp/test.mp4")
        .element("decodebin", name="decoder")
        # We stop here — decodebin's output pads are dynamic
        .build()
    )

    decoder = elems["decoder"]

    def on_pad_added(decodebin, pad):
        """Called by GStreamer when decodebin figures out the stream format."""
        caps = pad.get_current_caps()
        structure = caps.get_structure(0)
        name = structure.get_name()

        if name.startswith("video/"):
            # Build the video sink chain and attach it dynamically
            convert = Gst.ElementFactory.make("videoconvert", None)
            sink = Gst.ElementFactory.make("autovideosink", None)
            pipeline.add(convert)
            pipeline.add(sink)
            convert.link(sink)
            convert.sync_state_with_parent()
            sink.sync_state_with_parent()
            pad.link(convert.get_static_pad("sink"))

    # Connect the dynamic pad signal manually — the builder doesn't need to
    # know anything about this; it just returned a real Gst.Pipeline
    decoder.connect("pad-added", on_pad_added)

    return pipeline


# ─────────────────────────────────────────────────────────────────────────────
# Run all examples
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Example 1: Building and validating video chain")
    print("=" * 60)

    # Build both versions and verify they produce equivalent pipelines
    before = build_video_chain_BEFORE()
    after_pipeline, after_elems = build_video_chain_AFTER()

    # Count elements — both should be equal
    before_count = sum(1 for _ in before.iterate_elements())
    after_count  = sum(1 for _ in after_pipeline.iterate_elements())
    print(f"Before (raw API): {before_count} elements")
    print(f"After (builder):  {after_count} elements")
    assert before_count == after_count, "Element count mismatch!"
    print("✓ Both pipelines have the same structure\n")

    print("=" * 60)
    print("Example 3: Element access")
    print("=" * 60)
    demonstrate_element_access()

    print()
    print("=" * 60)
    print("Example 4: Error messages")
    print("=" * 60)
    demonstrate_error_messages()

    # Clean up
    before.set_state(Gst.State.NULL)
    after_pipeline.set_state(Gst.State.NULL)

    print()
    print("All examples completed.")
