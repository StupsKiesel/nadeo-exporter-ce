"""Macroblock ? stub gbx type. Extend with type-specific fields as needed."""

from __future__ import annotations

from . import data, instance


def register() -> None:
    data.register()
    instance.register()


def unregister() -> None:
    instance.unregister()
    data.unregister()
