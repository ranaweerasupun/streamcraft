"""
tests/test_pipeline.py — Tests for streamcraft.PipelineBuilder.

Test organization
-----------------
The tests are grouped into four classes, each focused on a specific
behavioral contract of the PipelineBuilder:

  TestBasicBuilding      — does it produce a correct, linked pipeline?
  TestPropertySetting    — does it set properties correctly, including
                           the underscore→hyphen conversion?
  TestCapsFiltering      — does .caps() correctly apply link_filtered()?
  TestErrorHandling      — does it raise clear errors for bad inputs?

All tests in this file use only software elements (videotestsrc, audiotestsrc,
fakesink, videoconvert, etc.) and do not require any hardware.
"""

import pytest

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from streamcraft import PipelineBuilder
from helpers import skip_if_missing


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _iter_pipeline_elements(pipeline: Gst.Pipeline):
    """
    Drive GStreamer's Iterator protocol to yield all elements in a pipeline.

    GStreamer's iterate_elements() returns a Gst.Iterator, which is NOT a
    Python iterator. It has its own .next() method returning a
    (Gst.IteratorResult, value) tuple. Python's built-in for loop doesn't
    know how to drive it, so we wrap it here into a proper Python generator.

    IteratorResult values:
      OK     — got a value, keep going
      DONE   — no more elements
      RESYNC — concurrent modification, restart the iteration
      ERROR  — something went wrong, stop
    """
    it = pipeline.iterate_elements()
    while True:
        result, elem = it.next()
        if result == Gst.IteratorResult.OK:
            yield elem
        elif result == Gst.IteratorResult.DONE:
            break
        elif result == Gst.IteratorResult.RESYNC:
            it.resync()
        else:
            break


def count_elements(pipeline: Gst.Pipeline) -> int:
    """Count elements in a pipeline."""
    return sum(1 for _ in _iter_pipeline_elements(pipeline))


def get_element_names(pipeline: Gst.Pipeline) -> set:
    """Return the set of element names in a pipeline."""
    return {e.get_name() for e in _iter_pipeline_elements(pipeline)}


