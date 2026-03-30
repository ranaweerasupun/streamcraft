"""
streamcraft.devices — Device detection and camera control for GStreamer pipelines.

This module solves two distinct but related problems that come up early in
every GStreamer project:

  1. "Does this machine have what my pipeline needs?" — checked via
     require_elements() and check_v4l2_device() before you try to build.

  2. "I have a PTZ camera; how do I control it?" — answered by V4L2PTZCamera,
     which auto-detects a camera's capabilities and presents a clean API.

The design philosophy is the same as the rest of streamcraft: we sit on top of
the standard tools (v4l2-ctl, GStreamer's ElementFactory) without hiding them.
If you need to go deeper, the underlying commands and APIs are still there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)


# ─────────────────────────────────────────────────────────────────────────────
# Part 1: Environment / dependency checking
#
# These functions answer the question "can I even start?" before you spend
# time building a pipeline that will fail in a confusing way at runtime.
# ─────────────────────────────────────────────────────────────────────────────

def require_elements(*factory_names: str) -> None:
    """
    Assert that all named GStreamer element factories are available on this system.

    Call this near the top of your program, before building any pipelines.
    If anything is missing, you get a clear error message with the exact
    apt-get command to fix it — rather than a cryptic None or segfault
    later when the missing element is actually needed.

    Args:
        *factory_names: One or more GStreamer element factory names, e.g.:
            require_elements("webrtcbin", "x264enc", "alsasrc", "rtph264pay")

    Raises:
        EnvironmentError: If one or more elements are not found, with a
            grouped message listing all missing elements and install hints.

    Example:
        # Check everything your pipeline needs in one shot at startup
        require_elements(
            "webrtcbin", "v4l2src", "jpegdec", "x264enc",
            "h264parse", "rtph264pay", "alsasrc", "opusenc", "rtpopuspay"
        )
    """
    missing = [
        name for name in factory_names
        if Gst.ElementFactory.find(name) is None
    ]

    if not missing:
        return  # All good — fast path, no output

    # Build a helpful error message. We group the missing elements by which
    # plugin package typically provides them, so the user knows what to install.
    _PLUGIN_HINTS = {
        "v4l2src":       "gstreamer1.0-plugins-good",
        "jpegdec":       "gstreamer1.0-plugins-good",
        "rtpvp8pay":     "gstreamer1.0-plugins-good",
        "rtpopuspay":    "gstreamer1.0-plugins-good",
        "alsasrc":       "gstreamer1.0-alsa",
        "alsasink":      "gstreamer1.0-alsa",
        "pulsesrc":      "gstreamer1.0-pulseaudio",
        "pulsesink":     "gstreamer1.0-pulseaudio",
        "audioconvert":  "gstreamer1.0-plugins-base",
        "audioresample": "gstreamer1.0-plugins-base",
        "videoconvert":  "gstreamer1.0-plugins-base",
        "videoscale":    "gstreamer1.0-plugins-base",
        "opusenc":       "gstreamer1.0-plugins-base",
        "vorbisenc":     "gstreamer1.0-plugins-base",
        "theoraenc":     "gstreamer1.0-plugins-base",
        "x264enc":       "gstreamer1.0-plugins-ugly",
        "x265enc":       "gstreamer1.0-plugins-ugly",
        "lamemp3enc":    "gstreamer1.0-plugins-ugly",
        "webrtcbin":     "gstreamer1.0-plugins-bad",
        "dtlssrtpdec":   "gstreamer1.0-plugins-bad",
        "srtpdec":       "gstreamer1.0-plugins-bad",
        "h264parse":     "gstreamer1.0-plugins-bad",
        "decodebin":     "gstreamer1.0-plugins-base",
        "decodebin3":    "gstreamer1.0-plugins-base",
        "avdec_h264":    "gstreamer1.0-libav",
        "avdec_h265":    "gstreamer1.0-libav",
    }

    # Collect suggested packages (deduped, preserving order)
    seen_pkgs: Dict[str, bool] = {}
    for name in missing:
        pkg = _PLUGIN_HINTS.get(name)
        if pkg and pkg not in seen_pkgs:
            seen_pkgs[pkg] = True

    lines = [
        f"Missing GStreamer elements: {', '.join(missing)}",
        "",
        "These elements were not found on this system.",
        "Verify each one with:  gst-inspect-1.0 <element-name>",
    ]

    if seen_pkgs:
        lines += [
            "",
            "Suggested packages to install:",
            "  sudo apt update && sudo apt install -y " + " ".join(seen_pkgs),
        ]
    else:
        lines += [
            "",
            "Install the relevant GStreamer plugin packages for your distribution.",
            "On Debian/Ubuntu: sudo apt install gstreamer1.0-plugins-{base,good,bad,ugly}",
        ]

    raise EnvironmentError("\n".join(lines))


def check_v4l2_device(device: str = "/dev/video0") -> Tuple[bool, str]:
    """
    Check whether a V4L2 video device exists and is readable.

    This is a lightweight check — it verifies the device file exists and
    that a 1-buffer test pipeline can open it. It does NOT try to capture
    a full frame, so it returns quickly.

    Args:
        device: The device path, e.g. "/dev/video0".

    Returns:
        A (success, message) tuple. success is True if the device is usable.
        message is a human-readable description of what was found or what
        went wrong — suitable for logging or displaying to the user.

    Example:
        ok, msg = check_v4l2_device("/dev/video0")
        if not ok:
            print(f"Camera not available: {msg}")
            sys.exit(1)
        print(msg)  # e.g. "Device /dev/video0 is accessible"
    """
    if not os.path.exists(device):
        return False, f"Device {device!r} does not exist"

    if not os.access(device, os.R_OK):
        return False, (
            f"Device {device!r} exists but is not readable. "
            f"Try: sudo usermod -aG video $USER  (then re-login)"
        )

    # Attempt a 1-buffer pipeline to confirm the device actually opens.
    # This catches "device busy" and driver errors that file permissions can't detect.
    test_pipe = None
    try:
        test_pipe = Gst.parse_launch(
            f"v4l2src device={device} num-buffers=1 ! fakesink"
        )
        ret = test_pipe.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            return False, (
                f"Device {device!r} could not be opened by GStreamer. "
                f"It may be in use by another process."
            )
        return True, f"Device {device!r} is accessible"
    except Exception as exc:
        return False, f"Device {device!r} test failed: {exc}"
    finally:
        if test_pipe is not None:
            test_pipe.set_state(Gst.State.NULL)


def list_v4l2_devices() -> List[str]:
    """
    Return a list of V4L2 video device paths available on this system.

    Scans /dev/video* and returns paths that exist. Does not verify that
    each device is actually openable — use check_v4l2_device() for that.

    Returns:
        A sorted list of device paths, e.g. ["/dev/video0", "/dev/video2"].
        Returns an empty list if no devices are found.
    """
    import glob
    return sorted(glob.glob("/dev/video*"))


# ─────────────────────────────────────────────────────────────────────────────
# Part 2: PTZ camera control
#
# V4L2PTZCamera wraps v4l2-ctl to control pan, tilt, and zoom on any
# camera that exposes those controls via the V4L2 extended controls API.
# Tested with Obsbot Tail Air, Obsbot Meet 4K, Logitech PTZ Pro, and similar.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ControlRange:
    """The valid range for a single PTZ control axis, as reported by the camera."""
    min: int
    max: int
    step: int
    current: int

    def clamp(self, value: int) -> int:
        """Clamp a value to this range, snapping to the nearest valid step."""
        clamped = max(self.min, min(self.max, value))
        # Snap to nearest valid step from the minimum
        if self.step > 0:
            offset = clamped - self.min
            snapped = self.min + round(offset / self.step) * self.step
            return max(self.min, min(self.max, snapped))
        return clamped

    @property
    def span(self) -> int:
        """The total range from min to max."""
        return self.max - self.min


@dataclass
class PTZStatus:
    """A snapshot of the camera's current state, including all ranges."""
    available: bool
    device: str
    pan: int = 0
    tilt: int = 0
    zoom: int = 0
    pan_range: Optional[ControlRange] = None
    tilt_range: Optional[ControlRange] = None
    zoom_range: Optional[ControlRange] = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict — convenient for sending over WebSocket."""
        return {
            "available": self.available,
            "device": self.device,
            "pan": self.pan,
            "tilt": self.tilt,
            "zoom": self.zoom,
            "ranges": {
                "pan":  {"min": self.pan_range.min,  "max": self.pan_range.max}  if self.pan_range  else None,
                "tilt": {"min": self.tilt_range.min, "max": self.tilt_range.max} if self.tilt_range else None,
                "zoom": {"min": self.zoom_range.min, "max": self.zoom_range.max} if self.zoom_range else None,
            },
        }


class V4L2PTZCamera:
    """
    Control pan, tilt, and zoom on any V4L2 camera that supports those controls.

    On creation, the class uses v4l2-ctl to auto-detect which controls the
    camera exposes and what ranges are valid for each. This means the same
    class works with any PTZ camera without you needing to hardcode min/max
    values per camera model.

    All set_*() methods silently clamp values to the camera's valid range,
    so callers don't need to guard against out-of-bounds values. If a control
    is not available on the connected camera, the method does nothing and
    returns False.

    If the camera is not connected, all operations are no-ops. This is
    intentional — it allows code that uses PTZ control to start successfully
    even when no PTZ camera is plugged in, and add the camera later.

    Example:
        cam = V4L2PTZCamera("/dev/video0")

        if cam.available:
            print(cam.status)
            cam.set_pan(90_000)     # tilt right (units are arc-seconds * 100)
            cam.set_tilt(-50_000)   # tilt down
            cam.set_zoom(200)       # 2x zoom (if zoom range is 100–400)
            cam.reset()             # back to center, minimum zoom

        # Or set everything at once:
        cam.set_ptz(pan=0, tilt=0, zoom=100)
    """

    # Default fallback ranges used when auto-detection fails or a control
    # isn't listed. These are typical values for mid-range PTZ cameras.
    _DEFAULT_PAN_MIN  = -468_000
    _DEFAULT_PAN_MAX  =  468_000
    _DEFAULT_TILT_MIN = -324_000
    _DEFAULT_TILT_MAX =  324_000
    _DEFAULT_ZOOM_MIN =  100
    _DEFAULT_ZOOM_MAX =  400

    def __init__(self, device: str = "/dev/video0"):
        self.device = device
        self.available = False

        # Ranges — populated by _detect_ranges(), defaulted here so that
        # all attributes always exist even if detection fails
        self.pan_range  = ControlRange(self._DEFAULT_PAN_MIN,  self._DEFAULT_PAN_MAX,  3600, 0)
        self.tilt_range = ControlRange(self._DEFAULT_TILT_MIN, self._DEFAULT_TILT_MAX, 3600, 0)
        self.zoom_range = ControlRange(self._DEFAULT_ZOOM_MIN, self._DEFAULT_ZOOM_MAX, 1,    self._DEFAULT_ZOOM_MIN)

        # Current values — updated on every successful set_*() call
        self._pan  = 0
        self._tilt = 0
        self._zoom = self._DEFAULT_ZOOM_MIN

        self._check_availability()
        if self.available:
            self._detect_ranges()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def pan(self) -> int:
        return self._pan

    @property
    def tilt(self) -> int:
        return self._tilt

    @property
    def zoom(self) -> int:
        return self._zoom

    @property
    def status(self) -> PTZStatus:
        """Return the current camera state as a PTZStatus dataclass."""
        return PTZStatus(
            available=self.available,
            device=self.device,
            pan=self._pan,
            tilt=self._tilt,
            zoom=self._zoom,
            pan_range=self.pan_range,
            tilt_range=self.tilt_range,
            zoom_range=self.zoom_range,
        )

    def set_pan(self, value: int) -> bool:
        """
        Set the pan position. Value is clamped to the camera's valid range.
        Units are camera-specific (typically arc-seconds × 100 for most PTZ cameras).

        Returns True on success, False if the camera is unavailable or the
        control failed.
        """
        if not self.available:
            return False
        value = self.pan_range.clamp(value)
        if self._set_ctrl("pan_absolute", value):
            self._pan = value
            return True
        return False

    def set_tilt(self, value: int) -> bool:
        """
        Set the tilt position. Value is clamped to the camera's valid range.

        Returns True on success, False if the camera is unavailable or the
        control failed.
        """
        if not self.available:
            return False
        value = self.tilt_range.clamp(value)
        if self._set_ctrl("tilt_absolute", value):
            self._tilt = value
            return True
        return False

    def set_zoom(self, value: int) -> bool:
        """
        Set the zoom level. Value is clamped to the camera's valid range.

        Returns True on success, False if the camera is unavailable or the
        control failed.
        """
        if not self.available:
            return False
        value = self.zoom_range.clamp(value)
        if self._set_ctrl("zoom_absolute", value):
            self._zoom = value
            return True
        return False

    def set_ptz(self, pan: int, tilt: int, zoom: int) -> bool:
        """
        Set pan, tilt, and zoom in one call. All values are clamped.

        Returns True only if all three controls succeeded.
        """
        ok_pan  = self.set_pan(pan)
        ok_tilt = self.set_tilt(tilt)
        ok_zoom = self.set_zoom(zoom)
        return ok_pan and ok_tilt and ok_zoom

    def reset(self) -> bool:
        """Move to center position (pan=0, tilt=0) at minimum zoom."""
        return self.set_ptz(0, 0, self.zoom_range.min)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _check_availability(self) -> None:
        """Use v4l2-ctl --info to verify the device is reachable."""
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", self.device, "--info"],
                capture_output=True, text=True, timeout=2,
            )
            self.available = (result.returncode == 0)
        except FileNotFoundError:
            # v4l2-ctl is not installed
            self.available = False
        except Exception:
            self.available = False

    def _detect_ranges(self) -> None:
        """
        Parse v4l2-ctl --list-ctrls to find valid ranges for pan, tilt, zoom.

        The output looks like:
            pan_absolute 0x009a0908 (int) : min=-522000 max=522000 step=3600 default=0 value=0
            tilt_absolute 0x009a0909 (int) : min=-270000 max=270000 step=3600 default=0 value=0
            zoom_absolute 0x009a090d (int) : min=100 max=400 step=10 default=100 value=100

        We parse min, max, step, and current value for each control we care
        about and store them in ControlRange instances.
        """
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", self.device, "--list-ctrls"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return

            for line in result.stdout.splitlines():
                line = line.strip()

                if "pan_absolute" in line:
                    parsed = _parse_v4l2_ctrl_line(line)
                    if parsed:
                        self.pan_range = parsed
                        self._pan = parsed.current

                elif "tilt_absolute" in line:
                    parsed = _parse_v4l2_ctrl_line(line)
                    if parsed:
                        self.tilt_range = parsed
                        self._tilt = parsed.current

                elif "zoom_absolute" in line:
                    parsed = _parse_v4l2_ctrl_line(line)
                    if parsed:
                        self.zoom_range = parsed
                        self._zoom = parsed.current

        except Exception:
            # If detection fails for any reason, we keep the defaults.
            # This is a deliberate silent failure — PTZ detection failing
            # should not crash the application.
            pass

    def _set_ctrl(self, control_name: str, value: int) -> bool:
        """
        Run v4l2-ctl --set-ctrl to apply a single control value.
        Returns True on success, False on any failure.
        """
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", self.device,
                 "--set-ctrl", f"{control_name}={value}"],
                capture_output=True, text=True, timeout=1,
            )
            return result.returncode == 0
        except Exception:
            return False


def _parse_v4l2_ctrl_line(line: str) -> Optional[ControlRange]:
    """
    Parse a single line of v4l2-ctl --list-ctrls output into a ControlRange.

    The format is:
        <name> <hex_id> (<type>) : min=<N> max=<N> step=<N> default=<N> value=<N>

    We only need min, max, step, and the current value. Returns None if
    any required field is missing or unparseable.
    """
    fields: Dict[str, int] = {}
    for token in line.split():
        for key in ("min", "max", "step", "value"):
            if token.startswith(f"{key}="):
                try:
                    fields[key] = int(token.split("=", 1)[1])
                except ValueError:
                    pass

    required = ("min", "max", "step", "value")
    if not all(k in fields for k in required):
        return None

    return ControlRange(
        min=fields["min"],
        max=fields["max"],
        step=fields["step"],
        current=fields["value"],
    )
