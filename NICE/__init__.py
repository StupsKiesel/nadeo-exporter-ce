"""NICE — pure-Python GBX parser.

This package is the lowest layer of the 3-layer architecture (NICE →
GBXBPY → Blender Addon). It must remain free of any Blender (`bpy`) and
domain (`gbxbpy`) imports — see ``.claude/NICE-PARSER-BOUNDARY.md``.

Logging
-------
All diagnostic output goes through the ``nice`` logger hierarchy
(``nice.structs``, ``nice.construct``, ``nice.math``, ...). The package
attaches a :class:`logging.NullHandler` so applications that don't
configure logging see nothing, matching the standard library convention.

The Blender addon (or any host application) is expected to attach its
own handler, e.g. ``logging.getLogger("nice").setLevel(logging.WARNING)``
followed by ``addHandler(...)``.
"""

from __future__ import annotations

import logging

LOGGER_NAME: str = "nice"
"""Root name for every NICE-emitted log record."""

log: logging.Logger = logging.getLogger(LOGGER_NAME)
log.addHandler(logging.NullHandler())


def configure_logging(
    level: int | str = logging.WARNING,
    *,
    handler: logging.Handler | None = None,
    format_string: str = "[%(name)s] %(levelname)s: %(message)s",
) -> logging.Logger:
    """Configure the ``nice`` logger for standalone / test use.

    The Blender addon does not need to call this — it sets up its own
    handlers on the root ``nice`` logger. This helper exists so scripts
    in ``test and debug scripts/`` and the ``.venv310`` REPL get useful
    output without boilerplate.
    """
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    if handler is None:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(format_string))
    # Avoid stacking duplicate stream handlers on repeated calls.
    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.NullHandler)
        for h in root.handlers
    )
    if not has_stream:
        root.addHandler(handler)
    return root


__all__ = ["LOGGER_NAME", "log", "configure_logging"]
