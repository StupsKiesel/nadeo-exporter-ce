"""Diagnostic fallback material.

Used when ``MaterialSpec.family`` does not match any registered builder.
Produces a flat magenta material so missing-shader cases are obvious in
the viewport without crashing the import.
"""

from __future__ import annotations

from ..spec import MaterialSpec
from ._common import new_material, add_output, add_principled


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_FallbackMaterial")
    tree = mat.node_tree

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # Bright magenta + emissive so it's visible regardless of scene lighting.
    bsdf.inputs["Base Color"].default_value = (1.0, 0.0, 1.0, 1.0)
    if "Emission Color" in bsdf.inputs:           # Blender 4.x
        bsdf.inputs["Emission Color"].default_value = (1.0, 0.0, 1.0, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 0.5
    elif "Emission" in bsdf.inputs:               # older Blender
        bsdf.inputs["Emission"].default_value = (1.0, 0.0, 1.0, 1.0)
    return mat


__all__ = ["build"]
