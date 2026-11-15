"""Resolve GameData-relative paths and external references to absolute paths.

Trackmania files reference each other through GameData-relative paths (e.g.
``Stadium\\Media\\Material\\PlatformIce.Material.Gbx``) and through external
ref tables that may contain ``..`` segments relative to the file being
parsed. :class:`Resolver` centralises the lookup logic so loaders never
have to reinvent it.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

log = logging.getLogger("gbxbpy.resolve.resolver")


class Resolver:
    """Resolves file paths against one or more GameData roots.

    Args:
        gamedata_dir: Path to a Trackmania GameData directory. May be
            empty when only file-relative resolution is needed.
        extra_roots: Additional roots to search after ``gamedata_dir``.

    The resolver is case-insensitive on Windows and case-sensitive on
    POSIX, matching the host filesystem.
    """

    def __init__(
        self,
        gamedata_dir: str = "",
        *,
        extra_roots: Optional[List[str]] = None,
    ) -> None:
        self.gamedata_dir = os.path.normpath(os.path.abspath(gamedata_dir)) if gamedata_dir else ""
        self._roots: List[str] = []
        if self.gamedata_dir:
            self._roots.append(self.gamedata_dir)
            # Kept even before Stadium is extracted; the library may fetch it on demand.
            self._roots.append(os.path.join(self.gamedata_dir, "Stadium"))
        for root in extra_roots or []:
            norm = os.path.normpath(os.path.abspath(root))
            if norm and norm not in self._roots:
                self._roots.append(norm)

    @property
    def roots(self) -> List[str]:
        """All search roots, in lookup order."""
        return list(self._roots)

    def resolve(self, ref_path: str, *, source_path: str = "",
                probe: bool = False) -> Optional[str]:
        """Resolve ``ref_path`` to an absolute file path, or ``None``.

        Args:
            ref_path: A relative path as it appears in a GBX file. May
                contain ``..`` segments and either separator style.
            source_path: Absolute path of the file the ref came from. Used
                to resolve ``..``-prefixed paths.
        """
        if not ref_path:
            return None
        from .. import library

        normalised = ref_path.replace("/", os.sep).replace("\\", os.sep)

        # Absolute paths are used as-is. Stripping their root would nest them
        # below GameData on POSIX ("GameData/home/user/.wine/.../GameData/...");
        # only the part after a ``GameData`` folder is retried below.
        if os.path.isabs(normalised):
            candidate = os.path.normpath(normalised)
            if (library.isfile if probe else library.ensure)(candidate):
                return candidate
            parts = candidate.split(os.sep)
            folded = [part.casefold() for part in parts]
            if "gamedata" not in folded:
                return None
            index = len(folded) - 1 - folded[::-1].index("gamedata")
            normalised = os.sep.join(parts[index + 1:])

        # 1) `..`-prefixed paths resolve against the source file's directory.
        if ".." in normalised and source_path:
            candidate = os.path.normpath(
                os.path.join(os.path.dirname(source_path), normalised)
            )
            if (library.isfile if probe else library.ensure)(candidate):
                return candidate

        # 2) Strip leading `..` and `.`, then search GameData roots.
        parts = normalised.split(os.sep)
        while parts and parts[0] in ("", ".", ".."):
            parts.pop(0)
        if not parts:
            return None
        clean = os.sep.join(parts)

        roots = self._roots
        if self.gamedata_dir and parts[0].casefold() == "stadium":
            stadium_root = os.path.normcase(os.path.join(self.gamedata_dir, "Stadium"))
            roots = [root for root in roots if os.path.normcase(root) != stadium_root]
        candidates = [os.path.normpath(os.path.join(root, clean)) for root in roots]
        # Files on disk or in the game index first; with an active library the
        # rest are requested in one batch, since the game's listing can miss files.
        if probe:
            return next((c for c in candidates if library.isfile(c)), None)
        return library.first_available(candidates)

    def to_gamedata_relative(self, abs_path: str) -> str:
        """Return ``abs_path`` expressed relative to GameData, or ``""``."""
        if not abs_path or not self.gamedata_dir:
            return ""
        norm = os.path.normpath(os.path.abspath(abs_path))
        gd = self.gamedata_dir + os.sep
        if norm.lower().startswith(gd.lower()):
            return norm[len(gd):]
        return ""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<Resolver gamedata={self.gamedata_dir!r} roots={len(self._roots)}>"


__all__ = ["Resolver"]
