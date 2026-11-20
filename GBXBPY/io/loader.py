"""Read a .Gbx file and return the raw NICE container.

This module is the only place in GBXBPY that knows how NICE is invoked.
It returns the parsed container verbatim — chunk dispatch and domain
construction live in :mod:`GBXBPY.registry` and :mod:`GBXBPY.load`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..errors import ParseError

log = logging.getLogger("gbxbpy.io.loader")


def read_gbx(
    path: str,
    *,
    recursive: bool = True,
    external_loader: Optional[Any] = None,
) -> Any:
    """Parse a .Gbx file from disk and return the NICE container.

    Args:
        path: Absolute path to the .Gbx file.
        recursive: When True, NICE follows external references and parses
            referenced files. When False, callers must supply
            ``external_loader``.
        external_loader: Optional callback ``(relative_path) -> Container``
            used by NICE to resolve external refs. When provided this
            overrides ``recursive``.

    Returns:
        The parsed NICE container with ``classId``, ``header``, ``body``,
        ``referenceTable``, ``filepath``, ``_errors``, ``_warns`` populated.

    Raises:
        ParseError: When the file cannot be read or parsed.
    """
    abs_path = os.path.abspath(path)
    from .. import library

    if not library.ensure(abs_path):
        raise ParseError(f"GBX file not found: {abs_path}")

    def read():
        try:
            with open(abs_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise ParseError(f"Failed to read {abs_path}: {e}") from e

        return read_gbx_bytes(
            raw,
            source_path=abs_path,
            recursive=recursive,
            external_loader=external_loader,
        )

    try:
        parsed = read()
    except ParseError:
        if not library.refetch(abs_path):
            raise
        return read()
    if getattr(parsed, "_errors", None) and library.refetch(abs_path):
        return read()
    return parsed


def read_gbx_bytes(
    data: bytes,
    *,
    source_path: str = "",
    recursive: bool = True,
    external_loader: Optional[Any] = None,
) -> Any:
    """Parse GBX content from memory. See :func:`read_gbx` for arg semantics."""
    # NICE imports are deferred so importing GBXBPY does not pull NICE
    # before the addon has put the workspace root on sys.path.
    from NICE.src.parser import parse_bytes
    from NICE.src.gbx_structs import GbxStruct
    from construct import Container

    if external_loader is not None:
        errors: list = []
        warns: list = []
        try:
            parsed = GbxStruct.parse(
                data,
                gbx_data={},
                nodes=[],
                filename=source_path,
                load_external_file=external_loader,
                errors=errors,
                warns=warns,
            )
        except Exception as e:
            raise ParseError(f"NICE failed to parse {source_path}: {e}") from e
        parsed.filepath = source_path
        parsed.node_offset = 0
        parsed._errors = list(set(errors))
        parsed._warns = list(set(warns))
    else:
        try:
            parsed = parse_bytes(
                data,
                filepath=source_path,
                recursive=recursive,
                log=False,
            )
        except Exception as e:
            raise ParseError(f"NICE failed to parse {source_path}: {e}") from e

    if not hasattr(parsed, "classId"):
        raise ParseError(f"Parsed container has no classId: {source_path}")

    errs = list(getattr(parsed, "_errors", []) or [])
    if errs:
        log.debug("NICE reported %d errors for %s", len(errs), source_path)

    return parsed


__all__ = ["read_gbx", "read_gbx_bytes"]
