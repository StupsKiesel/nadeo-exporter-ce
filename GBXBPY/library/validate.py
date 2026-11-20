"""Structural checks for extracted game files, and the library check.

Openplanet's pack browser used to write files cut off at a 4 KiB page
boundary (every vista VegetTreeModel was affected). The extractor plugin now
compares each written size with the game's, and the library refetches files
whose size differs from the game index; these checks catch the rest:

* ``.Gbx``: header, user data and reference table must parse; a compressed
  body must be exactly as long as its size field (and decompress when the
  ``lzo`` module is available). An uncompressed body (Openplanet always
  writes those) is accepted when it ends with the ``0xFACADE01``
  end-of-chunks marker or NICE parses it without errors. Many intact files
  legitimately end without the marker, and NICE cannot parse every class,
  so a body failing both is only rejected when the file size is an exact
  multiple of 4 KiB, the signature of the extractor bug.
* ``.dds``: header plus the byte size implied by format, size, mips and faces
  (Nadeo ships some TGA images named ``.dds``; those get the TGA check).
* ``.wav`` / ``.ogg`` / ``.webm`` / ``.tga``: container magic and declared
  lengths; other types (JSON and XML are lenient or encrypted) only have to
  be non-empty.

The module never imports ``bpy`` so it can run on a worker thread.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:
    from .index import GameIndex
    from .server import FileResult

log = logging.getLogger("gbxbpy.library.validate")

GBX_END_MARKER = b"\x01\xde\xca\xfa"
PAGE_SIZE = 4096

ProgressCallback = Callable[[float, str], None]

@dataclass
class FileCheck:
    ok: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class _Truncated(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise _Truncated(f"needs {n} bytes at offset {self.pos}, file has {len(self.data)}")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def string(self) -> bytes:
        return self.take(self.u32())


def _skip_folders(r: _Reader, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("reference table folder nesting too deep")
    for _ in range(r.u32()):
        r.string()
        _skip_folders(r, depth + 1)


def _lzo_decompress() -> Optional[Callable[[bytes, int], bytes]]:
    try:
        import lzo  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional native module
        return None
    return lambda data, size: lzo.decompress(data, False, size)


_lzo = _lzo_decompress()


def gbx_body(data: bytes, decompress: Optional[Callable[[bytes, int], bytes]] = None) -> Optional[bytes]:
    """Body bytes of a binary GBX file, or ``None``.

    ``None`` when the header is malformed or the body is compressed and no
    decompressor is available (``decompress`` overrides the ``lzo`` module).
    """
    r = _Reader(data)
    try:
        if r.take(3) != b"GBX":
            return None
        version = r.u16()
        fmt = r.take(4)
        r.u32()  # class id
        if version >= 6:
            r.take(r.u32())
        r.u32()  # node count
        external = r.u32()
        if external:
            r.u32()
            _skip_folders(r)
            for _ in range(external):
                flags = r.u32()
                if flags & 4:
                    r.u32()
                else:
                    r.string()
                r.u32()
                if version >= 5:
                    r.u32()
                if not flags & 4:
                    r.u32()
        if fmt[2:3] != b"C":
            return data[r.pos:]
        uncompressed = r.u32()
        r.u32()
        decompress = decompress or _lzo
        if decompress is None:
            return None
        return decompress(data[r.pos:], uncompressed)
    except (_Truncated, ValueError):
        return None
    except Exception:  # noqa: BLE001 - corrupt compressed stream
        return None


def check_gbx(data: bytes) -> FileCheck:
    r = _Reader(data)
    try:
        if r.take(3) != b"GBX":
            return FileCheck(False, "missing GBX magic")
        version = r.u16()
        if version < 3 or version > 6:
            return FileCheck(False, f"unsupported GBX version {version}")
        fmt = r.take(4)
        if fmt[0:1] != b"B" or fmt[1:2] not in b"UC" or fmt[2:3] not in b"UC":
            return FileCheck(False, f"unexpected format {fmt!r}")
        body_compressed = fmt[2:3] == b"C"
        r.u32()  # class id
        if version >= 6:
            r.take(r.u32())  # user data (header chunks)
        r.u32()  # node count
        external = r.u32()
        if external > 100_000:
            return FileCheck(False, f"implausible reference count {external}")
        if external:
            r.u32()  # ancestor level
            _skip_folders(r)
            for _ in range(external):
                flags = r.u32()
                if flags & 4:
                    r.u32()  # resource index
                else:
                    r.string()  # file name
                r.u32()  # node index
                if version >= 5:
                    r.u32()  # use file
                if not flags & 4:
                    r.u32()  # folder index
        if body_compressed:
            uncompressed = r.u32()
            compressed = r.u32()
            available = len(data) - r.pos
            if available < compressed:
                return FileCheck(False, f"truncated: compressed body needs {compressed} bytes, file has {available}")
            if available > compressed:
                return FileCheck(False, f"{available - compressed} unexpected bytes after compressed body")
            if _lzo is not None:
                try:
                    body = _lzo(data[r.pos:], uncompressed)
                except Exception as exc:  # noqa: BLE001 - corrupt stream
                    return FileCheck(False, f"body does not decompress: {exc}")
                if len(body) != uncompressed:
                    return FileCheck(False, "decompressed body size mismatch")
                if not body.endswith(GBX_END_MARKER):
                    return FileCheck(False, "decompressed body lacks end marker")
            return FileCheck(True)
        if len(data) - r.pos >= 4 and data.endswith(GBX_END_MARKER):
            return FileCheck(True)
    except _Truncated as exc:
        return FileCheck(False, f"truncated header: {exc}")
    except ValueError as exc:
        return FileCheck(False, str(exc))
    # Only page-sized bodies can be the extractor bug; anything else passes
    # either way, so skip the (slow) NICE parse for it.
    if len(data) % PAGE_SIZE:
        return FileCheck(True, "no end marker; not page-sized")
    if _nice_parses(data):
        return FileCheck(True)
    return FileCheck(False, f"truncated: body does not parse and ends at a {PAGE_SIZE}-byte page boundary ({len(data)} bytes)")


_quiet = threading.local()


class _QuietProbeFilter(logging.Filter):
    """Drops NICE records emitted while this thread runs a validation parse."""

    def filter(self, _record: logging.LogRecord) -> bool:
        return not getattr(_quiet, "active", False)


_PROBE_FILTER = _QuietProbeFilter()
for _name in ("nice.structs", "nice.construct", "tm2020.nice.parser", "tm2020.nice.content"):
    logging.getLogger(_name).addFilter(_PROBE_FILTER)


def check_parsed_light(parsed) -> FileCheck:
    """Require a complete CPlugLight parse, without interpreting its effects."""
    if getattr(parsed, "classId", None) != 0x0901D000:
        return FileCheck(False, "not a CPlugLight")
    errors = getattr(parsed, "_errors", None)
    if errors:
        return FileCheck(False, "; ".join(map(str, errors)))
    body = getattr(parsed, "body", None)
    if not isinstance(body, dict):
        return FileCheck(False, "missing CPlugLight body")
    settings = body.get(0x0901D004)
    if not isinstance(settings, dict) or "_chunkParseFailed" in settings:
        return FileCheck(False, "missing or failed CPlugLight settings")
    bodies = [body]
    model_body = getattr(settings.get("gxLightModel"), "body", None)
    if isinstance(model_body, dict):
        bodies.append(model_body)
    for chunks in bodies:
        if 0xFACADE01 not in chunks or "_bodyParseFailed" in chunks:
            return FileCheck(False, "incomplete light body")
        if any(isinstance(chunk, dict) and "_chunkParseFailed" in chunk
               for chunk in chunks.values()):
            return FileCheck(False, "failed light chunk")
    return FileCheck(True)


def check_light(data: bytes) -> FileCheck:
    """Validate a hook replacement without loading or fetching external references."""
    from ..errors import ParseError
    from ..io import read_gbx_bytes

    previous = getattr(_quiet, "active", False)
    _quiet.active = True
    try:
        parsed = read_gbx_bytes(data, source_path="<light recovery>", external_loader=_no_external)
    except ParseError as exc:
        return FileCheck(False, str(exc))
    finally:
        _quiet.active = previous
    return check_parsed_light(parsed)


def _nice_parses(data: bytes) -> bool:
    """True when NICE parses the whole file without chunk or body errors."""
    _quiet.active = True
    try:
        from ..io import read_gbx_bytes

        parsed = read_gbx_bytes(data, source_path="<gamedata sync>", external_loader=_no_external)
    except Exception:  # noqa: BLE001 - any parser failure means "not verified"
        return False
    finally:
        _quiet.active = False
    body = getattr(parsed, "body", None)
    if hasattr(body, "keys") and "_bodyParseFailed" in body:
        return False
    return not getattr(parsed, "_errors", None)


def _no_external(*_args, **_kwargs):
    return None


# DXGI formats: bytes per 4x4 block (block-compressed) or per pixel.
_DXGI_BLOCK = {70: 8, 71: 8, 72: 8, 73: 16, 74: 16, 75: 16, 76: 16, 77: 16, 78: 16,
               79: 8, 80: 8, 81: 8, 82: 16, 83: 16, 84: 16, 94: 16, 95: 16, 96: 16,
               97: 16, 98: 16, 99: 16}
_DXGI_PIXEL = {1: 16, 2: 16, 3: 16, 4: 16, 5: 12, 6: 12, 7: 12, 8: 12, 9: 8, 10: 8,
               11: 8, 12: 8, 13: 8, 14: 8, 15: 8, 16: 8, 17: 8, 18: 8, 23: 4, 24: 4,
               25: 4, 26: 4, 27: 4, 28: 4, 29: 4, 30: 4, 31: 4, 32: 4, 33: 4, 34: 4,
               35: 4, 36: 4, 37: 4, 38: 4, 39: 4, 40: 4, 41: 4, 42: 4, 43: 4, 44: 4,
               45: 4, 46: 4, 47: 4, 48: 2, 49: 2, 50: 2, 51: 2, 52: 2, 53: 2, 54: 2,
               55: 2, 56: 2, 57: 2, 58: 2, 59: 2, 60: 1, 61: 1, 62: 1, 63: 1, 64: 1,
               65: 1, 85: 2, 86: 2, 87: 4, 88: 4, 89: 4, 90: 4, 91: 4, 92: 4, 93: 4,
               115: 2}
_FOURCC_BLOCK = {b"DXT1": 8, b"DXT2": 16, b"DXT3": 16, b"DXT4": 16, b"DXT5": 16,
                 b"ATI1": 8, b"BC4U": 8, b"BC4S": 8, b"ATI2": 16, b"BC5U": 16, b"BC5S": 16}
_FOURCC_PIXEL = {36: 8, 110: 8, 111: 2, 112: 4, 113: 8, 114: 4, 115: 8, 116: 16}


def _dds_size(width: int, height: int, depth: int, mips: int, block: int, pixel: int) -> int:
    total = 0
    for _ in range(max(mips, 1)):
        if block:
            total += max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block * depth
        else:
            total += width * height * pixel * depth
        width, height, depth = max(1, width // 2), max(1, height // 2), max(1, depth // 2)
    return total


def check_dds(data: bytes) -> FileCheck:
    if data[:4] != b"DDS " and len(data) >= 18 and data[2] in (2, 3, 10, 11):
        return check_tga(data)
    if len(data) < 128 or data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        return FileCheck(False, "missing DDS header")
    flags, height, width = struct.unpack_from("<III", data, 8)
    depth, mips = struct.unpack_from("<II", data, 24)
    pf_flags, fourcc_raw, bitcount = struct.unpack_from("<I4sI", data, 80)
    caps2 = struct.unpack_from("<I", data, 112)[0]
    depth = depth if flags & 0x800000 and depth else 1
    mips = mips if flags & 0x20000 and mips else 1
    faces = bin(caps2 & 0xFC00).count("1") if caps2 & 0x200 else 1
    offset = 128
    block = pixel = 0
    if pf_flags & 0x4:
        if fourcc_raw == b"DX10":
            if len(data) < 148:
                return FileCheck(False, "truncated DX10 header")
            dxgi, _dim, misc, array_size = struct.unpack_from("<IIII", data, 128)
            offset = 148
            faces = max(array_size, 1) * (6 if misc & 0x4 else 1)
            block = _DXGI_BLOCK.get(dxgi, 0)
            pixel = _DXGI_PIXEL.get(dxgi, 0)
        else:
            block = _FOURCC_BLOCK.get(fourcc_raw, 0)
            pixel = _FOURCC_PIXEL.get(struct.unpack("<I", fourcc_raw)[0], 0)
    elif bitcount:
        pixel = bitcount // 8
    if not block and not pixel:
        return FileCheck(True, "unknown pixel format; header only")
    expected = offset + _dds_size(width, height, depth, mips, block, pixel) * faces
    if len(data) < expected:
        return FileCheck(False, f"truncated: pixel data needs {expected} bytes, file has {len(data)}")
    return FileCheck(True)


def check_wav(data: bytes) -> FileCheck:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return FileCheck(False, "missing RIFF/WAVE header")
    declared = struct.unpack_from("<I", data, 4)[0] + 8
    if len(data) < declared:
        return FileCheck(False, f"truncated: RIFF declares {declared} bytes, file has {len(data)}")
    return FileCheck(True)


def check_ogg(data: bytes) -> FileCheck:
    if data[:4] != b"OggS":
        return FileCheck(False, "missing OggS header")
    last = data.rfind(b"OggS")
    if last < 0 or len(data) - last < 27 or not data[last + 5] & 0x4:
        return FileCheck(False, "truncated: last Ogg page is not end-of-stream")
    return FileCheck(True)


def check_tga(data: bytes) -> FileCheck:
    if len(data) < 18:
        return FileCheck(False, "truncated TGA header")
    id_len, cmap_type, image_type = data[0], data[1], data[2]
    cmap_len, cmap_bits = struct.unpack_from("<H", data, 5)[0], data[7]
    width, height = struct.unpack_from("<HH", data, 12)
    bits = data[16]
    if image_type in (2, 3):
        expected = 18 + id_len + (cmap_len * ((cmap_bits + 7) // 8) if cmap_type else 0)
        expected += width * height * ((bits + 7) // 8)
        if len(data) < expected:
            return FileCheck(False, f"truncated: image needs {expected} bytes, file has {len(data)}")
    return FileCheck(True)


def check_webm(data: bytes) -> FileCheck:
    if data[:4] != b"\x1a\x45\xdf\xa3":
        return FileCheck(False, "missing EBML header")
    return FileCheck(True)


_CHECKERS: dict[str, Callable[[bytes], FileCheck]] = {
    ".gbx": check_gbx,
    ".dds": check_dds,
    ".wav": check_wav,
    ".ogg": check_ogg,
    ".tga": check_tga,
    ".webm": check_webm,
}


def check_bytes(name: str, data: bytes) -> FileCheck:
    if not data:
        return FileCheck(False, "empty file")
    checker = _CHECKERS.get(os.path.splitext(name)[1].lower())
    if checker is None:
        return FileCheck(True, "type not checked")
    return checker(data)


def check_file(path: str) -> FileCheck:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return FileCheck(False, f"unreadable: {exc}")
    return check_bytes(path, data)


# ---------------------------------------------------------------------------
# Library check
# ---------------------------------------------------------------------------


class CheckCancelled(BaseException):
    """Raised from the progress callback to abort a library check."""


def _no_progress(_fraction: float, _message: str) -> None:
    pass


def _walk(root: str) -> Iterable[str]:
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            yield os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")


def check_library(root: str, index=None, *, progress: Optional[ProgressCallback] = None) -> list[tuple[str, str]]:
    """``(GameData-relative path, reason)`` for every broken file below ``root``.

    A file whose size matches the game index counts as intact without
    reading it; a size mismatch is broken; files the index does not list
    get the structural check.
    """
    progress = progress or _no_progress
    rel_paths = list(_walk(root))
    total = max(len(rel_paths), 1)
    bad = []
    for number, rel in enumerate(rel_paths):
        if number % 50 == 0:
            progress(number / total, f"Checking {number}/{len(rel_paths)}")
        path = os.path.join(root, *rel.split("/"))
        expected = index.size(rel) if index is not None else None
        if expected:
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                bad.append((rel, f"unreadable: {exc}"))
                continue
            if size != expected:
                bad.append((rel, f"{size} bytes, the game has {expected}"))
            continue
        check = check_file(path)
        if not check.ok:
            bad.append((rel, check.reason))
    progress(1.0, f"Checked {len(rel_paths)} files")
    return bad


def restore_hook_header(hook_data: bytes, original_prefix: bytes, expected_size: int) -> bytes:
    """Restore omitted v6 header data only when the original prefix proves the repair."""
    if len(hook_data) < 17 or hook_data[:9] != b"GBX\x06\x00BUUR":
        raise ValueError("header restoration requires an uncompressed v6 GBX")
    header_size = struct.unpack_from("<I", hook_data, 13)[0]
    if not header_size or len(hook_data) + header_size != expected_size:
        raise ValueError("size deficit does not equal the declared header-data size")
    if len(original_prefix) < 17 + header_size or original_prefix[:17] != hook_data[:17]:
        raise ValueError("original header is unavailable or differs from the hook output")
    restored = hook_data[:17] + original_prefix[17:17 + header_size] + hook_data[17:]
    if restored[:len(original_prefix)] != original_prefix:
        raise ValueError("restored bytes do not match the original extracted prefix")
    if not restored.endswith(GBX_END_MARKER):
        raise ValueError("hook body has no end marker")
    check = check_gbx(restored)
    if not check.ok:
        raise ValueError(f"restored GBX is invalid: {check.reason}")
    return restored


def recover_hook_extract(rel: str, hook_data: bytes, normal_prefix: bytes,
                         expected_size: Optional[int]) -> bytes:
    """Bytes to install after a hook extraction of ``rel``.

    The hook output itself when it is complete and valid; otherwise the header
    data the game skipped, restored from the normal (truncated) extraction.
    Raises ``ValueError`` when neither is proven valid.
    """
    if hook_data and (not expected_size or len(hook_data) == expected_size):
        check = check_bytes(rel, hook_data)
        if check.ok:
            return hook_data
    if not expected_size:
        raise ValueError("hook output is not valid and the game size is unknown")
    try:
        return restore_hook_header(hook_data, normal_prefix, expected_size)
    except ValueError as exc:
        raise ValueError(f"hook output {len(hook_data)} of {expected_size} bytes; {exc}") from None


def remove_alias_copies(extract_root: str, index: GameIndex) -> int:
    """Delete ``Name.Gbx_394`` copies the game writes for ``Name.Gbx?394`` index aliases."""
    removed = 0
    for rel in _walk(extract_root):
        base, sep, number = rel.rpartition("_")
        if not sep or not number.isdigit() or not index.has_file(base) or index.has_file(rel):
            continue
        try:
            os.remove(os.path.join(extract_root, *rel.split("/")))
            removed += 1
        except OSError as exc:
            log.warning("Cannot remove alias copy %s: %s", rel, exc)
    return removed


def _contained(root_path: str, parts: list[str]) -> str:
    base = os.path.realpath(root_path)
    target = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath((base, target)) != base:
        raise ValueError("path escapes GameData directory")
    return target


def _game_parts(path: str) -> list[str]:
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0].lower() != "gamedata":
        raise ValueError("result is not a GameData file")
    parts = parts[1:]
    if any(not part or part in (".", "..") or ":" in part for part in parts):
        raise ValueError("invalid GameData path")
    return parts


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def move_file(source: str, target: str) -> None:
    """Atomically replace ``target`` with ``source`` and remove ``source``, across drives too."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.replace(source, target)
        return
    except OSError as exc:
        if not _cross_device(exc):
            raise
    partial = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".bridge-move-", dir=os.path.dirname(target),
                                         delete=False) as handle:
            partial = handle.name
        shutil.copyfile(source, partial)
        os.replace(partial, target)
        partial = None
    finally:
        if partial is not None and os.path.exists(partial):
            os.remove(partial)
    try:
        os.remove(source)
    except OSError as exc:
        log.warning("Installed %s but could not remove the extracted copy: %s", target, exc)


