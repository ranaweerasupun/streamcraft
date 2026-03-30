# Contributing to streamcraft

Thank you for your interest in contributing! streamcraft is a small, focused library — contributions that stay true to its design philosophy (sit on top, not underneath; fail fast with actionable messages; no framework lock-in) are especially welcome.

---

## Table of contents

- [Getting started](#getting-started)
- [Development setup](#development-setup)
- [Running the tests](#running-the-tests)
- [Submitting changes](#submitting-changes)
- [Coding style](#coding-style)
- [Design philosophy](#design-philosophy)
- [What makes a good contribution](#what-makes-a-good-contribution)
- [Reporting bugs](#reporting-bugs)
- [Requesting features](#requesting-features)

---

## Getting started

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/<your-username>/streamcraft.git
   cd streamcraft
   ```
3. **Create a branch** for your work:
   ```bash
   git checkout -b your-feature-or-fix
   ```

---

## Development setup

streamcraft depends on GStreamer's Python bindings, which must be installed via the system package manager before setting up the virtual environment.

**Step 1 — system dependencies (Debian / Ubuntu / Raspberry Pi OS):**

```bash
sudo apt update && sudo apt install -y \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools \
    gstreamer1.0-alsa \
    v4l-utils
```

**Step 2 — create a virtual environment with system-site-packages access:**

```bash
python -m venv --system-site-packages .venv
source .venv/bin/activate
```

The `--system-site-packages` flag is required so that `import gi` works inside the venv.

**Step 3 — install streamcraft in editable mode with all extras:**

```bash
pip install -e ".[aiohttp]"
```

---

## Running the tests

```bash
# Software-only tests (no hardware required, runs in CI)
pytest

# Including hardware tests (requires a V4L2 camera on /dev/video0)
pytest -m hardware
```

The test suite has 93 tests across three modules:

| Module | Tests | Description |
|---|---|---|
| `test_pipeline.py` | 37 | `PipelineBuilder` — construction, property mapping, error handling |
| `test_devices.py` | 37 | `require_elements`, `V4L2PTZCamera`, `check_v4l2_device` |
| `test_webrtc.py` | 19 | `WebRTCSession` — signaling, ICE buffering, lifecycle |

Hardware-dependent tests are marked `@pytest.mark.hardware` and skipped by default. The software-only tests use GStreamer's built-in test elements (`videotestsrc`, `audiotestsrc`, `fakesink`) and complete in under one second.

**Please make sure all existing tests pass before submitting a pull request.** If your change adds new functionality, add tests for it too.

---

## Submitting changes

1. **Commit** your changes with a clear, descriptive message:
   ```bash
   git commit -m "Add support for tee elements in PipelineBuilder"
   ```
2. **Push** to your fork:
   ```bash
   git push origin your-feature-or-fix
   ```
3. **Open a pull request** against the `main` branch of the upstream repository.

In your pull request description, please include:
- What the change does and why it is useful.
- Any GStreamer concepts or edge cases worth knowing about.
- Whether the change requires hardware to test (and if so, which hardware).

---

## Coding style

- Follow [PEP 8](https://peps.python.org/pep-0008/). Line length is 99 characters.
- Use descriptive variable names — GStreamer code already has a lot of implicit context; don't add to that with single-letter names.
- Public API functions and classes must have docstrings.
- Keep the public API surface in `__init__.py` up to date — anything exported there is a public commitment.
- GStreamer property names use hyphens; Python keyword arguments use underscores. The underscore-to-hyphen conversion is a documented feature of `PipelineBuilder` — preserve it.

---

## Design philosophy

Before contributing, please read the **Design philosophy** section in the README. In short:

- **Sit on top, not underneath.** streamcraft reduces boilerplate but does not hide GStreamer. Contributors should return real `Gst.*` objects wherever possible, not wrap them in custom types.
- **Linear pipelines only (in the builder).** `PipelineBuilder` deliberately handles only linear topologies. Do not add branching or dynamic pad support to the builder itself — the right answer is to return a `Gst.Pipeline` that users can extend with the standard API.
- **Fail fast with actionable messages.** Every error raised by streamcraft should tell the user exactly what is wrong and what to do about it. Vague `ValueError: bad input` messages are not acceptable.
- **No framework lock-in.** `WebRTCSession` accepts any async callable. Pull requests that hard-code aiohttp, FastAPI, or any other framework into the core library will not be merged. Examples can use specific frameworks; the library cannot.

---

## What makes a good contribution

**More likely to be merged:**
- Bug fixes with a regression test.
- Improvements to error messages that make them more actionable.
- New device utilities that follow the same pattern as `check_v4l2_device` and `list_v4l2_devices`.
- Documentation improvements and example corrections.
- Support for additional Linux distributions' package manager instructions (e.g. Fedora, Arch).

**Less likely to be merged without prior discussion:**
- Large new features — please open an issue first to discuss the design.
- Changes to `PipelineBuilder` that add non-linear topology support.
- Dependencies on packages that are not available in standard Linux distribution repositories.
- Changes to the public API that would break existing users.

When in doubt, open an issue before writing code.

---

## Reporting bugs

Please open a GitHub issue and include:

- Your OS and distribution version (`lsb_release -a`).
- Your Python version (`python --version`).
- The GStreamer version (`gst-launch-1.0 --version`).
- A minimal, self-contained code snippet that reproduces the problem.
- The full traceback or error output.

---

## Requesting features

Open a GitHub issue describing:

- The problem you are trying to solve (not just the solution you have in mind).
- How it fits with the library's existing design.
- Whether you are willing to implement it yourself.
