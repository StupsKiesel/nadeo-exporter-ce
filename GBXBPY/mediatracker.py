"""Read-only MediaTracker inspection; coordinates/times retain GBX units."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

from .errors import ParseError
from .model.mediatracker import (
    MediaTrackerBlock,
    MediaTrackerClip,
    MediaTrackerClipDocument,
    MediaTrackerTrack,
)


_CLASSES = {
    0x03029000: "Triangles",
    0x0304B000: "Triangles2D",
    0x0304C000: "Triangles3D",
    0x03084000: "CameraGame",
    0x030A0000: "CameraOrbital",
    0x030A1000: "CameraPath",
    0x030A2000: "CameraCustom",
    0x030A5000: "Image",
    0x030A8000: "Text",
    0x030AB000: "TransitionFade",
    0x03126000: "DepthOfField",
    0x03195000: "Interface",
    0x0329F000: "Entity",
    0x07010000: "ControlEffectSimi",
}
_CONDITIONS = (
    "None", "RaceTimeLessThan", "RaceTimeGreaterThan", "AlreadyTriggered",
    "SpeedLessThan", "SpeedGreaterThan", "NotAlreadyTriggered", "MaxPlayCount",
    "RandomOnce", "Random",
)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _plain(child)
            for key, child in value.items()
            if key not in {"_io", "_iopos", "_errors", "_warns", "node_offset"}
        }
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def _chunks(node: Any, *, omit: tuple[int, ...] = ()) -> dict[int, Any]:
    return {
        key: _plain(value)
        for key, value in node.get("body", {}).items()
        if isinstance(key, int) and key != 0xFACADE01 and key not in omit
    }


def _properties(chunk: Any, *, omit: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _plain(value)
        for key, value in chunk.items()
        if key not in omit and not str(key).startswith("_")
    }


def _clip_from_node(node: Any) -> MediaTrackerClip:
    if node.get("classId") != 0x03079000:
        raise ParseError(f"Expected CGameCtnMediaClip, got {node.get('classId')!r}")
    clip_data = node.get("body", {}).get(0x0307900D)
    if not isinstance(clip_data, dict) or "tracks" not in clip_data:
        raise ParseError("MediaTracker clip has no decoded track list")

    tracks = []
    for track_index, track_node in enumerate(clip_data["tracks"]):
        if track_node.get("classId") != 0x03078000:
            raise ParseError(f"Track {track_index} is not a CGameCtnMediaTrack")
        track_body = track_node.get("body", {})
        track_data = track_body.get(0x03078001)
        if not isinstance(track_data, dict) or "blocks" not in track_data:
            raise ParseError(f"Track {track_index} has no decoded block list")

        blocks = []
        for block_index, block_node in enumerate(track_data["blocks"]):
            if not isinstance(block_node, dict) or "classId" not in block_node:
                raise ParseError(f"Track {track_index} block {block_index} is invalid")
            blocks.append(MediaTrackerBlock(
                class_id=block_node["classId"],
                chunks=_chunks(block_node),
            ))

        settings = track_body.get(0x03078005)
        tracks.append(MediaTrackerTrack(
            name=track_data.get("name", ""),
            blocks=blocks,
            properties=_properties(track_data, omit=("name", "blocks")),
            settings=_plain(settings) if settings is not None else None,
            chunks=_chunks(track_node, omit=(0x03078001, 0x03078005)),
        ))

    return MediaTrackerClip(
        name=clip_data.get("name", ""),
        tracks=tracks,
        properties=_properties(clip_data, omit=("name", "tracks")),
        chunks=_chunks(node, omit=(0x0307900D,)),
    )


def load_media_clip(path: str | Path) -> MediaTrackerClipDocument:
    """Load a standalone ``.Clip.Gbx`` into plain editable domain data."""
    from NICE.src.parser import parse_bytes

    source_path = str(Path(path).resolve())
    source_bytes = Path(source_path).read_bytes()
    parsed = parse_bytes(source_bytes, filepath=source_path, recursive=False)
    if parsed.get("_errors"):
        raise ParseError(f"MediaTracker clip parse errors: {parsed['_errors']}")
    clip = _clip_from_node(parsed)
    return MediaTrackerClipDocument(
        clip=clip,
        source_path=source_path,
        source_bytes=source_bytes,
        source_label=Path(source_path).name,
        warnings=tuple(parsed.get("_warns", [])),
    )


MAP_MEDIA_SLOTS = (
    ("clipIntro", "Intro"),
    ("clipGroupInGame", "In-Game"),
    ("clipGroupEndRace", "End Race"),
    ("clipPodium", "Podium"),
    ("clipAmbiance", "Ambiance"),
)


def load_map_media_trackers(
    path: str | Path, *, include_global: bool = False,
) -> dict[str, MediaTrackerClipDocument]:
    """Load the five modern map slots independently, including empty slots."""
    from NICE.src.parser import parse_bytes

    source_path = str(Path(path).resolve())
    source_bytes = Path(source_path).read_bytes()
    parsed = parse_bytes(source_bytes, filepath=source_path, recursive=False)
    if parsed.get("classId") != 0x03043000:
        raise ParseError(f"Expected CGameCtnChallenge map, got {parsed.get('classId')!r}")
    if parsed.get("_errors"):
        raise ParseError(f"Map parse errors: {parsed['_errors']}")

    media_clips: dict[str, list[Any]] = {key: [] for key, _label in MAP_MEDIA_SLOTS}
    body = parsed.get("body", {})

    def add_clip(label: str, node: Any) -> None:
        if node is None or (isinstance(node, dict) and node.get("_index") == -1):
            return
        if not isinstance(node, dict) or node.get("classId") != 0x03079000:
            raise ParseError(f"{label} is not a CGameCtnMediaClip")
        media_clips.setdefault(label, []).append(node)

    map_media = body.get(0x03043049)
    if map_media is not None and (not isinstance(map_media, dict) or "version" not in map_media):
        raise ParseError("Map MediaTracker section is undecoded")
    if isinstance(map_media, dict) and "version" in map_media:
        for name in ("clipIntro", "clipPodium", "clipAmbiance"):
            add_clip(name, map_media.get(name))
        for group_name in ("clipGroupInGame", "clipGroupEndRace"):
            group = map_media.get(group_name)
            if group is None or group.get("_index") == -1:
                continue
            group_data = group.get("body", {}).get(0x0307A003)
            if not isinstance(group_data, dict) or "clips" not in group_data:
                raise ParseError(f"{group_name} has no decoded clip list")
            for node in group_data.get("clips", []):
                add_clip(group_name, node)
    if include_global:
        add_clip("clipGlobal", body.get(0x03043026, {}).get("clipGlobal"))

    documents = {}
    for slot, nodes in media_clips.items():
        tracks = []
        for index, node in enumerate(nodes):
            clip = _clip_from_node(node)
            for track in clip.tracks:
                if slot in ("clipGroupInGame", "clipGroupEndRace"):
                    label = clip.name or f"Clip {index + 1}"
                    track.name = f"{label} / {track.name}" if track.name else label
                tracks.append(track)
        documents[slot] = MediaTrackerClipDocument(
            clip=MediaTrackerClip(name=slot, tracks=tracks),
            source_path=source_path,
            source_bytes=source_bytes,
            source_label=dict(MAP_MEDIA_SLOTS).get(slot, slot),
            editable=False,
            warnings=tuple(parsed.get("_warns", [])),
        )
    return documents


def load_map_media_clip_preview(path: str | Path) -> MediaTrackerClipDocument:
    """Compatibility combined read-only preview, including legacy Global."""
    documents = load_map_media_trackers(path, include_global=True)
    combined_tracks = []
    for slot, document in documents.items():
        for track in document.clip.tracks:
            track.name = f"{slot} / {track.name}" if track.name else slot
            combined_tracks.append(track)
    first = next(iter(documents.values()))
    return MediaTrackerClipDocument(
        clip=MediaTrackerClip(name=Path(first.source_path).stem, tracks=combined_tracks),
        source_path=first.source_path,
        source_bytes=first.source_bytes,
        source_label=Path(first.source_path).name,
        editable=False,
        warnings=first.warnings,
    )


def write_media_clip(document: MediaTrackerClipDocument, path: str | Path) -> str:
    """Build and write an edited standalone clip to the requested path."""
    from .serializer.mediatracker import write_media_clip as _write_media_clip

    if not document.editable:
        raise ValueError("Map-embedded MediaTracker previews are read-only")
    return _write_media_clip(document, str(path))


def inspect_mediatracker(parsed: Any) -> dict[str, Any]:
    """Report all modern map clip slots and index-paired group triggers.

    Unknown/failed chunks are surfaced in ``issues`` rather than interpreted.
    Entity recording nodes are identified, not expanded into ghost samples.
    No scene objects, source bytes or parsed containers are modified.
    """
    if parsed.get("classId", 0x03043000) != 0x03043000:
        raise ValueError("MediaTracker map inspection requires CGameCtnChallenge (0x03043000)")
    issues: list[str] = []

    def value(raw, path):
        if isinstance(raw, bytes):
            return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "hex": raw.hex()}
        if isinstance(raw, dict):
            if "classId" in raw or "_index" in raw:
                ref = {"node_index": raw.get("_index"),
                       "class_id": f"0x{int(raw['classId']):08X}" if "classId" in raw else None}
                if raw.get("classId") == 0x07010000:
                    ref["chunks"] = chunks(raw, path)
                return ref
            return {str(k): value(v, f"{path}.{k}") for k, v in raw.items()
                    if not str(k).startswith("_")}
        if isinstance(raw, (list, tuple)):
            return [value(v, f"{path}[{i}]") for i, v in enumerate(raw)]
        if raw is None or isinstance(raw, (str, int, float, bool)):
            return raw
        raise TypeError(f"Unsupported report value at {path}: {type(raw).__name__}")

    def chunks(node, path, omit=()):
        result = {}
        for cid, chunk in node.get("body", {}).items():
            if cid == 0xFACADE01:
                continue
            if not isinstance(cid, int):
                if not str(cid).startswith("_"):
                    issues.append(f"{path}: unparsed body field {cid}")
                continue
            key = f"0x{cid:08X}"
            if not isinstance(chunk, dict):
                issues.append(f"{path}.{key}: invalid chunk")
                continue
            fields = {}
            for name, child in chunk.items():
                if name in ("_unknownChunkId", "_chunkParseFailed", "_bodyParseFailed", "rest"):
                    issues.append(f"{path}.{key}: undecoded {name}")
                    fields[name] = value(child, f"{path}.{key}.{name}")
                elif not str(name).startswith("_") and name not in omit:
                    fields[name] = value(child, f"{path}.{key}.{name}")
            result[key] = fields
        return result

    def require(node, class_id, path):
        if node.get("classId") != class_id:
            issues.append(f"{path}: expected class 0x{class_id:08X}, got {node.get('classId')}")
            return False
        return True

    def block(node, path):
        class_id = node.get("classId")
        if class_id not in _CLASSES:
            issues.append(f"{path}: unsupported block class {class_id}")
        return {
            "node_index": node.get("_index"),
            "class_id": f"0x{class_id:08X}" if class_id is not None else None,
            "type": _CLASSES.get(class_id, "Unknown"),
            "chunks": chunks(node, path),
        }

    def clip(node, path):
        require(node, 0x03079000, path)
        data = node.get("body", {}).get(0x0307900D, {})
        if "tracks" not in data:
            issues.append(f"{path}: missing decoded modern clip tracks")
        tracks = []
        for i, track_node in enumerate(data.get("tracks", [])):
            track_path = f"{path}.tracks[{i}]"
            require(track_node, 0x03078000, track_path)
            track_data = track_node.get("body", {}).get(0x03078001, {})
            if "blocks" not in track_data:
                issues.append(f"{track_path}: missing decoded track blocks")
            tracks.append({
                "node_index": track_node.get("_index"),
                "name": track_data.get("name", ""),
                "chunks": chunks(track_node, track_path, omit=("blocks",)),
                "blocks": [block(n, f"{track_path}.blocks[{j}]")
                           for j, n in enumerate(track_data.get("blocks", []))],
            })
        return {"node_index": node.get("_index"), "name": data.get("name", ""),
                "chunks": chunks(node, path, omit=("tracks",)), "tracks": tracks}

    def group(node, path):
        require(node, 0x0307A000, path)
        data = node.get("body", {}).get(0x0307A003, {})
        clips, triggers = data.get("clips", []), data.get("triggers", [])
        if "triggers" not in data or len(clips) != len(triggers):
            issues.append(f"{path}: missing/mismatched clip and trigger arrays")
        entries = []
        for i, clip_node in enumerate(clips):
            trigger = value(triggers[i], f"{path}.triggers[{i}]") if i < len(triggers) else None
            if trigger is not None:
                condition = trigger["condition"]
                trigger["condition_name"] = (_CONDITIONS[condition]
                    if 0 <= condition < len(_CONDITIONS) else "Unknown")
            entries.append({"index": i, "clip": clip(clip_node, f"{path}.clips[{i}]"),
                            "trigger": trigger})
        return {"node_index": node.get("_index"),
                "chunks": chunks(node, path, omit=("clips", "triggers")),
                "entries": entries,
                "unmatched_triggers": value(triggers[len(clips):], path)}

    def present(node, path):
        if node is None:
            return False
        if "_invalid_index" in node:
            issues.append(f"{path}: invalid node reference {node['_invalid_index']}")
        return node.get("_index") != -1

    body = parsed.get("body", {})
    media = body.get(0x03043049)
    slots, groups = {}, {}
    if media is not None:
        if "version" not in media:
            issues.append("map.0x03043049: MediaTracker section was not decoded")
        else:
            for name in ("clipIntro", "clipPodium", "clipAmbiance"):
                node = media.get(name)
                slots[name] = clip(node, name) if present(node, name) else None
            for name in ("clipGroupInGame", "clipGroupEndRace"):
                node = media.get(name)
                groups[name] = group(node, name) if present(node, name) else None
    global_node = body.get(0x03043026, {}).get("clipGlobal")
    slots["clipGlobal"] = (clip(global_node, "clipGlobal")
                           if present(global_node, "clipGlobal") else None)
    for legacy in (0x03043020, 0x03043021):
        if legacy in body:
            issues.append(f"map.0x{legacy:08X}: legacy clip slots not supported")
    additional = None
    if 0x03043064 in body:
        raw = body[0x03043064].get("_unknownChunkId")
        if isinstance(raw, bytes) and raw == struct.pack("<4I", 0, 0, 4, 0):
            additional = {"status": "verified_empty", "groups": []}
        else:
            issues.append("map.0x03043064: additional clip groups not decoded")
            additional = {"status": "undecoded", "raw": value(raw, "additional_groups")}
    clips = [c for c in slots.values() if c is not None]
    clips += [entry["clip"] for g in groups.values() if g for entry in g["entries"]]
    tracks = [t for c in clips for t in c["tracks"]]
    blocks = [b for t in tracks for b in t["blocks"]]
    return {
        "source": parsed.get("filepath", ""),
        "map_mediatracker_version": media.get("version") if media else None,
        "units": {"time": "seconds", "position": "GBX metres (Y up)",
                  "angles": "radians", "trigger_coords": "raw integer cells"},
        "trigger_size": value(media.get("triggerSize"), "trigger_size") if media else None,
        "slots": slots, "groups": groups, "additional_groups": additional,
        "summary": {"clips": len(clips), "tracks": len(tracks), "blocks": len(blocks),
                    "trigger_cells": sum(len(e["trigger"]["coords"]) for g in groups.values()
                                         if g for e in g["entries"] if e["trigger"])},
        "parser_errors": list(parsed.get("_errors", [])), "issues": issues,
        "complete": not issues and not parsed.get("_errors"),
        "entity_recordings": "Referenced recording nodes are not expanded into simulation samples",
    }


__all__ = [
    "MediaTrackerBlock",
    "MediaTrackerTrack",
    "MediaTrackerClip",
    "MediaTrackerClipDocument",
    "load_media_clip",
    "load_map_media_clip_preview",
    "load_map_media_trackers",
    "MAP_MEDIA_SLOTS",
    "write_media_clip",
    "inspect_mediatracker",
]