def same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def install_extracted(root: str, extract_root: str, results: Iterable[FileResult],
                      index: Optional[GameIndex] = None, *, verify: bool = True,
                      statuses: tuple[str, ...] = ("OK",),
                      progress: Optional[ProgressCallback] = None) -> tuple[int, dict[str, str]]:
    """Move extraction results from Openplanet's ``extract_root`` into the library ``root``.

    With ``verify``, a file is installed only when its size matches the game
    and it passes :func:`check_bytes`; rejected files stay in ``extract_root``
    for diagnosis. When both folders are the same, files are only checked.
    Returns ``(installed, {result path: reason})``.
    """
    progress = progress or _no_progress
    results = list(results)
    installed = 0
    failed = {}
    for number, result in enumerate(results):
        progress(number / max(len(results), 1), f"Installing {number}/{len(results)}: {result.path}")
        try:
            if result.status not in statuses:
                reason = f" ({result.reason})" if result.reason else ""
                raise ValueError(f"{result.status}: {result.detail}{reason}")
            parts = _game_parts(result.path)
            rel = "/".join(parts)
            source = _contained(extract_root, parts)
            target = _contained(root, parts)
            if not os.path.isfile(source):
                raise ValueError("extracted file not found")
            if verify:
                expected = index.size(rel) if index is not None else None
                if expected is None and result.detail.isdigit():
                    expected = int(result.detail)
                size = os.path.getsize(source)
                if expected and size != expected:
                    raise ValueError(f"size mismatch: {size} bytes, game has {expected}")
                with open(source, "rb") as handle:
                    check = check_bytes(rel, handle.read())
                if not check.ok:
                    raise ValueError(check.reason)
            if os.path.normcase(source) != os.path.normcase(target):
                move_file(source, target)
            installed += 1
        except (OSError, ValueError) as exc:
            failed[result.path] = str(exc)
            log.warning("Library install failed for %s: %s", result.path, exc)
    progress(1.0, f"Installed {installed}/{len(results)} files")
    return installed, failed


def missing_files(root: str, index: GameIndex, *, skip_suffixes: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """``(GameData-relative path, size)`` of every indexed file absent from ``root``."""
    present = {rel.lower() for rel in _walk(root)}
    return [(rel, size) for rel, size in index.files()
            if rel.lower() not in present and not rel.lower().endswith(skip_suffixes)]
