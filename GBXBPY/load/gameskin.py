"""Loader for ``CPlugGameSkin`` (GameSkin.Gbx)."""

from __future__ import annotations

import logging
from typing import Any

from ..model.gameskin import GameSkin, SkinFid
from ..registry import register
from ._gbx_helpers import body_dict, file_stem

log = logging.getLogger("gbxbpy.load.gameskin")

# Body chunks
_CHUNK_PAINTER    = 0x090F4003  # PainterTextureName + PainterSceneId (unversioned)
_CHUNK_VERSIONED  = 0x090F4004  # versioned: DirName + CustomizableFids (chunkl 0x004)
_CHUNK_VERSIONED2 = 0x090F4005  # older/alt versioned layout in NICE (relativeSkinDirectory + fids)


def skin_fids_chunk(parsed: Any) -> Any:
    """The fid-list chunk of a parsed GameSkin, or ``None``.

    The header repeats the ``0x090F4005`` layout under id ``0x090F4000``.
    Some Openplanet extracts scramble the body after the header, so the
    body chunk is missing and the header copy is the only readable list.
    """
    body = getattr(parsed, "body", None)
    if isinstance(body, dict):
        for chunk_id in (_CHUNK_VERSIONED, _CHUNK_VERSIONED2):
            chunk = body.get(chunk_id)
            # NICE stores a failed chunk parse as an empty Container.
            if chunk is not None and (getattr(chunk, "fids", None) is not None
                                      or getattr(chunk, "customizableFids", None) is not None):
                return chunk
    header = getattr(parsed, "header", None)
    for entry, raw in zip(getattr(header, "entries", None) or (), getattr(header, "data", None) or ()):
        if getattr(entry, "id", None) != 0x090F4000 or not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            from NICE.src.gbx_structs import body_chunks

            return body_chunks[_CHUNK_VERSIONED2].parse(bytes(raw))
        except Exception as e:  # noqa: BLE001
            log.debug("game skin header fids unreadable: %s", e)
    return None


def _parse_fids(raw_list: Any) -> list[SkinFid]:
    out: list[SkinFid] = []
    if not raw_list:
        return out
    for entry in raw_list:
        if entry is None:
            continue
        fid = SkinFid(
            class_id=int(getattr(entry, "classId", 0) or getattr(entry, "ClassId", 0) or 0),
            name=str(getattr(entry, "name", "") or getattr(entry, "type", "") or ""),
            directory=str(getattr(entry, "directory", "") or getattr(entry, "filePath", "") or ""),
        )
        out.append(fid)
    return out


@register(0x090F4000)
def load_game_skin(parsed, *, source_path="", resolver=None):  # noqa: ARG001
    body = body_dict(parsed)
    skin = GameSkin(
        source_path=source_path or None,
        class_id=int(getattr(parsed, "classId", 0) or 0),
        name=file_stem(source_path),
        raw=parsed,
    )

    # Unversioned painter info (chunk 003)
    chunk = body.get(_CHUNK_PAINTER)
    if chunk is not None:
        skin.painter_texture_name = str(
            getattr(chunk, "painterTextureName", "") or ""
        )
        skin.painter_scene_id = str(
            getattr(chunk, "painterSceneId", "") or ""
        )

    # Versioned layout — prefer 004 (chunkl-spec), fall back to 005 (NICE legacy)
    chunk = skin_fids_chunk(parsed)
    if chunk is not None:
        # dir_name field has two possible names depending on chunk version
        skin.dir_name = str(
            getattr(chunk, "dirName", "")
            or getattr(chunk, "relativeSkinDirectory", "")
            or ""
        )
        skin.dir_name_alt = str(getattr(chunk, "dirNameAlt", "") or "")

        # painter info from versioned chunk supersedes chunk 003
        ptr = str(getattr(chunk, "painterTextureName", "") or getattr(chunk, "u02", "") or "")
        psi = str(getattr(chunk, "painterSceneId",    "") or getattr(chunk, "u03", "") or "")
        if ptr:
            skin.painter_texture_name = ptr
        if psi:
            skin.painter_scene_id = psi

        raw_fids = (
            getattr(chunk, "customizableFids", None)
            or getattr(chunk, "fids", None)
        )
        skin.fids = _parse_fids(raw_fids)

    log.debug(
        "Loaded game_skin %s dir=%r fids=%d",
        skin.name, skin.dir_name, len(skin.fids),
    )
    return skin


__all__: list[str] = ["skin_fids_chunk"]
