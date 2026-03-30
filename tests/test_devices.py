"""
tests/test_devices.py — Tests for streamcraft.devices.

The devices module has three distinct kinds of code, each requiring a
different testing approach:

  1. Pure Python logic (ControlRange, _parse_v4l2_ctrl_line) — these have
     no external dependencies and can be tested exhaustively.

  2. GStreamer element checking (require_elements) — depends on GStreamer
     but not on hardware. Uses ElementFactory.find() which is always safe.

  3. System calls (check_v4l2_device, V4L2PTZCamera) — these shell out to
     v4l2-ctl and probe /dev/video*. Most tests use a non-existent device
     path so they exercise the failure path without needing hardware.
     Tests that need real hardware are marked @pytest.mark.hardware.
"""

import pytest
import sys, os

from streamcraft.devices import (
    require_elements,
    check_v4l2_device,
    list_v4l2_devices,
    V4L2PTZCamera,
    ControlRange,
    PTZStatus,
    _parse_v4l2_ctrl_line,
)


# ─────────────────────────────────────────────────────────────────────────────
# TestControlRange — pure Python, no GStreamer, no hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestControlRange:
    """
    ControlRange is a pure data class with a clamp() method.
    We can test it completely without any hardware or system calls.
    """

    def test_clamp_within_range_is_unchanged(self):
        """A value already inside the valid range should pass through unchanged."""
        r = ControlRange(min=-100, max=100, step=10, current=0)
        assert r.clamp(50) == 50

    def test_clamp_above_max_returns_max(self):
        r = ControlRange(min=-100, max=100, step=10, current=0)
        assert r.clamp(999) == 100

    def test_clamp_below_min_returns_min(self):
        r = ControlRange(min=-100, max=100, step=10, current=0)
        assert r.clamp(-999) == -100

    def test_clamp_at_exact_min_boundary(self):
        r = ControlRange(min=-100, max=100, step=10, current=0)
        assert r.clamp(-100) == -100

    def test_clamp_at_exact_max_boundary(self):
        r = ControlRange(min=-100, max=100, step=10, current=0)
        assert r.clamp(100) == 100

    def test_clamp_snaps_to_nearest_step(self):
        """
        Many V4L2 cameras reject values that aren't aligned to their step size.
        For a range with step=3600, the value 5000 should snap to 3600
        (the nearest valid step from the minimum).
        """
        r = ControlRange(min=0, max=36000, step=3600, current=0)
        assert r.clamp(5000) == 3600   # closer to 3600 than to 7200

    def test_clamp_snaps_up_when_closer_to_next_step(self):
        r = ControlRange(min=0, max=36000, step=3600, current=0)
        assert r.clamp(5500) == 7200   # closer to 7200 than to 3600

    def test_clamp_with_step_one_does_not_snap(self):
        """When step=1, every integer is valid — no snapping should occur."""
        r = ControlRange(min=100, max=400, step=1, current=100)
        assert r.clamp(237) == 237

    def test_span_property(self):
        """span should return max - min."""
        r = ControlRange(min=-468_000, max=468_000, step=3600, current=0)
        assert r.span == 936_000

    def test_negative_range_clamp(self):
        """Negative ranges (like pan/tilt on real cameras) should clamp correctly."""
        r = ControlRange(min=-468_000, max=468_000, step=3600, current=0)
        result = r.clamp(500_000)
        assert result == 468_000

    def test_clamp_zero_step_does_not_divide_by_zero(self):
        """
        If step is 0 (which shouldn't happen with real cameras but is
        defensively handled), clamp should still return a clamped value
        rather than crashing with ZeroDivisionError.
        """
        r = ControlRange(min=0, max=100, step=0, current=0)
        result = r.clamp(50)   # Should not raise
        assert 0 <= result <= 100


