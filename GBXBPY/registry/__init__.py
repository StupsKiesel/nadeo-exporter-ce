"""Loader registry: maps a GBX class id to the function that builds a model.

Loaders in :mod:`GBXBPY.load` register themselves here so the top-level
:func:`GBXBPY.load_file` can dispatch on class id alone.
"""

from .registry import register, get_loader, registered_class_ids, dispatch

__all__ = ["register", "get_loader", "registered_class_ids", "dispatch"]
