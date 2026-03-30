"""
tests/helpers.py — Shared helper utilities for the streamcraft test suite.

This is a plain Python module (not a pytest conftest) so it can be imported
normally by any test file with: from helpers import skip_if_missing

The reason this exists separately from conftest.py is that pytest's conftest
system and Python's regular import system are two different things. conftest.py
is loaded automatically by pytest, but it can't be reliably imported with a
regular 'from conftest import ...' statement when pytest is run from the
project root. Plain modules like this one don't have that ambiguity.
"""

import pytest

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)


def element_available(factory_name: str) -> bool:
    """Return True if the named GStreamer element factory is installed."""
    return Gst.ElementFactory.find(factory_name) is not None


def skip_if_missing(*factory_names: str):
    """
    Return a pytest.mark.skipif decorator that skips a test if any of the
    named GStreamer elements are not installed on this machine.

    Usage:
        @skip_if_missing("x264enc", "h264parse")
        def test_h264_pipeline():
            ...
    """
    missing = [n for n in factory_names if not element_available(n)]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"GStreamer elements not installed: {', '.join(missing)}"
    )