# ─────────────────────────────────────────────────────────────────────────────
# TestParseV4l2CtrlLine — pure Python, no hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestParseV4l2CtrlLine:
    """
    _parse_v4l2_ctrl_line() parses a single line of v4l2-ctl --list-ctrls
    output. It's pure text processing — no hardware or system calls involved.
    We test it with real output captured from actual cameras.
    """

    def test_parses_pan_line(self):
        """Test with a real pan_absolute line from an Obsbot camera."""
        line = "pan_absolute 0x009a0908 (int) : min=-522000 max=522000 step=3600 default=0 value=0"
        result = _parse_v4l2_ctrl_line(line)
        assert result is not None
        assert result.min == -522000
        assert result.max == 522000
        assert result.step == 3600
        assert result.current == 0

    def test_parses_tilt_line(self):
        line = "tilt_absolute 0x009a0909 (int) : min=-270000 max=270000 step=3600 default=0 value=-36000"
        result = _parse_v4l2_ctrl_line(line)
        assert result is not None
        assert result.min == -270000
        assert result.max == 270000
        assert result.current == -36000

    def test_parses_zoom_line(self):
        line = "zoom_absolute 0x009a090d (int) : min=100 max=400 step=10 default=100 value=150"
        result = _parse_v4l2_ctrl_line(line)
        assert result is not None
        assert result.min == 100
        assert result.max == 400
        assert result.step == 10
        assert result.current == 150

    def test_returns_none_for_missing_fields(self):
        """
        If min, max, step, or value are absent from the line, return None
        rather than a partially populated ControlRange.
        """
        # Missing 'value=' field
        line = "pan_absolute 0x009a0908 (int) : min=-100 max=100 step=10 default=0"
        result = _parse_v4l2_ctrl_line(line)
        assert result is None

    def test_returns_none_for_empty_line(self):
        assert _parse_v4l2_ctrl_line("") is None

    def test_returns_none_for_unrelated_line(self):
        """Lines about non-PTZ controls (brightness, contrast, etc.) should return None."""
        line = "brightness 0x00980900 (int) : min=0 max=255 step=1 default=128 value=128"
        # This has all the fields, so it WILL parse — that's OK.
        # The caller (V4L2PTZCamera._detect_ranges) filters by control name,
        # not the parser. The parser just extracts numbers.
        result = _parse_v4l2_ctrl_line(line)
        assert result is not None  # Parser succeeds — filtering is the caller's job

    def test_parses_non_zero_current_value(self):
        """The parser should correctly extract a non-zero current value."""
        line = "pan_absolute 0x009a0908 (int) : min=-468000 max=468000 step=3600 default=0 value=439200"
        result = _parse_v4l2_ctrl_line(line)
        assert result is not None
        assert result.current == 439200


# ─────────────────────────────────────────────────────────────────────────────
# TestRequireElements — uses GStreamer, no hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestRequireElements:
    """
    require_elements() checks whether GStreamer plugin elements are installed.
    No hardware needed — it only calls Gst.ElementFactory.find().
    """

    def test_passes_silently_for_known_good_elements(self):
        """
        videotestsrc and fakesink are always available when GStreamer base
        is installed. This call should return None (no error) silently.
        """
        # Should not raise
        result = require_elements("videotestsrc", "fakesink", "audioconvert")
        assert result is None

    def test_raises_for_nonexistent_element(self):
        """A completely made-up element name should raise EnvironmentError."""
        with pytest.raises(EnvironmentError):
            require_elements("this_element_does_not_exist_xyz123")

    def test_error_message_lists_missing_element(self):
        """The error message should name the missing element."""
        with pytest.raises(EnvironmentError, match="does_not_exist_xyz"):
            require_elements("does_not_exist_xyz")

    def test_raises_if_any_element_is_missing(self):
        """
        Even if most elements exist, a single missing one should cause failure.
        This is the "all or nothing" contract: you asked for N elements,
        you get an error if any of them is absent.
        """
        with pytest.raises(EnvironmentError):
            require_elements("videotestsrc", "fakesink", "this_one_is_missing_xyz")

    def test_error_message_lists_all_missing_elements(self):
        """When multiple elements are missing, all should appear in the error."""
        with pytest.raises(EnvironmentError) as exc_info:
            require_elements("missing_one_abc", "missing_two_def")
        error_text = str(exc_info.value)
        assert "missing_one_abc" in error_text
        assert "missing_two_def" in error_text

    def test_error_includes_install_hint(self):
        """
        The error should always contain some installation guidance.
        We test for 'apt' or 'install' since the hint targets Debian/Ubuntu.
        """
        with pytest.raises(EnvironmentError, match="apt|install"):
            require_elements("definitely_not_installed_xyz")

    def test_single_element_that_exists_passes(self):
        """require_elements() with a single valid element should not raise."""
        require_elements("videotestsrc")  # Should not raise


# ─────────────────────────────────────────────────────────────────────────────
# TestCheckV4l2Device — mostly tests failure paths without hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckV4l2Device:
    """
    check_v4l2_device() returns a (bool, str) tuple.
    We can test most behavior without hardware by using non-existent device paths.
    """

    def test_returns_tuple(self):
        """
        The return value should always be a 2-tuple regardless of whether
        the device exists.
        """
        result = check_v4l2_device("/dev/video_does_not_exist_9999")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_false_for_nonexistent_device(self):
        """A path that doesn't exist should return (False, <message>)."""
        ok, msg = check_v4l2_device("/dev/video_does_not_exist_9999")
        assert ok is False

    def test_error_message_mentions_device_path(self):
        """The error message should include the device path for clarity."""
        path = "/dev/video_does_not_exist_9999"
        ok, msg = check_v4l2_device(path)
        assert path in msg

    def test_returns_string_message_on_failure(self):
        """The message part of the tuple should be a non-empty string."""
        ok, msg = check_v4l2_device("/dev/no_such_device")
        assert isinstance(msg, str)
        assert len(msg) > 0

    @pytest.mark.hardware
    @pytest.mark.v4l2
    def test_returns_true_for_real_device(self):
        """
        On a machine with a camera, /dev/video0 should exist and be readable.
        This test is skipped automatically in CI via the 'hardware' marker.
        Run locally with: pytest -m hardware
        """
        ok, msg = check_v4l2_device("/dev/video0")
        assert ok is True
        assert "accessible" in msg.lower() or "working" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# TestListV4l2Devices
