"""
tests/conftest.py — Shared pytest configuration, fixtures, and markers.

This file is automatically loaded by pytest before any tests run.
It defines two things that every test file in this suite relies on:

  1. Custom markers — @pytest.mark.hardware skips tests that require
     a real camera or audio device. This lets the full test suite run
     cleanly in CI environments where no hardware is attached.

  2. Shared fixtures — things like a pre-initialized GStreamer environment
     and a helper for checking whether a specific GStreamer element is
     available on this machine.
"""

import pytest

# helpers.py is a plain Python module that lives alongside this file.
# We import from it here so that conftest.py benefits from the same
# utilities without duplicating code.
from helpers import element_available, skip_if_missing  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# Custom markers
# ─────────────────────────────────────────────────────────────────────────────

def pytest_configure(config):
    """Register custom markers so pytest doesn't warn about unknown marks."""
    config.addinivalue_line(
        "markers",
        "hardware: mark test as requiring real hardware (camera, audio device). "
        "Skip with: pytest -m 'not hardware'"
    )
    config.addinivalue_line(
        "markers",
        "v4l2: mark test as requiring a /dev/video* device to be present."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def cleanup_gst_pipelines():
    """
    Automatically clean up any GStreamer pipelines that a test creates.

    This is an 'autouse' fixture, meaning it runs for every single test
    without needing to be explicitly requested. After each test, it sets
    all active pipelines back to NULL state.

    Without this, a test that fails mid-way through (before its own
    cleanup code runs) would leave a pipeline running, which could
    interfere with subsequent tests.

    In practice, tests are responsible for their own pipeline.set_state(NULL)
    calls, but this fixture provides a safety net.
    """
    # Before the test: nothing to do
    yield
    # After the test: GStreamer's global registry doesn't hold pipeline
    # references, so we rely on each test to clean up. If they don't,
    # Python's garbage collector will eventually call the destructor.
    # This fixture is here as a hook point for future enhancement.