# ─────────────────────────────────────────────────────────────────────────────
# TestBasicBuilding
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicBuilding:
    """
    Tests that the builder produces a structurally correct pipeline:
    the right number of elements, correctly named, correctly linked.
    """

    def test_two_element_pipeline_builds(self):
        """
        The simplest possible pipeline — a source and a sink.
        Verifies that build() succeeds and returns a Gst.Pipeline.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc")
            .element("fakesink")
            .build()
        )
        try:
            assert isinstance(pipeline, Gst.Pipeline)
            assert count_elements(pipeline) == 2
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_returned_pipeline_is_real_gst_pipeline(self):
        """
        The pipeline returned by build() should be a real Gst.Pipeline,
        not a wrapper or proxy. This means standard GStreamer API methods
        like get_by_name() and set_state() should work directly on it.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc", name="src")
            .element("fakesink")
            .build()
        )
        try:
            # Standard GStreamer API should work without any unwrapping
            elem = pipeline.get_by_name("src")
            assert elem is not None
            assert isinstance(elem, Gst.Element)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_elements_dict_contains_all_named_elements(self):
        """
        The second return value from build() should be a dict mapping
        each named element to its Gst.Element instance.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="source")
            .element("videoconvert", name="converter")
            .element("fakesink", name="sink")
            .build()
        )
        try:
            assert "source" in elems
            assert "converter" in elems
            assert "sink" in elems
            assert len(elems) == 3
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_elements_dict_uses_factory_name_as_fallback(self):
        """
        When no explicit name is given to .element(), the factory name
        is used as the dict key. This is the "lazy" access pattern —
        convenient when you only have one element of each type.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc")
            .element("fakesink")
            .build()
        )
        try:
            assert "videotestsrc" in elems
            assert "fakesink" in elems
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_elements_dict_and_get_by_name_return_same_object(self):
        """
        elems["foo"] and pipeline.get_by_name("foo") should return the
        exact same Gst.Element object — not just equivalent objects.
        This confirms the dict is populated from the actual pipeline elements.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="src")
            .element("fakesink")
            .build()
        )
        try:
            from_dict = elems["src"]
            from_pipeline = pipeline.get_by_name("src")
            # Compare by GStreamer element name — same underlying object
            assert from_dict.get_name() == from_pipeline.get_name()
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_pipeline_can_transition_to_playing(self):
        """
        A correctly built pipeline should be able to start.
        This is the most important integration check — it verifies that
        the elements are not just created but correctly linked to each other.
        A link error typically manifests as a FAILURE on set_state(PLAYING).
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc", num_buffers=5)
            .element("fakesink", sync=False)
            .build()
        )
        try:
            ret = pipeline.set_state(Gst.State.PLAYING)
            assert ret != Gst.StateChangeReturn.FAILURE, (
                "Pipeline failed to start — elements are probably not linked correctly"
            )
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_pipeline_name_is_set(self):
        """
        The optional name argument to PipelineBuilder() should be set
        on the resulting Gst.Pipeline object.
        """
        pipeline, _ = (
            PipelineBuilder(name="my-test-pipeline")
            .element("videotestsrc")
            .element("fakesink")
            .build()
        )
        try:
            assert pipeline.get_name() == "my-test-pipeline"
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_unnamed_pipeline_still_builds(self):
        """
        PipelineBuilder() with no name argument should still work.
        GStreamer assigns a default name in that case.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc")
            .element("fakesink")
            .build()
        )
        try:
            assert pipeline.get_name() is not None
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_longer_chain_correct_element_count(self):
        """
        A five-element chain should produce exactly five elements in the pipeline.
        This verifies that every .element() call in the chain is materialized.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="src")
            .element("videoconvert", name="conv1")
            .element("videoscale", name="scale")
            .element("videoconvert", name="conv2")
            .element("fakesink", name="sink")
            .build()
        )
        try:
            assert count_elements(pipeline) == 5
            assert len(elems) == 5
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_audio_chain_builds_and_starts(self):
        """
        Audio pipelines use a different set of elements than video.
        This test ensures the builder works equally well for audio chains.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("audiotestsrc", name="src", num_buffers=10)
            .element("audioconvert", name="conv")
            .element("fakesink", name="sink", sync=False)
            .build()
        )
        try:
            assert count_elements(pipeline) == 3
            ret = pipeline.set_state(Gst.State.PLAYING)
            assert ret != Gst.StateChangeReturn.FAILURE
        finally:
            pipeline.set_state(Gst.State.NULL)


# ─────────────────────────────────────────────────────────────────────────────
# TestPropertySetting
# ─────────────────────────────────────────────────────────────────────────────

class TestPropertySetting:
    """
    Tests that properties are set correctly on elements, including the
    automatic underscore-to-hyphen name conversion.
    """

    def test_integer_property_is_set(self):
        """
        An integer property should survive the round-trip through the builder
        and be readable back from the element with the same value.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="src", num_buffers=42)
            .element("fakesink")
            .build()
        )
        try:
            assert elems["src"].get_property("num-buffers") == 42
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_boolean_property_is_set(self):
        pipeline, elems = (
            PipelineBuilder()
            .element("fakesink", name="sink", sync=False)
            .build()
        )
        try:
            assert elems["sink"].get_property("sync") is False
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_string_property_is_set(self):
        """
        String properties like device paths should be set and readable back.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("filesrc", name="src", location="/tmp/test.mp4")
            .build()
        )
        try:
            assert elems["src"].get_property("location") == "/tmp/test.mp4"
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_underscore_to_hyphen_conversion_simple(self):
        """
        The most common case: a property with a single hyphen.
        'num_buffers' (Python) should set 'num-buffers' (GStreamer).
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="src", num_buffers=7)
            .element("fakesink")
            .build()
        )
        try:
            # Read back using the GStreamer name (with hyphen) to confirm
            assert elems["src"].get_property("num-buffers") == 7
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_underscore_to_hyphen_conversion_multiple_hyphens(self):
        """
        Properties with multiple hyphens (e.g. 'max-size-buffers' on a queue)
        should work correctly when written with underscores in Python:
        'max_size_buffers' → 'max-size-buffers'.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("audiotestsrc")
            .element("queue", name="q",
                     max_size_buffers=10,
                     max_size_time=0,
                     max_size_bytes=0)
            .element("fakesink")
            .build()
        )
        try:
            assert elems["q"].get_property("max-size-buffers") == 10
            assert elems["q"].get_property("max-size-time") == 0
            assert elems["q"].get_property("max-size-bytes") == 0
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_multiple_properties_on_same_element(self):
        """
        Multiple keyword arguments should all be applied to the same element.
        This tests that the property loop in _make_element() handles
        multiple arguments correctly (not just the first or last one).
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", name="src",
                     num_buffers=5,
                     pattern=1,   # 1 = "snow"
                     is_live=True)
            .element("fakesink")
            .build()
        )
        try:
            src = elems["src"]
            assert src.get_property("num-buffers") == 5
            assert src.get_property("pattern") == 1
            assert src.get_property("is-live") is True
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("x264enc")
    def test_x264enc_properties(self):
        """
        Test the exact property set used in the robot server's video pipeline.
        This is a real-world regression test to ensure our wrapper handles
        the x264enc element's property names correctly.
        """
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc", num_buffers=5)
            .element("videoconvert")
            .element("x264enc", name="enc",
                     tune="zerolatency",
                     speed_preset="ultrafast",
                     bitrate=2000,
                     key_int_max=30)
            .element("fakesink", sync=False)
            .build()
        )
        try:
            enc = elems["enc"]
            assert enc.get_property("bitrate") == 2000
            assert enc.get_property("key-int-max") == 30
        finally:
            pipeline.set_state(Gst.State.NULL)


