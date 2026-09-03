"""Shared test helpers. Kept free of psychopy so the pure tests run headless in CI."""

from __future__ import annotations

import importlib

import pytest


def import_or_skip(name: str, *, reason: str):
    """Import ``name`` or skip the calling test module.

    ``pytest.importorskip`` only skips when the module is *missing*. On the CI
    runner psychopy is installed but cannot be imported: pyglet wants libGLU and
    an X display, wx wants GTK, and those surface as ImportError,
    AttributeError or NoSuchDisplayException depending on which library gives
    up first. Anything that goes wrong importing a display-bound module means
    "not this machine", so skip on any exception.
    """
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - see docstring
        pytest.skip(f"{reason} ({type(exc).__name__}: {exc})", allow_module_level=True)
