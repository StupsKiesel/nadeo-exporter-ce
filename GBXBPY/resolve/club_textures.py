"""Opt-in public club media downloads for the game's decoration texture slots."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import urllib.request

from .locators import LocatorCache


# Stadium.Collection.Gbx's custom-deco Fids; these are shared by decoration,
# block and item materials, not arbitrary advertisements or map sign skins.
TEXTURE_FIELDS = {
    "decalsponsor1x1biga_d": ("decalUrl",),
    "decalsponsor4x1a_m": ("decalSponsor4x1Url",),
    "warprace155": ("screen64x41Url", "screen16x9Url"),
    "warprace8x1a": ("screen8x1Url",),
    "warprace16x1a": ("screen16x1Url",),
}


@dataclass
class ClubTextures:
    club_id: int
    name: str
    paths: dict[str, str]
    source: str
    used: set[str] = field(default_factory=set)

    def map_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").casefold()
        if "/media/texture/" not in "/" + normalized:
            return path
        stem = os.path.basename(normalized).split(".", 1)[0]
        replacement = self.paths.get(stem)
        if replacement:
            self.used.add(stem)
            return replacement
        return path


def load_club_textures(
    club_id: int, cache_dir: str, *, opener=None, progress=None,
) -> tuple[ClubTextures, list[str]]:
    """Fetch current public metadata and cache images; return media and problems."""
    if type(club_id) is not int or club_id <= 0:
        raise ValueError("Club ID must be a positive integer")
    source = f"https://trackmania.io/api/club/{club_id}"
    request = urllib.request.Request(source, headers={
        "User-Agent": "TrackmaniaBlenderBridge (club textures)", "Accept": "application/json",
    })
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=30) as response:
        if getattr(response, "status", 200) != 200:
            raise OSError(f"Club {club_id}: HTTP {response.status}")
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Club metadata exceeds 1 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] != club_id:
        raise ValueError("Club metadata does not match the requested ID")
    name = data.get("name")
    if not isinstance(name, str):
        raise ValueError("Club metadata has no valid name")
    cache = LocatorCache(cache_dir, max_bytes=32 * 1024 * 1024, opener=open_url)
    paths, problems = {}, []
    for index, (stem, fields) in enumerate(TEXTURE_FIELDS.items()):
        if progress is not None:
            progress(index / len(TEXTURE_FIELDS), f"Downloading club {club_id}: {fields[0]}")
        url = next((data.get(key) for key in fields if data.get(key)), "")
        if not isinstance(url, str):
            raise ValueError(f"Club metadata has an invalid {fields[0]}")
        if not url:
            continue
        target = cache.fetch(url, stem + ".png")
        if target is None:
            problems.append(f"Club {club_id} {fields[0]} unavailable: {cache.problems.get(url, 'not found')}")
        else:
            paths[stem] = target
    if not paths:
        problems.append(f"Club {club_id} has no downloadable decoration textures; stock textures retained")
    if progress is not None:
        progress(1, f"Club {club_id} textures ready")
    return ClubTextures(club_id, name, paths, source), problems
