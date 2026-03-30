"""
streamcraft — Friendly Python utilities for GStreamer.

Sits on top of the standard gi.repository GStreamer bindings,
reducing boilerplate while keeping all GStreamer concepts visible.

Quick start:
    from streamcraft import PipelineBuilder

    pipeline, elems = (
        PipelineBuilder()
        .element("v4l2src", name="src", device="/dev/video0")
        .caps("image/jpeg,width=1280,height=720,framerate=30/1")
        .element("queue", max_size_buffers=3, leaky=2)
        .element("jpegdec")
        .element("videoconvert")
        .element("x264enc", tune="zerolatency", speed_preset="ultrafast", bitrate=2000)
        .element("h264parse", config_interval=1)
        .element("rtph264pay", pt=96)
        .build()
    )

    # The pipeline is a real Gst.Pipeline — do anything with it
    encoder = elems["encoder"]   # or pipeline.get_by_name("encoder")
    pipeline.set_state(Gst.State.PLAYING)
"""

from .pipeline import PipelineBuilder
from .devices import (
    require_elements,
    check_v4l2_device,
    list_v4l2_devices,
    V4L2PTZCamera,
    PTZStatus,
    ControlRange,
)
from .webrtc import WebRTCSession

__all__ = [
    # Pipeline building
    "PipelineBuilder",
    # Device utilities
    "require_elements",
    "check_v4l2_device",
    "list_v4l2_devices",
    "V4L2PTZCamera",
    "PTZStatus",
    "ControlRange",
    # WebRTC session management
    "WebRTCSession",
]
__version__ = "0.1.0"