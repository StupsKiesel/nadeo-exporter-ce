"""GBXBPY — domain/data layer built on top of NICE.

This package converts raw parsed GBX data (from NICE) into stable, easy-to-use
Python structures that the Blender addon can consume without understanding
parser internals.

Architecture rule: this package may import NICE, but must never import bpy.
See .claude/NICE-PARSER-BOUNDARY.md.

Coordinate convention: GBXBPY exposes all geometry, placement, and rotation
data in **Blender's axis convention** (right-handed, Z-up). The GBX → Blender
swizzle is applied once, inside loaders, via :mod:`GBXBPY.coords`. The addon
must not re-swizzle.

Public entry points:
    :func:`load`        Parse a .Gbx file from disk and return an Asset.
    :func:`load_bytes`  Parse a .Gbx blob from memory and return an Asset.
    :class:`Resolver`   GameData-aware path resolver.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

LOGGER_NAME = "gbxbpy"

log = logging.getLogger(LOGGER_NAME)
log.addHandler(logging.NullHandler())


def configure_logging(
    level: int = logging.WARNING,
    *,
    handler: logging.Handler | None = None,
    format_string: str = "[%(name)s] %(levelname)s: %(message)s",
) -> logging.Logger:
    """Attach a stream handler to the gbxbpy logger root.

    Intended for standalone use (debug scripts, tests). The Blender addon
    should configure the ``gbxbpy`` logger itself instead of calling this.
    """
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    if handler is None:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(format_string))
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in root.handlers
    )
    if not has_stream:
        root.addHandler(handler)
    return root


# Eagerly import the loader subpackage so all @register decorators fire.
from . import load as _load  # noqa: E402, F401
from .resolve import Resolver  # noqa: E402
from .mediatracker import (  # noqa: E402
    load_media_clip,
    load_map_media_clip_preview,
    load_map_media_trackers,
    write_media_clip,
)


def load(
    path: str,
    *,
    resolver: Optional[Resolver] = None,
    recursive: bool = True,
):
    """Parse a .Gbx file and return the matching :class:`~GBXBPY.model.Asset`.

    Args:
        path: Absolute or working-directory-relative path to a .Gbx file.
        resolver: Optional :class:`Resolver` for GameData-relative lookups.
        recursive: When True, NICE follows external references during parse.

    Raises:
        :class:`~GBXBPY.errors.ParseError` on read/parse failure.
        :class:`~GBXBPY.errors.UnsupportedClassError` when no loader is
        registered for the file's class id.
    """
    from .io import read_gbx
    from .registry import dispatch

    parsed = read_gbx(path, recursive=recursive)
    return dispatch(parsed, source_path=parsed.filepath, resolver=resolver)


def load_bytes(
    data: bytes,
    *,
    source_path: str = "",
    resolver: Optional[Resolver] = None,
    recursive: bool = True,
):
    """Parse a .Gbx blob from memory and return the matching :class:`Asset`."""
    from .io import read_gbx_bytes
    from .registry import dispatch

    parsed = read_gbx_bytes(data, source_path=source_path, recursive=recursive)
    return dispatch(parsed, source_path=source_path, resolver=resolver)


__all__ = [
    "LOGGER_NAME",
    "log",
    "configure_logging",
    "Resolver",
    "load",
    "load_bytes",
    "load_media_clip",
    "load_map_media_clip_preview",
    "load_map_media_trackers",
    "write_media_clip",
]
