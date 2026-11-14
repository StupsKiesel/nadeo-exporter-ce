"""Preserving writer for edited standalone MediaTracker clips."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from construct import Container

from ..errors import ParseError
from ..model.mediatracker import (
    MediaTrackerBlock,
    MediaTrackerClipDocument,
    MediaTrackerTrack,
)


_CLIP_CLASS_ID = 0x03079000
_CLIP_DATA_CHUNK = 0x0307900D
_TRACK_CLASS_ID = 0x03078000
_TRACK_DATA_CHUNK = 0x03078001
_TRACK_SETTINGS_CHUNK = 0x03078005
_FACADE_CHUNK_ID = 0xFACADE01


def _to_construct(value: Any) -> Any:
    if isinstance(value, dict):
        result = Container()
        for key, child in value.items():
            result[key] = _to_construct(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_to_construct(child) for child in value]
    return value


def _merge_construct(original: Any, value: Any) -> Any:
    if isinstance(value, dict) and isinstance(original, dict):
        for key, child in value.items():
            if key not in original:
                raise ValueError(f"Adding parsed data fields is not supported: {key}")
            original[key] = _merge_construct(original[key], child)
        return original
    if isinstance(value, list) and isinstance(original, list):
        if len(value) != len(original):
            return _to_construct(value)
        for index, child in enumerate(value):
            original[index] = _merge_construct(original[index], child)
        return original
    return _to_construct(value)


def _replace_chunks(
    node: Any,
    chunks: dict[int, Any],
    path: str,
    *,
    exclude: tuple[int, ...] = (),
) -> None:
    body = node.get("body", {})
    existing = {
        key for key in body
        if isinstance(key, int) and key != _FACADE_CHUNK_ID and key not in exclude
    }
    if existing != set(chunks):
        raise ValueError(f"{path}: changing the chunk set is not supported")
    for chunk_id, value in chunks.items():
        body[chunk_id] = _merge_construct(body[chunk_id], value)


def _data_fields(data: Any) -> set[str]:
    return {key for key in data if isinstance(key, str) and not key.startswith("_")}


def _write_block(node: Any, block: MediaTrackerBlock, path: str) -> None:
    if node.get("classId") != block.class_id:
        raise ValueError(f"{path}: block class id changed")
    _replace_chunks(node, block.chunks, path)


def _write_track(node: Any, track: MediaTrackerTrack, path: str) -> None:
    if node.get("classId") != _TRACK_CLASS_ID:
        raise ValueError(f"{path}: expected a MediaTracker track node")

    body = node["body"]
    data = body.get(_TRACK_DATA_CHUNK)
    if data is None or len(data.get("blocks", [])) != len(track.blocks):
        raise ValueError(f"{path}: adding or removing blocks is not supported")
    if set(track.properties) != _data_fields(data) - {"name", "blocks"}:
        raise ValueError(f"{path}: changing the track data fields is not supported")
    data["name"] = track.name
    for key, value in track.properties.items():
        data[key] = _merge_construct(data[key], value)

    settings = body.get(_TRACK_SETTINGS_CHUNK)
    if (settings is None) != (track.settings is None):
        raise ValueError(f"{path}: adding or removing track settings is not supported")
    if settings is not None:
        if set(track.settings or {}) != _data_fields(settings):
            raise ValueError(f"{path}: changing the track settings fields is not supported")
        for key, value in (track.settings or {}).items():
            settings[key] = _merge_construct(settings[key], value)

    extra_chunks = {
        key: value for key, value in track.chunks.items()
        if key not in (_TRACK_DATA_CHUNK, _TRACK_SETTINGS_CHUNK)
    }
    _replace_chunks(
        node,
        extra_chunks,
        path,
        exclude=(_TRACK_DATA_CHUNK, _TRACK_SETTINGS_CHUNK),
    )

    for index, block in enumerate(track.blocks):
        _write_block(data["blocks"][index], block, f"{path}.blocks[{index}]")


def build_media_clip(document: MediaTrackerClipDocument) -> bytes:
    """Build and reparse an edited clip, preserving its original GBX structure."""
    from NICE.src.parser import generate_file, parse_bytes

    parsed = parse_bytes(
        document.source_bytes,
        filepath=document.source_path,
        recursive=False,
    )
    if parsed.get("classId") != _CLIP_CLASS_ID:
        raise ParseError("MediaTracker source is not a standalone CGameCtnMediaClip")
    if parsed.get("_errors"):
        raise ParseError(f"MediaTracker source has parse errors: {parsed['_errors']}")

    output = deepcopy(parsed)
    data = output.body.get(_CLIP_DATA_CHUNK)
    if data is None:
        raise ParseError("MediaTracker clip has no decoded clip data")
    if len(data.get("tracks", [])) != len(document.clip.tracks):
        raise ValueError("Adding or removing tracks is not supported")
    if set(document.clip.properties) != _data_fields(data) - {"name", "tracks"}:
        raise ValueError("Changing the clip data fields is not supported")

    data["name"] = document.clip.name
    for key, value in document.clip.properties.items():
        data[key] = _merge_construct(data[key], value)

    clip_chunks = {
        key: value for key, value in document.clip.chunks.items()
        if key != _CLIP_DATA_CHUNK
    }
    _replace_chunks(output, clip_chunks, "clip", exclude=(_CLIP_DATA_CHUNK,))

    for index, track in enumerate(document.clip.tracks):
        _write_track(data["tracks"][index], track, f"clip.tracks[{index}]")

    raw = generate_file(output, remove_external=False)
    reopened = parse_bytes(raw, recursive=False)
    if reopened.get("classId") != _CLIP_CLASS_ID or reopened.get("_errors"):
        raise ParseError(f"Exported MediaTracker clip failed reparse: {reopened.get('_errors')}")
    return raw


def write_media_clip(document: MediaTrackerClipDocument, path: str) -> str:
    """Write an edited clip to ``path`` and return the resulting path."""
    from pathlib import Path

    output_path = Path(path)
    raw = build_media_clip(document)
    output_path.write_bytes(raw)
    return str(output_path)


__all__ = ["build_media_clip", "write_media_clip"]
