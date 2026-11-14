"""Exception hierarchy for GBXBPY.

All errors raised by this package should derive from :class:`GbxbpyError`
so that the addon (and tools) can catch domain-layer failures with one
``except`` clause.
"""

from __future__ import annotations


class GbxbpyError(Exception):
    """Base class for all GBXBPY errors."""


class ParseError(GbxbpyError):
    """A NICE parse failed or returned unusable data for this kind of file."""


class ResolveError(GbxbpyError):
    """A referenced file or asset could not be located on disk."""


class UnsupportedClassError(GbxbpyError):
    """The GBX class id has no loader registered in GBXBPY."""


class NormalizationError(GbxbpyError):
    """A post-load normalization pass failed (inheritance, variant resolve...)."""


__all__ = [
    "GbxbpyError",
    "ParseError",
    "ResolveError",
    "UnsupportedClassError",
    "NormalizationError",
]
