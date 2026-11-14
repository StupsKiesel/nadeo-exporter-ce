"""Path and reference resolution.

Turns GameData-relative paths and external/node references into absolute
file paths and loaded assets. Caching strategy lives in :mod:`GBXBPY.io`.
"""

from .resolver import Resolver
from .block_lookup import BlockResolver, find_block_file
from .material_lookup import MaterialResolver
from .modifier_lookup import resolve_modifier_folder, resolve_modifier_materials, resolve_modifier_renames

__all__ = [
    "Resolver",
    "BlockResolver",
    "find_block_file",
    "MaterialResolver",
    "resolve_modifier_folder",
    "resolve_modifier_materials",
    "resolve_modifier_renames",
]
