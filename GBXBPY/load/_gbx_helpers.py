"""Small helpers shared by GBXBPY loaders.

These are deliberately simple — pull a body dict out of a parsed container,
read header metadata, extract a usable file path from a NodeRef. Loaders
re-implementing these inline made the codebase noisy.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Tuple


def surface_id(value: Any, *, gameplay: bool = False) -> str:
    """NICE sources may expose a surface ID as an integer or enum name."""
    if value is None or value == "":
        return ""
    text = str(value)
    if not text.lstrip("-").isdecimal():
        return text
    from NICE.src.gbx_enums import GbxEPlugSurfaceGameplayId, GbxEPlugSurfacePhysicsId
    enum = GbxEPlugSurfaceGameplayId if gameplay else GbxEPlugSurfacePhysicsId
    name = enum.decmapping.get(int(text))
    if name is None:
        logging.getLogger("gbxbpy.load").warning("Unknown %s surface ID: %s",
                                               "gameplay" if gameplay else "physics", text)
        return text
    return str(name)


def body_dict(parsed: Any) -> dict:
    """Return ``parsed.body`` coerced to a real dict (or empty dict)."""
    body = getattr(parsed, "body", None)
    if isinstance(body, dict):
        return body
    try:
        return dict(body) if body is not None else {}
    except (TypeError, ValueError):
        return {}


def file_stem(path: str) -> str:
    """Filename without any extensions (strip everything after the first dot)."""
    if not path:
        return ""
    base = os.path.basename(path)
    return base.split(".", 1)[0] if base else ""


def external_node_paths(parsed: Any, path: str) -> dict:
    """``{node index: absolute path}`` of the file's external references."""
    folder = os.path.dirname(path)
    nodes: dict = {}
    table = getattr(parsed, "referenceTable", None)
    for node in getattr(table, "externalNodes", None) or []:
        rel = getattr(node, "_computedRelativePath", "") or getattr(node, "ref", "") or ""
        nodes[int(node.nodeIndex)] = os.path.normpath(os.path.join(folder, rel))
    return nodes


def node_path(node: Any) -> str:
    """Pull a usable GameData-relative path off a NodeRef-like object.

    Returns ``""`` when no path is available.
    """
    if node is None:
        return ""
    for attr in ("gamedata_path", "filePath", "filepath"):
        value = getattr(node, attr, "")
        if value:
            return str(value)
    rel = getattr(node, "_relativeFilePath", "") or ""
    if rel:
        parts = rel.replace("\\", "/").split("/")
        while parts and parts[0] in ("..", ""):
            parts.pop(0)
        return "/".join(parts)
    return ""


def extract_header_meta(parsed: Any) -> Tuple[bytes, str]:
    """Return ``(icon_bytes, item_type_string)`` from the file header.

    ``item_type_string`` is empty for files that don't carry a 0x2E002000
    header chunk (e.g. block-info files).
    """
    header = getattr(parsed, "header", None)
    if header is None:
        return (b"", "")
    data = getattr(header, "data", None)
    if not isinstance(data, (list, tuple)):
        return (b"", "")
    icon = b""
    item_type = ""
    for entry in data:
        cid = int(getattr(entry, "chunkId", 0) or 0)
        if cid == 0x2E001004:
            icon = bytes(getattr(entry, "data", b"") or b"")
        elif cid == 0x2E002000:
            it = getattr(entry, "itemType", "") or ""
            if it:
                item_type = str(it)
    return (icon, item_type)


def extract_header_icon(parsed: Any) -> bytes:
    """Return the icon bytes from the file header, or ``b""``."""
    icon, _ = extract_header_meta(parsed)
    return icon


__all__ = [
    "surface_id",
    "body_dict",
    "file_stem",
    "node_path",
    "extract_header_meta",
    "extract_header_icon",
]
