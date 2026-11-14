"""DynaObject - data-only gbx type (not instanced into maps directly)."""

from __future__ import annotations

from . import data


def register() -> None:
    data.register()


def unregister() -> None:
    data.unregister()

