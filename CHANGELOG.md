# Changelog

All notable changes to streamcraft will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
streamcraft uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

_Nothing yet._

---

## [0.1.0] — 2026-03-30

Initial release. 🎉

### Added

#### `PipelineBuilder` (`pipeline.py`)
- Fluent builder for linear GStreamer pipelines: chain `.element()` and `.caps()` calls and get a fully linked `Gst.Pipeline` back from `.build()`.
- Automatic underscore-to-hyphen conversion for GStreamer property names — write `speed_preset="ultrafast"` instead of `speed-preset`.
- Named element support via the `name=` keyword argument; named elements are returned in the `elems` dict from `.build()` and are also accessible via `pipeline.get_by_name()`.
- `.caps()` method for inserting `capsfilter` elements between pipeline stages.
- Clear, actionable error messages when a plugin is missing, a property name is wrong, or two elements cannot be linked, including `gst-inspect-1.0` hints.

#### `require_elements` (`devices.py`)
- Checks that all requested GStreamer plugins are installed before any pipeline construction begins.
- Raises `EnvironmentError` listing every missing element and the exact `apt install` command needed to add them.

#### `check_v4l2_device` (`devices.py`)
- Verifies that a V4L2 device path exists, is readable, and can be opened.
- Returns a `(bool, str)` tuple: `(True, "")` on success or `(False, <human-readable reason>)` on failure, including permission hints (`sudo usermod -aG video $USER`).

#### `list_v4l2_devices` (`devices.py`)
- Returns a sorted list of all V4L2 device paths currently present on the system (e.g. `['/dev/video0', '/dev/video2']`).
- Returns an empty list when no devices are found; never raises.

#### `V4L2PTZCamera` (`devices.py`)
- Controls pan, tilt, and zoom on any V4L2 camera that exposes those controls via `v4l2-ctl`.
- Auto-detects the camera's valid min/max ranges at startup — no hardcoded per-model constants.
- All operations are silent no-ops returning `False` when no PTZ camera is connected, so application code does not need to guard against a missing camera.
- `set_pan()`, `set_tilt()`, `set_zoom()`, `reset()` control methods.
- `status` property returns a `PTZStatus` dataclass with the current position and detected ranges.
- `PTZStatus.to_dict()` for easy serialisation to JSON.
- `ControlRange` dataclass exposing `min` and `max` for each axis.

#### `WebRTCSession` (`webrtc.py`)
- Manages the full WebRTC signaling lifecycle on top of a GStreamer `webrtcbin` element: SDP offer/answer exchange and ICE candidate buffering.
- Handles the ICE candidate race condition where remote candidates arrive before the remote description is set — candidates are buffered and applied automatically once the description is available.
- GLib main loop → asyncio thread bridging built in; no manual thread synchronisation required.
- Framework-agnostic: `connect_ice_sender()` and `handle_message()` accept any `async` callable that sends a JSON-serialisable dict — compatible with aiohttp, FastAPI, Starlette, raw `websockets`, and others.
- `stop()` releases the GStreamer pipeline and all associated resources cleanly.

#### Public API (`__init__.py`)
- Exports: `PipelineBuilder`, `require_elements`, `check_v4l2_device`, `list_v4l2_devices`, `V4L2PTZCamera`, `PTZStatus`, `ControlRange`, `WebRTCSession`.
- `__version__ = "0.1.0"`.

#### Test suite (`tests/`)
- 93 tests across three modules: `test_pipeline.py` (37), `test_devices.py` (37), `test_webrtc.py` (19).
- Software-only tests use GStreamer's built-in test elements (`videotestsrc`, `audiotestsrc`, `fakesink`) and require no hardware.
- Hardware-dependent tests marked `@pytest.mark.hardware` and skipped by default.

#### Examples (`examples/`)
- `streaming_server.py`: A complete bidirectional video and audio streaming server for Raspberry Pi. Streams H.264 + Opus to a browser over WebRTC and receives the browser's camera and microphone back. Exposes PTZ camera controls through browser sliders. Serves over TLS (Tailscale HTTPS or self-signed certificate).
- `interface.html`: Browser UI served by the example server — no build step, no framework, plain HTML/CSS/JS.

#### Project infrastructure
- `pyproject.toml` with `setuptools` build backend, metadata, classifiers, and optional `aiohttp` dependency.
- MIT licence.
- GitHub Actions CI workflow (`.github/workflows/test.yml`) running the software-only test suite on push and pull request.

---

[Unreleased]: https://github.com/ranaweerasupun/streamcraft/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ranaweerasupun/streamcraft/releases/tag/v0.1.0