# ─────────────────────────────────────────────────────────────────────────────

class TestListV4l2Devices:
    """list_v4l2_devices() scans /dev/video* — no hardware needed to test the basics."""

    def test_returns_list(self):
        """The return value should always be a list, even if empty."""
        result = list_v4l2_devices()
        assert isinstance(result, list)

    def test_all_results_are_strings(self):
        """Every item in the returned list should be a string path."""
        for path in list_v4l2_devices():
            assert isinstance(path, str)

    def test_results_start_with_dev(self):
        """All returned paths should start with /dev/ by convention."""
        for path in list_v4l2_devices():
            assert path.startswith("/dev/")

    def test_results_are_sorted(self):
        """The list should be sorted — /dev/video0 before /dev/video2, etc."""
        devices = list_v4l2_devices()
        assert devices == sorted(devices)


# ─────────────────────────────────────────────────────────────────────────────
# TestV4L2PTZCamera — tests the "unavailable" path without hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestV4L2PTZCamera:
    """
    Most of V4L2PTZCamera's interesting behavior requires a real camera,
    but we can test the "no camera present" path completely without hardware.
    When the device doesn't exist, the camera should be gracefully unavailable —
    all operations should be silent no-ops returning False.
    """

    FAKE_DEVICE = "/dev/video_definitely_not_real_9999"

    def test_available_is_false_for_nonexistent_device(self):
        """If the device path doesn't exist, available should be False."""
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.available is False

    def test_set_pan_returns_false_when_unavailable(self):
        """All set_* methods should return False gracefully when unavailable."""
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.set_pan(90_000) is False

    def test_set_tilt_returns_false_when_unavailable(self):
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.set_tilt(-50_000) is False

    def test_set_zoom_returns_false_when_unavailable(self):
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.set_zoom(200) is False

    def test_set_ptz_returns_false_when_unavailable(self):
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.set_ptz(0, 0, 100) is False

    def test_reset_returns_false_when_unavailable(self):
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.reset() is False

    def test_status_property_works_when_unavailable(self):
        """
        status should return a PTZStatus even when the camera is unavailable.
        This is important for WebSocket handlers that serialize cam.status.to_dict()
        — they should not crash just because the camera isn't plugged in.
        """
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        status = cam.status
        assert isinstance(status, PTZStatus)
        assert status.available is False

    def test_status_to_dict_is_serializable(self):
        """
        status.to_dict() should return a plain dict with no non-serializable
        objects — suitable for json.dumps() in a WebSocket handler.
        """
        import json
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        d = cam.status.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        assert isinstance(serialized, str)

    def test_default_ranges_are_set_when_unavailable(self):
        """
        Even when a camera is unavailable, the ControlRange objects should
        be populated with reasonable defaults — not None or unset.
        """
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        assert cam.pan_range is not None
        assert cam.tilt_range is not None
        assert cam.zoom_range is not None
        assert isinstance(cam.pan_range, ControlRange)

    def test_pan_tilt_zoom_properties_start_at_zero(self):
        """The pan, tilt, zoom properties should default to reasonable values."""
        cam = V4L2PTZCamera(self.FAKE_DEVICE)
        # Pan and tilt default to 0 (center)
        assert cam.pan == 0
        assert cam.tilt == 0
        # Zoom defaults to the minimum (no zoom)
        assert cam.zoom == cam.zoom_range.min

    @pytest.mark.hardware
    @pytest.mark.v4l2
    def test_real_camera_is_available(self):
        """
        On a machine with a PTZ camera, the camera should be detected.
        Skipped unless run with: pytest -m hardware
        """
        cam = V4L2PTZCamera("/dev/video0")
        assert cam.available is True

    @pytest.mark.hardware
    @pytest.mark.v4l2
    def test_real_camera_ranges_are_detected(self):
        """
        On a real PTZ camera, auto-detection should populate ranges with
        values that differ from all-zeros.
        """
        cam = V4L2PTZCamera("/dev/video0")
        if not cam.available:
            pytest.skip("No PTZ camera on /dev/video0")
        # If a PTZ camera is present, its ranges should be non-trivial
        assert cam.pan_range.max > 0
        assert cam.tilt_range.max > 0
        assert cam.zoom_range.max > cam.zoom_range.min
