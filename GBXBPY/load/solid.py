"""Static legacy CPlugSolid trees, used by decoration ocean surrounds."""

from __future__ import annotations

from typing import Any

from ..errors import ParseError
from ..model.mesh import Mesh
from ..registry import register
from ._gbx_helpers import body_dict, file_stem, node_path
from .mesh import _build_lod, _parse_material_binding, _parse_visual

CLASS_ID = 0x09005000


@register(CLASS_ID)
def load_solid(parsed: Any, *, source_path: str = "", resolver: Any = None) -> Mesh:
    """Bake the static tree's local transforms before the usual mesh conversion."""
    visuals, materials, geoms = [], [], []

    def visit(node, ancestors=()):
        body = body_dict(node)
        transform = getattr(body.get(0x0904F01A), "loc", None)
        transforms = ((transform,) if transform is not None else ()) + ancestors
        geometry = body.get(0x0904F016)
        if geometry is not None:
            visual = _parse_visual(geometry.visual)
            if visual.vertices and visual.indices:
                for vertex in visual.vertices:
                    for loc in transforms:
                        x, y, z = vertex.px, vertex.py, vertex.pz
                        vertex.px = x * loc.XX + y * loc.YX + z * loc.ZX + loc.TX
                        vertex.py = x * loc.XY + y * loc.YY + z * loc.ZY + loc.TY
                        vertex.pz = x * loc.XZ + y * loc.YZ + z * loc.ZZ + loc.TZ
                        x, y, z = vertex.nx, vertex.ny, vertex.nz
                        vertex.nx = x * loc.XX + y * loc.YX + z * loc.ZX
                        vertex.ny = x * loc.XY + y * loc.YY + z * loc.ZY
                        vertex.nz = x * loc.XZ + y * loc.YZ + z * loc.ZZ
                visual.bbox_min = tuple(
                    min(getattr(v, axis) for v in visual.vertices) for axis in ("px", "py", "pz")
                )
                visual.bbox_max = tuple(
                    max(getattr(v, axis) for v in visual.vertices) for axis in ("px", "py", "pz")
                )
                binding = _parse_material_binding(geometry.shaderFile)
                binding.material_path = node_path(geometry.shaderFile) or binding.material_path
                geoms.append((len(visuals), len(materials)))
                visuals.append(visual)
                materials.append(binding)
        for child in getattr(body.get(0x0904F006), "children", ()) or ():
            visit(child, transforms)

    solid = body_dict(parsed).get(0x09005011)
    if solid is None:
        raise ParseError(f"CPlugSolid has no tree chunk: {source_path}")
    visit(solid.tree)
    return Mesh(
        source_path=source_path or None, class_id=CLASS_ID,
        name=file_stem(source_path), raw=parsed, materials=materials,
        lods=[_build_lod(1, 0.0, geoms, visuals)] if geoms else [],
    )
