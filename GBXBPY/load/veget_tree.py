"""Loader for ``CPlugVegetTreeModel`` (class id ``0x2F086000``).

Vegetation files (``*.VegetTreeModel.Gbx``) carry two or three LOD trees
(``tree1`` = high, ``tree2`` = lower, ``tree3`` = lowest; the third tree is
present when the header's tree count ``u02`` is 3, which is the case for most
vista-environment models). Each tree is an array of
*parts*; every part references either:

* an **inline visual node** (``CPlugVisualIndexedTriangles``) whose body
  contains the mesh data directly, or
* an **external mesh** (a NodeRef with a GameData-relative path).

Each part also carries a ``material_index`` into the model-level
``materials`` (a list of name strings) and ``textures`` (one D/N/R
texture entry per material).

This loader produces a :class:`~GBXBPY.model.VegetationModel` containing
a single :class:`VegetationVariant`. The variant's :class:`Mesh` holds
one :class:`MeshLOD` per non-empty tree (LOD 1/2/4 = tree1/2/3, following the
Trackmania 1, 2, 4, 8 LOD numbering)
and one :class:`MaterialBinding` per material slot, with texture paths
populated from the textures array.

Vista files must be the full Openplanet extracts; the truncated stubs
shipped in some GameData dumps come back with ``_bodyParseFailed`` in the
body. We log a warning and return an empty :class:`VegetationModel` rather
than crashing.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from ..model import (
    MaterialBinding,
    Mesh,
    MeshLOD,
    Submesh,
    VegetationModel,
    VegetationVariant,
    Vertex,
)
from ..registry import register
from .mesh import (
    _bbox_for_visuals,
    _build_lod,
    _parse_visual,
    _texture_path_from_node,
    _ScratchVisual,
)

log = logging.getLogger("gbxbpy.load.veget_tree")

CLASS_ID = 0x2F086000

_CHUNK_MAIN = 0x2F086000


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _node_path(node: Any) -> str:
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


def _build_material_bindings(
    materials: List[str],
    textures: List[Any],
    *,
    source_path: str = "",
    resolver: Any = None,
) -> List[MaterialBinding]:
    """One :class:`MaterialBinding` per material slot, with D/N/R texture paths."""
    def texture_path(node: Any) -> str:
        # Keep ../ segments until resolution against the vegetation model.
        return str(getattr(node, "_relativeFilePath", "") or _texture_path_from_node(node))

    bindings: List[MaterialBinding] = []
    for i, name in enumerate(materials):
        binding = MaterialBinding()
        binding.material_path = str(name or "")
        if i < len(textures):
            tex = textures[i]
            for slot, attr in (("D", "texture_d"), ("N", "texture_n"), ("R", "texture_r")):
                node = getattr(tex, attr, None)
                path = texture_path(node)
                if not path:
                    img_attr = "image_" + attr.split("_", 1)[1]  # image_d / image_n / image_r
                    img_node = getattr(tex, img_attr, None)
                    path = texture_path(img_node)
                if path:
                    if resolver is not None:
                        resolved = resolver.resolve(path, source_path=source_path)
                        if resolved:
                            path = resolved
                    # The per-texture card flag is set on foliage, not bark,
                    # in the verified Stadium tree models.
                    target_slot = "FenceA" if slot == "D" and bool(getattr(tex, "u02", False)) else slot
                    binding.inline_textures[target_slot] = path
        bindings.append(binding)
    return bindings


def _collect_tree_parts(
    parts: List[Any],
) -> Tuple[List[_ScratchVisual], List[Tuple[int, int]]]:
    """Walk a ``tree1``/``tree2``/``tree3`` array, returning ``(visuals, geoms)``.

    Parts whose ``mesh`` is an external NodeRef (no inline body) are skipped
    with a debug log — VegetTreeModel parts almost always carry inline
    geometry, so an external ref is unusual and we cannot resolve it here
    without a resolver context.
    """
    visuals: List[_ScratchVisual] = []
    geoms: List[Tuple[int, int]] = []  # (visual_index, material_index)
    for part_idx, part in enumerate(parts or []):
        mesh_ref = getattr(part, "mesh", None)
        if mesh_ref is None:
            continue
        ext_path = _node_path(mesh_ref)
        body = getattr(mesh_ref, "body", None)
        if not body:
            if ext_path:
                log.debug(
                    "VegetTreeModel part[%d] references external mesh %s — "
                    "skipped (external refs not yet resolved here)",
                    part_idx, ext_path,
                )
            continue
        sv = _parse_visual(mesh_ref)
        if not sv.vertices or not sv.indices:
            continue
        mat_idx = int(getattr(part, "material_index", 0) or 0)
        visuals.append(sv)
        geoms.append((len(visuals) - 1, mat_idx))
    return visuals, geoms


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

@register(CLASS_ID)
def load_veget_tree(
    parsed: Any,
    *,
    source_path: str = "",
    resolver: Any = None,
) -> VegetationModel:
    """Build a :class:`VegetationModel` from a parsed ``CPlugVegetTreeModel``."""
    body = getattr(parsed, "body", None)

    model = VegetationModel()
    model.source_path = source_path

    # Vista-env files come back tagged when NICE could not decode the body.
    body_dict = body if isinstance(body, dict) else None
    if body_dict is not None and "_bodyParseFailed" in body_dict:
        log.warning(
            "VegetTreeModel body could not be decoded (truncated vista dump?): %s",
            source_path,
        )
        return model

    # Stadium files: the body Container *is* the main chunk struct (only
    # one chunk in the class). Vista files that did parse arrive the same
    # way. Older multi-chunk dict-form files keep working via the lookup.
    main = body
    if body_dict is not None:
        main = body_dict.get(_CHUNK_MAIN, body)

    if main is None or not (
        hasattr(main, "tree1") or hasattr(main, "tree2")
        or (isinstance(main, dict) and ("tree1" in main or "tree2" in main))
    ):
        log.warning("VegetTreeModel has no main chunk: %s", source_path)
        return model

    materials_raw = list(getattr(main, "materials", None) or [])
    textures_raw = list(getattr(main, "textures", None) or [])
    materials = [str(m) for m in materials_raw]
    bindings = _build_material_bindings(
        materials, textures_raw, source_path=source_path, resolver=resolver,
    )

    mesh_obj = Mesh()
    mesh_obj.source_path = source_path
    mesh_obj.materials = bindings

    lods: List[MeshLOD] = []
    for lod_level, attr in ((1, "tree1"), (2, "tree2"), (4, "tree3")):
        parts = list(getattr(main, attr, None) or [])
        if not parts:
            continue
        visuals, geoms = _collect_tree_parts(parts)
        if not geoms:
            continue
        lod = _build_lod(lod_level, 0.0, geoms, visuals)
        lods.append(lod)
    mesh_obj.lods = lods

    # Bounding box: union over the highest-detail LOD's visuals.
    if lods:
        # Re-derive from tree1 visuals if available (already inside lod.bounding_box,
        # but we also surface it on the Mesh implicitly via lods).
        pass

    if not lods:
        log.warning(
            "VegetTreeModel %s has no parseable inline geometry — "
            "external mesh refs are not yet resolved by this loader",
            source_path,
        )
        return model

    variant = VegetationVariant()
    variant.name = "default"
    variant.mesh = mesh_obj
    variant.weight = 1.0
    model.variants.append(variant)

    return model
