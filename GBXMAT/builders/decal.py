"""Decal — alpha-cutout overlay material.

TM2020 decal layout (real materials inspected: DecalCurbs, DecalMarks,
DecalGateGameplay, DecalObstaclePusher, DecalObstacleTube, …):

    * ``BaseColorA`` — RGBA. RGB = paint color, **alpha = cutout mask**
      (the visible silhouette of the logo / mark / arrow). The adapter
      maps this slot onto the canonical ``Diffuse`` role.
    * ``RoughMetal``, ``Normal``, ``BaseColorHueMask`` — standard slots.
    * Some decals also ship a separate ``Mask`` slot; when present it
      overrides the diffuse alpha.

Render mode: dithered alpha-clip. ``BLEND`` causes severe sort-order
flicker when a decal sits coplanar / near-coplanar with its host face
(every paint stripe on a road segment, every Curb mark, …) and the
result in viewport rendering is the opaque rectangle behind the decal —
exactly the "black or white square" symptom. ``DITHERED`` (Blender 4.2+)
or ``CLIP`` (older) keeps alpha-cutout but writes depth normally so
faces stay sorted correctly.
"""

from __future__ import annotations

from ..spec import MaterialSpec, TextureRole
from ..images import load_image
from ._common import (
    new_material,
    add_output,
    add_principled,
    add_image_texture,
    find_texture,
    wire_rough_metal,
    attach_funcshader_uv,
    apply_hue_recolour,
    normal_map_color,
)


def _set_cutout_render(mat) -> None:
    """Switch ``mat`` to dithered alpha-cutout (Blender 4.x and 5.x)."""
    if hasattr(mat, "surface_render_method"):
        try:
            mat.surface_render_method = "DITHERED"
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = "CLIP"
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "alpha_threshold"):
        try:
            mat.alpha_threshold = 0.5
        except Exception:  # noqa: BLE001
            pass
    if hasattr(mat, "shadow_method"):
        try:
            mat.shadow_method = "CLIP"
        except Exception:  # noqa: BLE001
            pass


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_Decal")
    _set_cutout_render(mat)

    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.6

    # ---- Diffuse (color) -----------------------------------------------
    diffuse_alpha_socket = None
    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, 200), label="Diffuse")
            attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            links.new(apply_hue_recolour(tree, spec, tex.outputs["Color"], source_tex=tex), bsdf.inputs["Base Color"])
            diffuse_alpha_socket = tex.outputs["Alpha"]

    # ---- Mask (cutout shape) -------------------------------------------
    # Decal materials store the silhouette in a separate Mask texture —
    # without wiring it the quad renders as a fully-opaque rectangle
    # (showing as a square of whatever color the diffuse happens to be,
    # often black).
    mask_socket = None
    mask_ref = find_texture(spec, TextureRole.MASK)
    if mask_ref is not None:
        m_img = load_image(mask_ref.path, role="Mask")
        if m_img is not None:
            m_tex = add_image_texture(tree, m_img, location=(-600, -200), label="Mask")
            # Most TM mask textures are BC4 (red-only) — alpha is then
            # constant 1. Use Color.R when present and multiply by Alpha
            # so RGBA-with-alpha-mask textures still work.
            sep = tree.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-380, -200)
            links.new(m_tex.outputs["Color"], sep.inputs["Color"])
            mul = tree.nodes.new("ShaderNodeMath")
            mul.operation = "MULTIPLY"
            mul.location = (-180, -200)
            links.new(sep.outputs[0], mul.inputs[0])
            links.new(m_tex.outputs["Alpha"], mul.inputs[1])
            mask_socket = mul.outputs[0]

    # Wire alpha: mask wins when present, else fall back to diffuse alpha.
    alpha_socket = mask_socket or diffuse_alpha_socket
    if alpha_socket is not None and "Alpha" in bsdf.inputs:
        links.new(alpha_socket, bsdf.inputs["Alpha"])
    elif "Alpha" in bsdf.inputs:
        # Decal binding had no resolvable cutout texture (common when a
        # Tech3 DecalPaint material declares its slots but the actual
        # logo / mark texture is supplied per-instance via a MUI
        # override we couldn't follow). Force the quad nearly-invisible
        # rather than letting it render as a flat opaque rectangle on
        # top of the road / wall — that "black or white square" symptom
        # is visually destructive, while a barely-visible ghost lets
        # the user spot that the decal exists without occluding the
        # geometry behind it.
        bsdf.inputs["Alpha"].default_value = 0.05

    # ---- Normal --------------------------------------------------------
    normal_ref = find_texture(spec, TextureRole.NORMAL)
    if normal_ref is not None:
        img = load_image(
            normal_ref.path, TextureRole.NORMAL,
            flip_normal_green=spec.flip_normal_green,
        )
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -500), label="Normal")
            attach_funcshader_uv(tree, tex, spec, TextureRole.NORMAL)
            nmap = tree.nodes.new("ShaderNodeNormalMap")
            nmap.location = (-300, -500)
            links.new(normal_map_color(tree, tex.outputs["Color"], img), nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- RoughMetal ----------------------------------------------------
    rm_ref = find_texture(spec, TextureRole.ROUGH_METAL)
    if rm_ref is not None:
        img = load_image(rm_ref.path, TextureRole.ROUGH_METAL)
        if img is not None:
            wire_rough_metal(tree, bsdf, img, location=(-600, -800))

    # ---- Emissive ------------------------------------------------------
    em_ref = find_texture(spec, TextureRole.EMISSIVE)
    if em_ref is not None:
        img = load_image(em_ref.path, TextureRole.EMISSIVE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -1100), label="Emissive")
            attach_funcshader_uv(tree, tex, spec, TextureRole.EMISSIVE)
            em_socket = (
                bsdf.inputs.get("Emission Color")
                or bsdf.inputs.get("Emission")
            )
            if em_socket is not None:
                links.new(tex.outputs["Color"], em_socket)
                if "Emission Strength" in bsdf.inputs:
                    bsdf.inputs["Emission Strength"].default_value = 1.0

    return mat


__all__ = ["build"]
