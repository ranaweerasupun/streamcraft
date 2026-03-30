"""
streamcraft.pipeline — Fluent builder for linear GStreamer pipelines.

Design philosophy
-----------------
This module solves one specific, well-bounded problem: building linear
GStreamer pipelines (A → B → C → ...) involves a lot of repetitive
boilerplate — ElementFactory.make(), set_property(), link(), link_filtered()
— that obscures what is actually a simple chain of data transformations.

The PipelineBuilder lets you express that chain concisely. It does NOT try
to abstract away GStreamer concepts like elements, pads, or caps — those
are still fully visible and named. It also does NOT try to handle every
possible pipeline topology. For branching, merging, or dynamic pads
(decodebin, webrtcbin), you work with the real Gst.Pipeline that build()
returns, using the standard GStreamer API.

Limitations (by design)
-----------------------
- Only linear topologies are supported in the builder itself.
- Caps filters between elements are supported (.caps()), but capssrc /
  capsfilter elements are NOT inserted — link_filtered() is used directly.
- Dynamic pads (e.g. decodebin's "pad-added" signal) must be connected
  manually after build() returns.
- Tee/branching pipelines should be started from a build() result and
  then extended manually.
"""

from __future__ import annotations

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field

# GStreamer must be initialized before any element operations.
# Calling init(None) multiple times is safe — it's a no-op after the first.
Gst.init(None)


# ──────────────────────────────────────────────────────────────────────────────
# Internal step types — these represent the *intent* before anything is built
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _ElementSpec:
    """Represents a pending element: factory name, optional element name, properties."""
    factory: str
    name: Optional[str]
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _CapsSpec:
    """
    Represents a caps constraint to apply on the link *after* the previous element.

    This becomes a link_filtered() call between adjacent elements. Note that
    GStreamer's link_filtered() implementation inserts a GstCapsFilter element
    into the pipeline automatically — so each .caps() call adds one element
    to the pipeline's element count beyond what .element() calls alone would
    produce. This is GStreamer's documented behavior and is the correct way
    to apply format constraints between elements.
    """
    caps_string: str


_Step = Union[_ElementSpec, _CapsSpec]


# ──────────────────────────────────────────────────────────────────────────────
# The public builder
# ──────────────────────────────────────────────────────────────────────────────

