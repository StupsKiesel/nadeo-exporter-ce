"""Read the external reference table of a GBX file without NICE.

Fetching a file's references before NICE parses it lets the library
request a whole dependency tree in a few batches instead of one round
trip per external file. Folder handling mirrors NICE's
``compute_all_folders``: folder index 0 is the file's own folder.
"""

from __future__ import annotations

import os
import struct


class _Truncated(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise _Truncated
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def string(self) -> str:
        return self.take(self.u32()).decode("utf-8", errors="replace")


def _folders(r: _Reader, parent: str, out: list[str], depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("reference folders nested too deep")
    for _ in range(r.u32()):
        folder = os.path.normpath(parent + r.string()) + os.sep
        out.append(folder)
        _folders(r, folder, out, depth + 1)


def parse_reference_table(data: bytes) -> list[str]:
    """Reference paths relative to the file's folder (may start with ``..``)."""
    r = _Reader(data)
    if r.take(3) != b"GBX":
        raise ValueError("missing GBX magic")
    version = r.u16()
    if version < 6:
        raise ValueError(f"unsupported GBX version {version}")
    r.take(4)  # format
    r.u32()  # class id
    r.take(r.u32())  # user data
    r.u32()  # node count
    count = r.u32()
    if not count:
        return []
    ancestors = r.u32()
    folders = [""]
    _folders(r, (".." + os.sep) * ancestors, folders)
    out = []
    for _ in range(count):
        flags = r.u32()
        name = None if flags & 4 else r.string()
        if flags & 4:
            r.u32()  # resource index
        r.u32()  # node index
        r.u32()  # use file
        if name is not None:
            folder_index = r.u32()
            if 0 <= folder_index < len(folders):
                out.append(os.path.normpath(folders[folder_index] + name))
    return out


def external_references(path: str) -> list[str]:
    """Absolute paths of the files ``path`` references; ``[]`` on any error."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(1 << 18)
            while True:
                try:
                    rels = parse_reference_table(data)
                    break
                except _Truncated:
                    more = handle.read()
                    if not more:
                        return []
                    data += more
    except (OSError, ValueError, UnicodeError, struct.error):
        return []
    base = os.path.dirname(os.path.abspath(path))
    return [os.path.normpath(os.path.join(base, rel)) for rel in rels]
