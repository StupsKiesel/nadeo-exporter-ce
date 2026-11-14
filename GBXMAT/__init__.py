"""GBXMAT — Trackmania material/shader builder for Blender.

A small, self-contained module that turns a :class:`MaterialSpec` (plain
dataclass) into a fully-wired ``bpy.types.Material`` datablock. Designed
to be reusable by *any* community Blender project that imports Trackmania
content, not just this addon.

Public API
----------
* :func:`get_or_create_material` — main entry point. Accepts a
  :class:`MaterialSpec`, returns a (cached) ``bpy.types.Material``.
* :func:`register_family` — register a custom shader-family builder by
  name. Lets downstream projects add or override TM-style shaders
  without forking GBXMAT.
* :func:`list_supported_families` — names of all currently-registered
  shader families.
* :func:`set_max_texture_size` — clamp the longest dimension of any
  loaded texture (0 = original size). Useful for "lite" import modes.
* :func:`configure_logging` — standalone logging setup helper. The
  Blender addon should configure the ``gbxmat`` logger itself instead.

Boundaries
----------
* No imports from any project-specific package (GBXBPY, addons, NICE).
* Only filesystem touch is ``Pillow`` decoding DDS bytes and
  ``bpy.data.images.new`` / ``bpy.data.images.load``.
* Caller is responsible for resolving GameData-relative paths into
  absolute paths on disk before constructing a :class:`MaterialSpec`.
"""

from __future__ import annotations

import logging

LOGGER_NAME = "gbxmat"

log = logging.getLogger(LOGGER_NAME)
log.addHandler(logging.NullHandler())


def configure_logging(
    level: int = logging.WARNING,
    *,
    handler: logging.Handler | None = None,
    format_string: str = "[%(name)s] %(levelname)s: %(message)s",
) -> logging.Logger:
    """Attach a stream handler to the ``gbxmat`` logger root.

    Intended for standalone use (debug scripts, tests). The Blender
    addon should configure the ``gbxmat`` logger itself instead of
    calling this helper.
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


from .spec import MaterialSpec, TextureRef, TextureRole  # noqa: E402
from .registry import (  # noqa: E402
    get_or_create_material,
    register_family,
    list_supported_families,
)
from .images import set_max_texture_size  # noqa: E402

__all__ = [
    "LOGGER_NAME",
    "configure_logging",
    "MaterialSpec",
    "TextureRef",
    "TextureRole",
    "get_or_create_material",
    "register_family",
    "list_supported_families",
    "set_max_texture_size",
]

__version__ = "0.1.0"