class PipelineBuilder:
    """
    A fluent, chainable builder for linear GStreamer pipelines.

    Each method returns `self` so you can chain calls. Call .build() at the
    end to get back a (Gst.Pipeline, elements_dict) tuple.

    The elements_dict maps each element's name (or factory name if you didn't
    supply a name) to its Gst.Element instance, so you can quickly grab
    elements you care about without calling pipeline.get_by_name().

    Example — reproducing the video chain from a robot teleoperation server:

        pipeline, elems = (
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

        # Access elements directly without get_by_name()
        encoder = elems["encoder"]
        encoder.set_property("bitrate", 4000)   # change bitrate at runtime

        # Or use the standard GStreamer API — nothing is hidden
        encoder = pipeline.get_by_name("encoder")

    Property naming
    ---------------
    GStreamer property names use hyphens (e.g. "speed-preset", "key-int-max").
    Python keyword arguments use underscores. The builder automatically converts
    underscores to hyphens when setting properties, so you can write:
        .element("x264enc", speed_preset="ultrafast", key_int_max=30)
    and the builder will call set_property("speed-preset", ...) correctly.

    If a property name doesn't have an underscore ambiguity (e.g. "bitrate",
    "device", "pt"), it works exactly as you'd expect.
    """

    def __init__(self, name: Optional[str] = None):
        """
        Args:
            name: Optional name for the Gst.Pipeline object itself.
                  Useful for debugging — shows up in bus messages and logs.
        """
        self._pipeline_name = name
        self._steps: List[_Step] = []

    # ── Builder methods ───────────────────────────────────────────────────────

    def element(
        self,
        factory: str,
        *,
        name: Optional[str] = None,
        **properties: Any,
    ) -> "PipelineBuilder":
        """
        Add a GStreamer element to the pipeline chain.

        Args:
            factory:    The GStreamer element factory name, e.g. "v4l2src",
                        "x264enc", "queue", "rtph264pay".
            name:       Optional human-readable name. If provided, this element
                        will be findable via pipeline.get_by_name(name) and
                        will appear in the returned elements dict under this key.
                        If omitted, the factory name is used as the dict key
                        (last element wins if the same factory appears twice).
            **properties: Element properties. Underscores become hyphens
                          automatically. All GStreamer property types are
                          supported (int, float, str, bool, enum ints).

        Returns:
            self, for chaining.

        Raises:
            RuntimeError: (deferred to build()) if the factory doesn't exist.
        """
        self._steps.append(
            _ElementSpec(factory=factory, name=name, properties=properties)
        )
        return self

    def caps(self, caps_string: str) -> "PipelineBuilder":
        """
        Insert a caps constraint on the link *following* the previous element.

        This is equivalent to calling link_filtered() between the previous and
        next element. It does NOT add a capsfilter element to the pipeline —
        the constraint is applied directly at the pad negotiation level.

        Caps strings use standard GStreamer notation:
            "video/x-h264,stream-format=byte-stream,alignment=au"
            "audio/x-raw,channels=1,rate=48000"
            "image/jpeg,width=1280,height=720,framerate=30/1"

        Args:
            caps_string: A GStreamer caps string.

        Returns:
            self, for chaining.

        Raises:
            ValueError: (deferred to build()) if the caps string is invalid.
        """
        self._steps.append(_CapsSpec(caps_string=caps_string))
        return self

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> Tuple[Gst.Pipeline, Dict[str, Gst.Element]]:
        """
        Materialize the pipeline: create all elements, set properties, link them.

        The returned Gst.Pipeline is a real, unstarted GStreamer pipeline.
        You own it entirely — set it to PLAYING, add more elements, connect
        signals, attach a bus watch, do whatever you need.

        Returns:
            A tuple (pipeline, elements) where:
            - pipeline is a Gst.Pipeline, ready to be set to PLAYING.
            - elements is a dict mapping element names to Gst.Element objects.
              Keys are either the 'name' you provided, or the factory name as
              a fallback. If you used the same factory twice without naming
              them, only the last one appears in the dict — use
              pipeline.get_by_name() instead in that case.

        Raises:
            RuntimeError: If any element factory is unknown, a property fails,
                          a caps string is invalid, or any link fails.
        """
        pipeline = Gst.Pipeline.new(self._pipeline_name)

        # We build a flat list of (element, caps_before_this_element) tuples.
        # caps_before is None for a plain link, or a Gst.Caps for a filtered link.
        built: List[Tuple[Gst.Element, Optional[Gst.Caps]]] = []
        pending_caps: Optional[Gst.Caps] = None
        elements: Dict[str, Gst.Element] = {}

        for step in self._steps:

            if isinstance(step, _CapsSpec):
                # Validate the caps string eagerly — fail fast with a clear message
                parsed = Gst.Caps.from_string(step.caps_string)
                if not parsed or parsed.is_empty():
                    raise RuntimeError(
                        f"Invalid or empty caps string: '{step.caps_string}'\n"
                        f"Caps strings use GStreamer notation, e.g.:\n"
                        f"  'video/x-h264,stream-format=byte-stream,alignment=au'\n"
                        f"  'audio/x-raw,channels=1,rate=48000'"
                    )
                # Caps will be applied on the NEXT link, not the previous one.
                # Think of it as: "the data leaving the previous element must
                # match this caps before entering the next element."
                pending_caps = parsed
                continue

            # It's an element spec — create it and add it to the pipeline
            elem = _make_element(step)
            pipeline.add(elem)

            built.append((elem, pending_caps))
            pending_caps = None  # caps consumed — reset for the next element

            # Register in the elements dict: explicit name takes priority,
            # factory name is the fallback
            key = step.name if step.name else step.factory
            elements[key] = elem

        # Now link all adjacent element pairs in sequence
        for i in range(len(built) - 1):
            src_elem, _          = built[i]
            dst_elem, filter_caps = built[i + 1]

            src_name = src_elem.get_name()
            dst_name = dst_elem.get_name()

            if filter_caps is not None:
                ok = src_elem.link_filtered(dst_elem, filter_caps)
                link_desc = f"link_filtered('{src_name}' -> '{dst_name}', caps={filter_caps.to_string()!r})"
            else:
                ok = src_elem.link(dst_elem)
                link_desc = f"link('{src_name}' -> '{dst_name}')"

            if not ok:
                raise RuntimeError(
                    f"Pipeline link failed: {link_desc}\n"
                    f"This usually means the elements have incompatible caps. "
                    f"Check that '{src_name}' can produce what '{dst_name}' expects.\n"
                    f"Tip: run `gst-inspect-1.0 {src_elem.get_factory().get_name()}` to see "
                    f"its source pad caps."
                )

        return pipeline, elements


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_element(spec: _ElementSpec) -> Gst.Element:
    """
    Create a GStreamer element from a spec and apply all its properties.

    This is a standalone function (not a method) so it can be tested
    independently and potentially reused outside the builder.
    """
    elem = Gst.ElementFactory.make(spec.factory, spec.name)

    if elem is None:
        # GStreamer returns None (not raises) when a factory isn't found.
        # We convert this to an exception with an actionable message.
        raise RuntimeError(
            f"GStreamer element factory '{spec.factory}' not found.\n"
            f"The plugin that provides this element may not be installed.\n"
            f"Common fixes:\n"
            f"  sudo apt install gstreamer1.0-plugins-base   # audioconvert, videoscale, ...\n"
            f"  sudo apt install gstreamer1.0-plugins-good   # v4l2src, jpegdec, ...\n"
            f"  sudo apt install gstreamer1.0-plugins-bad    # webrtcbin, ...\n"
            f"  sudo apt install gstreamer1.0-plugins-ugly   # x264enc, ...\n"
            f"  sudo apt install gstreamer1.0-libav          # h264 decode via ffmpeg\n"
            f"Verify the element exists: gst-inspect-1.0 {spec.factory}"
        )

    for python_name, value in spec.properties.items():
        # GStreamer uses hyphens in property names; Python uses underscores.
        # We convert automatically so callers can write speed_preset="ultrafast"
        # and have it correctly set "speed-preset".
        gst_name = python_name.replace("_", "-")
        try:
            elem.set_property(gst_name, value)
        except TypeError as exc:
            # TypeError is what GStreamer throws for wrong property type or unknown property.
            # We re-raise with context so the caller knows exactly where to look.
            raise RuntimeError(
                f"Failed to set property '{gst_name}' = {value!r} "
                f"on element '{spec.factory}'.\n"
                f"Original error: {exc}\n"
                f"Tip: run `gst-inspect-1.0 {spec.factory}` to see valid properties and types."
            ) from exc

    return elem
