"""Block skin packs (``CGameCtnBlockSkin`` pack descriptors).

A placed block's skin names a file relative to the game's ``Skins`` root,
e.g. ``Skins\\Any\\Advertisement4x1\\Blue.zip``. Stock skins are not part of
GameData; they ship inside the game's ``Packs\\*.zip`` archives. Custom skins
live in the user's ``Documents\\Trackmania\\Skins`` folder.

A pack is one of:

* a ZIP with a ``Skin.json`` (``"ClassId": "Parallax"``) listing image
  layers with a ``Depth`` and optional ``HasVerticalTiling``;
* a folder holding the same ``Skin.json`` (stock folders may reference
  layers that are absent, which makes them unresolvable);
* a single image (``.dds``/``.tga``/...) or movie (``.webm``).

:class:`SkinLibrary` locates the pack, parses ``Skin.json`` (which is not
strict JSON: ``"Layers" = [ { Name="PZ1.dds" Depth="1" } ]``) and extracts
the needed files into a cache directory, because Blender can only load
images and movies from disk.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import logging
import os
import re
import zipfile
from typing import Optional, Sequence

log = logging.getLogger("gbxbpy.resolve.skins")

IMAGE_EXTENSIONS = (".dds", ".tga", ".png", ".jpg", ".jpeg", ".webp")
MOVIE_EXTENSIONS = (".webm", ".mp4", ".ogv", ".avi", ".mkv")


@dataclass(frozen=True, slots=True)
class SkinLayer:
    path: str                     # absolute path of the extracted file
    depth: float = 1.0            # Skin.json depth in metres behind the screen surface
    vertical_tiling: bool = True
    is_movie: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedSkin:
    source: str                   # pack path as stored in the map
    class_id: str                 # "Parallax", "Image" or "Movie"
    layers: tuple[SkinLayer, ...]


_LAYER_BLOCK = re.compile(r"\{([^{}]*)\}")
_ATTRIBUTE = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")
_CLASS_ID = re.compile(r"\"?ClassId\"?\s*[:=]\s*\"(\w+)\"")


def parse_skin_json(text: str) -> tuple[str, list[dict[str, str]]]:
    """Return ``(class_id, layers)`` from Nadeo's relaxed ``Skin.json`` syntax."""
    match = _CLASS_ID.search(text)
    class_id = match.group(1) if match else ""
    start = text.find("[")
    body = text[start:] if start >= 0 else ""
    layers = []
    for block in _LAYER_BLOCK.findall(body):
        attrs = dict(_ATTRIBUTE.findall(block))
        if attrs.get("Name"):
            layers.append(attrs)
    return class_id, layers


_BLEND_SUFFIX = re.compile(r"\+([01])([01])([01])([AY])$", re.IGNORECASE)


def layer_blend(path: str) -> tuple[tuple[float, float, float] | None, bool]:
    """Return ``(fill_rgb, alpha_from_luma)`` from a ``Name+RGBA``/``Name+RGBY`` suffix.

    Stock overlay movies encode their compositing in the file name:
    ``Left+111A`` is white shaped by the movie alpha, ``Bottom+000A`` a
    black fade, ``EffectRain+111Y`` white shaped by the (alpha-less) movie
    luminance. Names without a suffix use their own colour and alpha.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    match = _BLEND_SUFFIX.search(stem)
    if match is None:
        return None, False
    fill = tuple(float(match.group(i)) for i in (1, 2, 3))
    return fill, match.group(4).upper() == "Y"


def _truthy(value: str) -> bool:
    return value.strip().casefold() in ("1", "true", "yes")


def _key(path: str) -> str:
    return path.replace("\\", "/").strip("/").casefold()


def default_cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "TrackmaniaBlenderBridge", "SkinCache")


def default_packs_dirs() -> list[str]:
    """Known install locations of the game's ``Packs`` folder that exist."""
    candidates = [
        r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Trackmania\Packs",
        r"C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Trackmania\Packs",
        r"C:\Program Files (x86)\Steam\steamapps\common\Trackmania\Packs",
        r"C:\Program Files\Epic Games\TrackmaniaNext\Packs",
    ]
    return [c for c in candidates if os.path.isdir(c)]


def default_user_dirs() -> list[str]:
    """User folders that contain a ``Skins`` directory (custom skins)."""
    path = os.path.join(os.path.expanduser("~"), "Documents", "Trackmania")
    return [path] if os.path.isdir(path) else []


