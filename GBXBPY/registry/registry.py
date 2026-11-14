"""Class-id → loader registry.

Loaders in :mod:`GBXBPY.load` register themselves here via the
:func:`register` decorator. :func:`dispatch` looks up the right loader for
a parsed NICE container and calls it, returning a domain
:class:`~GBXBPY.model.Asset`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from ..errors import UnsupportedClassError
from ..model.asset import Asset

log = logging.getLogger("gbxbpy.registry")

# Loader signature: (parsed_container, *, source_path, resolver) -> Asset
LoaderFn = Callable[..., Asset]

_REGISTRY: Dict[int, LoaderFn] = {}


def register(class_id: int) -> Callable[[LoaderFn], LoaderFn]:
    """Decorator: register ``fn`` as the loader for the given GBX class id.

    Re-registering an existing class id overwrites the previous entry and
    emits a debug log entry — convenient during development and reloads.
    """

    def wrapper(fn: LoaderFn) -> LoaderFn:
        if class_id in _REGISTRY:
            log.debug("Overwriting loader for class 0x%08X (%s -> %s)",
                      class_id, _REGISTRY[class_id].__name__, fn.__name__)
        _REGISTRY[class_id] = fn
        return fn

    return wrapper


def get_loader(class_id: int) -> Optional[LoaderFn]:
    """Return the loader registered for ``class_id``, or ``None``."""
    return _REGISTRY.get(class_id)


def registered_class_ids() -> list[int]:
    """Sorted list of class ids that currently have a loader registered."""
    return sorted(_REGISTRY)


def dispatch(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> Asset:
    """Look up a loader by ``parsed.classId`` and run it.

    Raises:
        UnsupportedClassError: When no loader is registered for the class id.
    """
    class_id = getattr(parsed, "classId", None)
    if class_id is None:
        raise UnsupportedClassError("Parsed container has no classId")

    loader = _REGISTRY.get(class_id)
    if loader is None:
        raise UnsupportedClassError(
            f"No GBXBPY loader registered for class 0x{class_id:08X} "
            f"(file: {source_path or '?'})"
        )

    log.debug("Dispatching class 0x%08X to %s", class_id, loader.__name__)
    return loader(parsed, source_path=source_path, resolver=resolver)


__all__ = ["register", "get_loader", "registered_class_ids", "dispatch"]