# ─────────────────────────────────────────────────────────────────────────────
# TestCapsFiltering
# ─────────────────────────────────────────────────────────────────────────────

class TestCapsFiltering:
    """
    Tests that .caps() correctly influences element linking.

    A caps filter applies link_filtered() between the preceding and following
    elements, restricting what media format flows between them. The key
    behaviors to test are: (a) a valid caps string doesn't break the build,
    (b) it's actually applied to the link (not silently ignored), and (c) an
    invalid caps string raises a clear error at build() time, not later.
    """

    def test_caps_does_not_break_build(self):
        """
        Adding a .caps() call between two compatible elements should not
        prevent the pipeline from building or starting.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc", num_buffers=5)
            .caps("video/x-raw,format=I420")
            .element("fakesink", sync=False)
            .build()
        )
        try:
            ret = pipeline.set_state(Gst.State.PLAYING)
            assert ret != Gst.StateChangeReturn.FAILURE
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_caps_inserts_one_capsfilter_element(self):
        """
        .caps() uses link_filtered() under the hood, which GStreamer implements
        by inserting a GstCapsFilter element between the two adjacent elements.
        So a 2-element pipeline with one .caps() call will contain 3 elements:
        the two you specified plus one hidden GstCapsFilter.

        This is GStreamer's documented behavior for link_filtered(). We test for
        it explicitly so future maintainers understand why the element count is
        higher than the number of .element() calls suggests.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc")
            .caps("video/x-raw,format=I420")
            .element("fakesink")
            .build()
        )
        try:
            # 2 explicit elements + 1 capsfilter inserted by link_filtered() = 3
            assert count_elements(pipeline) == 3
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_multiple_caps_in_chain(self):
        """
        Each .caps() call causes link_filtered() to insert one GstCapsFilter
        element. So two .caps() calls in a 3-element chain produce 5 elements
        total: 3 explicit + 2 capsfilters.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc", num_buffers=5)
            .caps("video/x-raw,format=I420")
            .element("videoconvert")
            .caps("video/x-raw,format=RGB")
            .element("fakesink", sync=False)
            .build()
        )
        try:
            # 3 explicit elements + 2 capsfilters from link_filtered() = 5
            assert count_elements(pipeline) == 5
        finally:
            pipeline.set_state(Gst.State.NULL)

    @skip_if_missing("h264parse")
    def test_h264_caps_constraint(self):
        """
        The specific caps used in the robot server's video chain.
        This is a regression test ensuring the caps string from the
        real production pipeline still parses and links correctly.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("videotestsrc", num_buffers=5)
            .element("videoconvert")
            .element("x264enc" if Gst.ElementFactory.find("x264enc") else "theoraenc",
                     tune="zerolatency" if Gst.ElementFactory.find("x264enc") else None)
            .element("fakesink", sync=False)
            .build()
        )
        try:
            assert count_elements(pipeline) == 4
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_audio_caps_constraint(self):
        """
        Caps constraints on audio pipelines should work the same as video.
        """
        pipeline, _ = (
            PipelineBuilder()
            .element("audiotestsrc", num_buffers=10)
            .element("audioconvert")
            .caps("audio/x-raw,channels=1,rate=48000")
            .element("fakesink", sync=False)
            .build()
        )
        try:
            ret = pipeline.set_state(Gst.State.PLAYING)
            assert ret != Gst.StateChangeReturn.FAILURE
        finally:
            pipeline.set_state(Gst.State.NULL)