class SkinLibrary:
    """Locate, parse and extract skin packs for one import."""

    def __init__(
        self,
        packs_dirs: Sequence[str] = (),
        user_dirs: Sequence[str] = (),
        cache_dir: str = "",
        locators=None,
    ) -> None:
        self.packs_dirs = [d for d in packs_dirs if d and os.path.isdir(d)]
        self.user_dirs = [d for d in user_dirs if d and os.path.isdir(d)]
        self.cache_dir = cache_dir or default_cache_dir()
        # Optional GBXBPY.resolve.locators.LocatorCache: downloads files that
        # are only available through their map locator URL.
        self.locators = locators
        self.problems: dict[str, str] = {}
        self._archives: Optional[list[tuple[str, dict[str, str]]]] = None
        self._resolved: dict[str, Optional[ResolvedSkin]] = {}

    # -- lookup ------------------------------------------------------------

    def _archive_index(self) -> list[tuple[str, dict[str, str]]]:
        """``(zip path, {casefolded entry: real entry})`` for every pack ZIP."""
        if self._archives is None:
            self._archives = []
            for folder in self.packs_dirs:
                for name in sorted(os.listdir(folder), key=str.casefold):
                    if not name.casefold().endswith(".zip"):
                        continue
                    path = os.path.join(folder, name)
                    try:
                        with zipfile.ZipFile(path) as archive:
                            entries = {_key(n): n for n in archive.namelist()}
                    except (OSError, zipfile.BadZipFile) as exc:
                        log.debug("skin archive %s unreadable: %s", path, exc)
                        continue
                    self._archives.append((path, entries))
        return self._archives

    def _locate(self, key: str) -> Optional[tuple[str, str]]:
        """Return ``(container, member)``: a loose file has an empty member."""
        for root in self.user_dirs:
            path = os.path.join(root, *key.split("/"))
            match = _case_insensitive(path)
            if match:
                return match, ""
        for archive, entries in self._archive_index():
            if key in entries:
                return archive, entries[key]
        return None

    def _folder_members(self, key: str) -> Optional[tuple[str, dict[str, str]]]:
        """Return ``(container, {basename: member})`` for a pack folder."""
        prefix = key.rstrip("/") + "/"
        for root in self.user_dirs:
            path = _case_insensitive(os.path.join(root, *key.split("/")))
            if path and os.path.isdir(path):
                return path, {n.casefold(): os.path.join(path, n) for n in os.listdir(path)}
        for archive, entries in self._archive_index():
            members = {k[len(prefix):]: v for k, v in entries.items()
                       if k.startswith(prefix) and "/" not in k[len(prefix):]}
            if members:
                return archive, members
        return None

    # -- extraction --------------------------------------------------------

    def _extract(self, container: str, member: str, data: Optional[bytes] = None) -> str:
        """Write ``member`` to the cache and return its absolute path."""
        if not member and data is None:
            return container  # loose file on disk
        digest = hashlib.sha1(f"{container}|{member}".casefold().encode("utf-8")).hexdigest()[:16]
        target = os.path.join(self.cache_dir, digest, os.path.basename(member.replace("\\", "/")))
        if data is None:
            with zipfile.ZipFile(container) as archive:
                info = archive.getinfo(member)
                if os.path.isfile(target) and os.path.getsize(target) == info.file_size:
                    return target
                data = archive.read(member)
        elif os.path.isfile(target) and os.path.getsize(target) == len(data):
            return target
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        return target

    def _read(self, container: str, member: str) -> bytes:
        if not member:
            with open(container, "rb") as handle:
                return handle.read()
        with zipfile.ZipFile(container) as archive:
            return archive.read(member)

    # -- public API --------------------------------------------------------

    def resolve(self, pack_path: str, url: str = "") -> Optional[ResolvedSkin]:
        """Resolve a map pack path, or ``None`` (reason in :attr:`problems`).

        ``url`` is the pack's locator URL; it is downloaded (when a locator
        cache is set) if the file is in neither the user nor the Packs folders.
        """
        if not pack_path:
            return None
        key = _key(pack_path)
        if key not in self._resolved:
            try:
                self._resolved[key] = self._resolve(pack_path, key, url)
            except (OSError, zipfile.BadZipFile, KeyError) as exc:
                self.problems[pack_path] = f"unreadable skin pack: {exc}"
                self._resolved[key] = None
        return self._resolved[key]

    def locate_file(self, pack_path: str, url: str = "") -> Optional[str]:
        """Disk path of a pack file (extracted from a Packs ZIP or downloaded)."""
        if not pack_path and not url:
            return None
        key = _key(pack_path or url.rsplit("/", 1)[-1])
        try:
            location = self._locate(key) if pack_path else None
            location = location or self._download(pack_path, url)
            if location is None:
                self.problems.setdefault(pack_path or url, self._missing_reason(url))
                return None
            return self._extract(*location)
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            self.problems[pack_path or url] = f"unreadable pack file: {exc}"
            return None

    def vehicle_files(self, path: str) -> tuple[dict[str, str], tuple[str, ...]]:
        """Extract vehicle GBX/texture/JSON resources, never auxiliary executables."""
        files = {}
        with zipfile.ZipFile(path) as archive:
            members = tuple(archive.namelist())
            seen = set()
            for member in members:
                parts = member.replace("\\", "/").split("/")
                if (member.startswith(("/", "\\")) or ":" in member
                        or any(part in (".", "..") for part in parts)):
                    raise ValueError(f"Unsafe vehicle skin archive entry: {member}")
                if member.endswith(("/", "\\")):
                    continue
                key = _key(member)
                if key in seen:
                    raise ValueError(f"Duplicate vehicle skin archive entry: {member}")
                seen.add(key)
                if os.path.splitext(key)[1] in IMAGE_EXTENSIONS + (".gbx", ".json"):
                    data = archive.read(member)
                    identity = f"{path}|{hashlib.sha256(data).hexdigest()}"
                    files[key] = self._extract(identity, member, data)
        return files, members

    def _download(self, pack_path: str, url: str) -> Optional[tuple[str, str]]:
        if not url or self.locators is None:
            return None
        name = os.path.basename(pack_path.replace("\\", "/")) if pack_path else ""
        path = self.locators.fetch(url, name)
        return (path, "") if path else None

    def _missing_reason(self, url: str) -> str:
        reason = "not found in Packs or user Skins folders"
        if url and self.locators is not None:
            return f"{reason}; {self.locators.problems.get(url, 'download failed')}"
        if url:
            return f"{reason}; locator downloads are disabled ({url})"
        return reason

    def _resolve(self, pack_path: str, key: str, url: str = "") -> Optional[ResolvedSkin]:
        ext = os.path.splitext(key)[1]
        location = self._locate(key) or self._download(pack_path, url)
        if ext in IMAGE_EXTENSIONS + MOVIE_EXTENSIONS:
            if location is None:
                self.problems[pack_path] = f"skin file {self._missing_reason(url)}"
                return None
            is_movie = ext in MOVIE_EXTENSIONS
            path = self._extract(*location)
            return ResolvedSkin(pack_path, "Movie" if is_movie else "Image",
                                (SkinLayer(path, is_movie=is_movie),))
        if ext == ".zip" and location is not None:
            data = self._read(*location)
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                members = {_key(n): n for n in inner.namelist()}
                if members:
                    container = f"{location[0]}|{location[1]}"
                    return self._from_members(
                        pack_path, container, members,
                        lambda member: inner.read(member),
                    )
        # Stock "Stunt*.zip" packs are empty; the skin is a sibling folder.
        folder_key = key[:-4] if ext == ".zip" else key
        folder = self._folder_members(folder_key)
        if folder is None:
            self.problems[pack_path] = f"skin pack {self._missing_reason(url)}"
            return None
        container, members = folder
        if os.path.isdir(container):
            return self._from_members(pack_path, container, members, None)
        with zipfile.ZipFile(container) as archive:
            return self._from_members(
                pack_path, container, members, lambda member: archive.read(member),
            )

    def _from_members(self, pack_path, container, members, read) -> Optional[ResolvedSkin]:
        """Build a skin from pack members; ``read=None`` means loose files."""
        def load(member: str) -> bytes:
            if read is None:
                with open(member, "rb") as handle:
                    return handle.read()
            return read(member)

        def materialize(member: str) -> str:
            return member if read is None else self._extract(container, member, read(member))

        manifest = members.get("skin.json")
        if manifest is None:
            images = sorted(k for k in members if k.endswith(IMAGE_EXTENSIONS))
            if not images:
                self.problems[pack_path] = "skin pack has neither Skin.json nor images"
                return None
            return ResolvedSkin(pack_path, "Image", (SkinLayer(materialize(members[images[0]])),))
        class_id, entries = parse_skin_json(load(manifest).decode("utf-8", "replace"))
        layers = []
        for entry in entries:
            member = members.get(_key(entry["Name"]))
            if member is None:
                self.problems[pack_path] = f"Skin.json layer {entry['Name']} is missing"
                return None
            try:
                depth = float(entry.get("Depth", "1") or 1.0)
            except ValueError:
                depth = 1.0
            path = materialize(member)
            layers.append(SkinLayer(
                path, depth, _truthy(entry.get("HasVerticalTiling", "false")),
                os.path.splitext(member)[1].casefold() in MOVIE_EXTENSIONS,
            ))
        if not layers:
            self.problems[pack_path] = "Skin.json lists no layers"
            return None
        return ResolvedSkin(pack_path, class_id or "Parallax", tuple(layers))


def _case_insensitive(path: str) -> Optional[str]:
    """Return ``path`` with on-disk casing, or ``None`` when absent."""
    if os.path.exists(path):
        return path
    parent, name = os.path.split(path)
    if not name or parent == path:
        return None
    real_parent = _case_insensitive(parent)
    if real_parent is None or not os.path.isdir(real_parent):
        return None
    for entry in os.listdir(real_parent):
        if entry.casefold() == name.casefold():
            return os.path.join(real_parent, entry)
    return None


__all__ = [
    "SkinLayer", "ResolvedSkin", "SkinLibrary", "parse_skin_json",
    "default_cache_dir", "default_packs_dirs", "default_user_dirs",
]
