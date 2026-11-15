"""Listing of every file the game can extract, with its size.

The Openplanet plugin walks the game's ``GameData`` fids (including the
files inside ``.pak`` archives) and uploads ``path<TAB>size`` lines. The
index lets lookups see files that were never extracted, and a disk size
that differs from the game's size marks a broken extract.

Paths are GameData-relative with ``/`` separators; lookups are
case-insensitive like the game's own file system.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

INDEX_FOLDER = ".bridge"
INDEX_FILE = "game_index.json"
UNAVAILABLE_FILE = "unavailable.json"


def _unavailable_path(library_dir: str) -> str:
    return os.path.join(library_dir, INDEX_FOLDER, UNAVAILABLE_FILE)


def load_unavailable(library_dir: str, game_version: str) -> set[str]:
    """Lowercased paths the game reported as not extractable for ``game_version``."""
    try:
        with open(_unavailable_path(library_dir), encoding="utf-8") as handle:
            data = json.load(handle)
        if str(data.get("game_version", "")) != game_version:
            return set()
        return {str(rel).lower() for rel in data.get("files", ())}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def save_unavailable(library_dir: str, game_version: str, rel_paths: Iterable[str]) -> None:
    """Add ``rel_paths`` to the unavailable list; a new game version starts a new list."""
    files = load_unavailable(library_dir, game_version) | {normalize(rel).lower() for rel in rel_paths}
    target = _unavailable_path(library_dir)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target + ".tmp", "w", encoding="utf-8") as handle:
        json.dump({"game_version": game_version, "files": sorted(files)}, handle, separators=(",", ":"))
    os.replace(target + ".tmp", target)


def normalize(rel_path: str) -> str:
    """GameData-relative path with ``/`` separators and no ``GameData/`` prefix."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    if parts and parts[0].lower() == "gamedata":
        parts = parts[1:]
    return "/".join(parts)


class GameIndex:
    """Case-insensitive file and folder tree of the game's GameData."""

    def __init__(self, entries: Iterable[tuple[str, int]] = (), *, game_version: str = "",
                 created: float = 0.0) -> None:
        self.game_version = game_version
        self.created = created or time.time()
        self._files: dict[str, tuple[str, int]] = {}
        # lowercased folder -> ({lower dir name: name}, {lower file name: name})
        self._folders: dict[str, tuple[dict[str, str], dict[str, str]]] = {"": ({}, {})}
        for rel, size in entries:
            self.add(rel, size)

    def __len__(self) -> int:
        return len(self._files)

    def add(self, rel_path: str, size: int) -> None:
        rel = normalize(rel_path)
        # "Name.Gbx?394" entries are empty aliases of "Name.Gbx"; the game
        # extracts them as "Name.Gbx_394", which no import ever references.
        if not rel or "?" in rel:
            return
        self._files[rel.lower()] = (rel, int(size))
        parts = rel.split("/")
        folder = ""
        for depth, name in enumerate(parts):
            children = self._folders.setdefault(folder, ({}, {}))
            if depth == len(parts) - 1:
                children[1].setdefault(name.lower(), name)
            else:
                children[0].setdefault(name.lower(), name)
                folder = f"{folder}/{name.lower()}" if folder else name.lower()

    def size(self, rel_path: str) -> Optional[int]:
        hit = self._files.get(normalize(rel_path).lower())
        return hit[1] if hit else None

    def has_file(self, rel_path: str) -> bool:
        return normalize(rel_path).lower() in self._files

    def has_folder(self, rel_path: str) -> bool:
        return normalize(rel_path).lower() in self._folders

    def children(self, rel_path: str) -> tuple[dict[str, str], dict[str, str]]:
        """``({lower: dir name}, {lower: file name})`` directly below ``rel_path``."""
        return self._folders.get(normalize(rel_path).lower(), ({}, {}))

    def files(self) -> list[tuple[str, int]]:
        return list(self._files.values())

    # -- persistence -------------------------------------------------------

    @staticmethod
    def path_for(library_dir: str) -> str:
        return os.path.join(library_dir, INDEX_FOLDER, INDEX_FILE)

    def save(self, library_dir: str) -> str:
        target = self.path_for(library_dir)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temp = target + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({
                "game_version": self.game_version,
                "created": self.created,
                "files": [[rel, size] for rel, size in self._files.values()],
            }, handle, separators=(",", ":"))
        os.replace(temp, target)
        return target

    @classmethod
    def load(cls, library_dir: str) -> Optional["GameIndex"]:
        path = cls.path_for(library_dir)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return cls(
                ((rel, size) for rel, size in data.get("files", ())),
                game_version=str(data.get("game_version", "")),
                created=float(data.get("created", 0.0)),
            )
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def parse_lines(text: str, unavailable: Optional[list[str]] = None) -> list[tuple[str, int]]:
        """``path<TAB>size[<TAB>flags]`` lines as uploaded by the plugin.

        Paths flagged ``nodata`` are also appended to ``unavailable``.
        """
        out = []
        for line in text.splitlines():
            path, tab, rest = line.strip("\r").partition("\t")
            if not tab or not path.strip():
                continue
            size, _tab, flags = rest.partition("\t")
            try:
                out.append((path.strip(), int(size or 0)))
            except ValueError:
                continue
            if unavailable is not None and "nodata" in flags.strip().split(","):
                unavailable.append(path.strip())
        return out