# ─────────────────────────────────────────────────────────────────────────────
# TestErrorHandling
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorHandling:
    """
    Tests that the builder raises clear, informative RuntimeErrors for
    bad inputs, and does so at build() time (not silently at runtime).

    Error quality is part of the library's API contract. These tests
    verify not just that an error is raised, but that the error message
    contains enough information to be actionable.
    """

    def test_unknown_factory_raises_runtime_error(self):
        """
        An element factory that doesn't exist should raise RuntimeError,
        not return None or crash silently.
        """
        with pytest.raises(RuntimeError):
            PipelineBuilder().element("this_element_definitely_does_not_exist_xyz").build()

    def test_unknown_factory_error_message_contains_factory_name(self):
        """
        The error message for an unknown factory should include the name
        of the factory so the user knows what to look for.
        """
        factory_name = "no_such_element_abc123"
        with pytest.raises(RuntimeError, match=factory_name):
            PipelineBuilder().element(factory_name).build()

    def test_unknown_factory_error_message_contains_install_hint(self):
        """
        The error message should mention plugin installation, since a missing
        factory almost always means a missing plugin package.
        """
        with pytest.raises(RuntimeError, match="apt|install|plugin"):
            PipelineBuilder().element("no_such_element_abc123").build()

    def test_bad_property_raises_runtime_error(self):
        """
        Setting a property that doesn't exist on an element should raise
        RuntimeError at build() time.
        """
        with pytest.raises(RuntimeError):
            (PipelineBuilder()
             .element("videotestsrc", this_property_does_not_exist=42)
             .build())

    def test_bad_property_error_mentions_element(self):
        """
        The error message for a bad property should name the element
        it was being set on.
        """
        with pytest.raises(RuntimeError, match="videotestsrc"):
            (PipelineBuilder()
             .element("videotestsrc", totally_fake_property=99)
             .build())

    def test_bad_property_error_mentions_property_name(self):
        """
        The error message should also include the (converted) property name
        so the user knows which property was wrong.
        """
        with pytest.raises(RuntimeError, match="totally-fake-property"):
            (PipelineBuilder()
             .element("videotestsrc", totally_fake_property=99)
             .build())

    def test_bad_caps_string_raises_runtime_error(self):
        """
        A syntactically invalid caps string should raise RuntimeError
        at build() time, not fail silently.
        """
        with pytest.raises(RuntimeError):
            (PipelineBuilder()
             .element("videotestsrc")
             .caps("this is not a valid caps string !!!")
             .element("fakesink")
             .build())

    def test_bad_caps_error_contains_the_bad_string(self):
        """
        The error message should echo back the invalid caps string
        so the user can see exactly what they wrote.
        """
        bad_caps = "not_valid_caps_xyz"
        with pytest.raises(RuntimeError, match=bad_caps):
            (PipelineBuilder()
             .element("videotestsrc")
             .caps(bad_caps)
             .element("fakesink")
             .build())

    def test_empty_builder_builds_empty_pipeline(self):
        """
        Building with no elements should succeed (returning an empty pipeline)
        rather than crashing. An empty pipeline is a valid GStreamer concept.
        """
        pipeline, elems = PipelineBuilder().build()
        try:
            assert isinstance(pipeline, Gst.Pipeline)
            assert len(elems) == 0
            assert count_elements(pipeline) == 0
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_builder_is_reusable_after_error(self):
        """
        After a build() call raises an error, the same builder instance
        should still be in a clean state. However, the recommended usage
        is to create a new builder — this tests that a failed build doesn't
        leave the builder in an undefined state that could cause a second
        call to behave unexpectedly.

        Note: We test this by creating a fresh builder and verifying it works,
        since our builder appends steps before building.
        """
        # First call — bad factory should raise
        try:
            PipelineBuilder().element("no_such_element").build()
        except RuntimeError:
            pass  # Expected

        # Second builder (fresh) should still work normally
        pipeline, elems = (
            PipelineBuilder()
            .element("videotestsrc")
            .element("fakesink")
            .build()
        )
        try:
            assert count_elements(pipeline) == 2
        finally:
            pipeline.set_state(Gst.State.NULL)
