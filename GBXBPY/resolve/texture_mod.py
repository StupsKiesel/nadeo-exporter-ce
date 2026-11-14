"""Map texture mods (``CGameCtnChallenge`` chunk ``0x03043019``).

A texture mod is a ZIP (usually ``Skins\\<Environment>\\Mod\\<Name>.zip``)
whose files replace game textures of the same name while the map is played.
Its layout mirrors a collection's ``Media`` folder with the ``Texture*``
level dropped, for example::

    Image/CustomModOpaque_D.dds  -> <Env>/Media/Texture_BlockCustom/Image/CustomModOpaque_D.dds
    Image/PlatformTech_D.dds     -> <Env>/Media/Texture/Image/PlatformTech_D.dds
    Moods/Night/Moon.tga         -> <Env>/Media/Moods/Night/Moon.tga

:meth:`TextureMod.map_path` turns a game texture path into the mod's
replacement (extracted into a cache folder) or returns it unchanged.
Files are matched by Media-relative path first, then by the ``Image/<file>``
convention most mods use, ignoring the file extension so a mod may ship a
``.dds`` for a stock ``.tga``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import zipfile
from typing import Optional

log = logging.getLogger("gbxbpy.resolve.texture_mod")

_MEDIA = "/media/"


def _stem_key(path: str) -> str:
    path = path.replace("\\", "/").strip("/").casefold()
    if path.endswith(".texture.gbx"):
        return path[:-len(".texture.gbx")]
    stem, _ext = os.path.splitext(path)
    return stem


def candidate_keys(path: str) -> list[str]:
    """Mod lookup keys (casefolded, extensionless) for a game texture path."""
    norm = path.replace("\\", "/").casefold()
    index = norm.rfind(_MEDIA)
    if index < 0:
        return []
    parts = [p for p in norm[index + len(_MEDIA):].split("/") if p]
    if not parts:
        return []
    keys = ["/".join(parts)]
    if len(parts) > 1 and parts[0].startswith("texture"):
        keys.append("/".join(parts[1:]))
    keys.append("image/" + parts[-1])
    result: list[str] = []
    for key in keys:
        stem = _stem_key(key)
        if stem not in result:
            result.append(stem)
    return result


class TextureMod:
    """Index of one texture mod ZIP; replacements are extracted on demand."""

    def __init__(self, archive_path: str, cache_dir: str, source: str = "") -> None:
        self.archive_path = archive_path
        self.source = source or os.path.basename(archive_path)
        digest = hashlib.sha1(os.path.abspath(archive_path).casefold().encode("utf-8")).hexdigest()[:16]
        self.cache_dir = os.path.join(cache_dir, digest)
        self._entries: dict[str, str] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                self._entries.setdefault(_stem_key(info.filename), info.filename)
        self._mapped: dict[str, str] = {}
        self.used: set[str] = set()
        self._lock = threading.Lock()

    @property
    def files(self) -> list[str]:
        return sorted(self._entries.values(), key=str.casefold)

    def member_for(self, path: str) -> Optional[str]:
        for key in candidate_keys(path):
            member = self._entries.get(key)
            if member is not None:
                return member
        return None

    def map_path(self, path: str) -> str:
        """Return the mod's replacement for ``path``, or ``path`` itself."""
        if not path:
            return path
        with self._lock:
            cached = self._mapped.get(path)
            if cached is not None:
                return cached
            result = path
            if not os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(self.cache_dir)):
                member = self.member_for(path)
                if member is not None:
                    try:
                        result = self._extract(member)
                        self.used.add(member)
                        log.debug("Texture mod %s replaces %s", member, path)
                    except (OSError, zipfile.BadZipFile, KeyError) as exc:
                        log.warning("Texture mod member %s unreadable: %s", member, exc)
            self._mapped[path] = result
            return result

    def _extract(self, member: str) -> str:
        parts = [p for p in member.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        target = os.path.join(self.cache_dir, *parts)
        with zipfile.ZipFile(self.archive_path) as archive:
            info = archive.getinfo(member)
            if os.path.isfile(target) and os.path.getsize(target) == info.file_size:
                return target
            os.makedirs(os.path.dirname(target), exist_ok=True)
            partial = target + ".part"
            with archive.open(info) as src, open(partial, "wb") as out:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
            os.replace(partial, target)
        return target


def load_texture_mod(pack_path: str, url: str, skins, cache_dir: str) -> tuple[Optional[TextureMod], str]:
    """Locate (or download) and index a map's texture mod.

    ``skins`` is a :class:`GBXBPY.resolve.skins.SkinLibrary`, which searches
    the user and Packs folders and downloads locators. Returns
    ``(mod, problem)``; ``mod`` is ``None`` when unavailable.
    """
    archive = skins.locate_file(pack_path, url) if skins is not None else None
    if archive is None:
        reason = skins.problems.get(pack_path or url, "not found") if skins is not None else "no skin library"
        return None, f"Texture mod {pack_path or url} unavailable: {reason}"
    try:
        mod = TextureMod(archive, cache_dir, pack_path or url)
    except (OSError, zipfile.BadZipFile) as exc:
        return None, f"Texture mod {archive} is not a readable ZIP: {exc}"
    if not mod.files:
        return None, f"Texture mod {archive} is empty"
    return mod, ""


__all__ = ["TextureMod", "candidate_keys", "load_texture_mod"]
