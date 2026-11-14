"""Persistent on-disk cache for parsed GBX assets.

Wraps any ``loader_fn(abs_path) -> Asset`` so repeated parses of the
same file (across imports / Blender sessions) return a pickled copy
instead of re-running NICE.

Cache key: SHA1 of the absolute path. Validity: ``(mtime_ns, size,
schema_version)`` stored in the pickle header — any mismatch triggers
a fresh parse.

Usage::

    from GBXBPY.cache import set_cache_dir, cached_load
    set_cache_dir(r"C:\\tmp\\gbx-cache")
    asset = cached_load(abs_path, lambda p: dispatch(read_gbx(p), ...))
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
from typing import Any, Callable, Optional

log = logging.getLogger("gbxbpy.cache")

# Bump when model shape or decoded values change; old pickles must reparse.
SCHEMA_VERSION = 11  # Preserve GPU particle hue controls.

_cache_dir: str = ""
_enabled: bool = False
# Per-process in-memory cache so a single import doesn't hit disk twice
# for the same file.
_memo: dict[str, Any] = {}


def set_cache_dir(path: str) -> None:
    """Configure where pickle files live. Empty string disables the cache."""
    global _cache_dir, _enabled
    if path:
        try:
            os.makedirs(path, exist_ok=True)
            _cache_dir = os.path.abspath(path)
            _enabled = True
            log.info("parse cache enabled: %s", _cache_dir)
        except OSError as e:
            log.warning("parse cache: cannot create %r: %s", path, e)
            _cache_dir = ""
            _enabled = False
    else:
        _cache_dir = ""
        _enabled = False


def is_enabled() -> bool:
    return _enabled


def reset_memo() -> None:
    """Drop the in-memory cache (call once per import to free RAM)."""
    _memo.clear()


def cache_path_for(abs_path: str) -> str:
    """Return where the pickle for ``abs_path`` would be stored."""
    digest = hashlib.sha1(abs_path.lower().encode("utf-8")).hexdigest()
    # Two-level fan-out keeps any single dir under a few thousand files.
    return os.path.join(_cache_dir, digest[:2], digest + ".pkl")


def cached_load(
    abs_path: str,
    loader: Callable[[str], Any],
    *,
    label: str = "",
) -> Optional[Any]:
    """Return parsed asset for ``abs_path``, hitting disk cache when possible.

    ``loader(abs_path)`` is only called on a cache miss. Failures are
    propagated to the caller (the cache only stores successful parses).
    """
    if not abs_path:
        return None

    memo_hit = _memo.get(abs_path)
    if memo_hit is not None:
        return memo_hit

    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    mtime_ns = st.st_mtime_ns
    size = st.st_size

    if _enabled:
        pkl = cache_path_for(abs_path)
        cached = _read_pickle(pkl, mtime_ns=mtime_ns, size=size)
        if cached is not None:
            _memo[abs_path] = cached
            return cached

    # Miss — parse and (best-effort) write the pickle.
    asset = loader(abs_path)
    if asset is None:
        return None

    _memo[abs_path] = asset
    if _enabled:
        try:
            _write_pickle(
                cache_path_for(abs_path),
                asset,
                mtime_ns=mtime_ns,
                size=size,
                label=label or os.path.basename(abs_path),
            )
        except Exception as e:  # noqa: BLE001 — caching is best-effort
            log.debug("parse cache: write failed for %s: %s", abs_path, e)
    return asset


# ---------------------------------------------------------------------------
#  Internals
# ---------------------------------------------------------------------------

def _read_pickle(pkl_path: str, *, mtime_ns: int, size: int) -> Optional[Any]:
    if not os.path.isfile(pkl_path):
        return None
    try:
        with open(pkl_path, "rb") as f:
            header = pickle.load(f)
            if (
                not isinstance(header, dict)
                or header.get("schema") != SCHEMA_VERSION
                or header.get("mtime_ns") != mtime_ns
                or header.get("size") != size
            ):
                return None
            return pickle.load(f)
    except Exception as e:  # noqa: BLE001 — corrupt cache files just miss
        log.debug("parse cache: read failed for %s: %s", pkl_path, e)
        return None


def _write_pickle(
    pkl_path: str, asset: Any, *, mtime_ns: int, size: int, label: str,
) -> None:
    os.makedirs(os.path.dirname(pkl_path), exist_ok=True)
    tmp = pkl_path + ".tmp"
    header = {
        "schema": SCHEMA_VERSION,
        "mtime_ns": mtime_ns,
        "size": size,
        "label": label,
    }
    # Strip the heavy NICE container — it's a non-picklable construct
    # parser tree and we don't need it after dispatch.
    _strip_raw(asset)
    with open(tmp, "wb") as f:
        pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)
        pickle.dump(asset, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, pkl_path)


def _strip_raw(asset: Any) -> None:
    """Drop ``raw`` / NICE-container references from a parsed asset tree.

    NICE containers are ``construct.Container`` subclasses that hold
    parser state and are neither picklable nor needed by downstream
    code (we only consume the GBXBPY model dataclasses).
    """
    seen: set[int] = set()
    stack: list[Any] = [asset]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        # Dataclasses + objects with __dict__/__slots__.
        attrs = None
        if hasattr(obj, "__dict__"):
            attrs = obj.__dict__
        if attrs is None and hasattr(obj, "__slots__"):
            attrs = {s: getattr(obj, s, None) for s in obj.__slots__}
        if attrs is None:
            continue
        seen.add(id(obj))
        if "raw" in attrs:
            try:
                setattr(obj, "raw", None)
            except (AttributeError, TypeError):
                pass
        for v in attrs.values():
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                stack.extend(v)
            elif isinstance(v, dict):
                stack.extend(v.values())
            elif hasattr(v, "__dict__") or hasattr(v, "__slots__"):
                stack.append(v)


__all__ = [
    "SCHEMA_VERSION",
    "set_cache_dir",
    "is_enabled",
    "reset_memo",
    "cached_load",
    "cache_path_for",
]
