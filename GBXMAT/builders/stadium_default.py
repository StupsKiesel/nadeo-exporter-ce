"""Stadium.Default — the 80% baseline shader.

Wires up the four most common Trackmania texture slots into a single
Principled-BSDF graph:

* :data:`TextureRole.DIFFUSE`     → ``Base Color``
* :data:`TextureRole.NORMAL`      → ``Normal`` via ``ShaderNodeNormalMap``
* :data:`TextureRole.ROUGH_METAL` → split into ``Roughness`` + ``Metallic``
                                    using ``Separate Color`` (R = rough, G = metal,
                                    as documented in Nadeo's texture list),
                                    wired directly.
                                    If the source DDS is single-channel
                                    (``image["source_channels"] == 1``)
                                    only roughness is wired and metal stays at 0.
* :data:`TextureRole.EMISSIVE`    → ``Emission Color``

Anything missing is silently omitted; the fallback default is whatever
``MaterialSpec.color`` says (so a flat-color material with no textures
still renders sensibly).
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
    attach_funcshader_uv,
    apply_hue_recolour,
    apply_grass_x2,
)


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_Stadium_Default")
    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(0, 0))
    out = add_output(tree, location=(300, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = spec.color

    # Vegetation / fence cards rely on a binary alpha cutout. Switch the
    # render method for cutout coverage; the shader threshold below makes
    # opacity binary even on Blender versions without a CLIP mode.
    if spec.alpha_cutout:
        # Blender 4.2+ uses ``surface_render_method``; older versions
        # use ``blend_method``. Set whichever exists.
        if hasattr(mat, "surface_render_method"):
            try:
                mat.surface_render_method = 'DITHERED'
            except Exception:  # noqa: BLE001
                pass
        if hasattr(mat, "blend_method"):
            try:
                mat.blend_method = 'CLIP'
                mat.alpha_threshold = 0.5
            except Exception:  # noqa: BLE001
                pass

    # ---- Diffuse --------------------------------------------------------
    diffuse_ref = find_texture(spec, TextureRole.DIFFUSE)
    diffuse_color_socket = None
    if diffuse_ref is not None:
        img = load_image(diffuse_ref.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, 200), label="Diffuse")
            attach_funcshader_uv(tree, tex, spec, TextureRole.DIFFUSE)
            diffuse_color_socket = tex.outputs["Color"]
            # TM diffuse textures often carry alpha for non-opacity reasons
            # (detail masks, packed channels). Only wire it as opacity when
            # the source material explicitly opted in via OpacityIsDiffuseAlpha.
            if (spec.opacity_from_diffuse_alpha or spec.alpha_cutout) and "Alpha" in bsdf.inputs:
                alpha = tex.outputs["Alpha"]
                if spec.alpha_cutout:
                    clip = tree.nodes.new("ShaderNodeMath")
                    clip.operation = "GREATER_THAN"
                    clip.location = (-300, 50)
                    clip.label = "Foliage cutout"
                    clip.inputs[1].default_value = 0.5
                    links.new(alpha, clip.inputs[0])
                    alpha = clip.outputs[0]
                links.new(alpha, bsdf.inputs["Alpha"])
            diffuse_color_socket = apply_hue_recolour(
                tree, spec, diffuse_color_socket, source_tex=tex,
            )

    # ---- Detail / TBaseColor overlay -----------------------------------
    # Tech3 ``PyPxzT*`` materials (TrackWall, RoadDirt, …) ship a second
    # color texture that's meant to darken/pattern the diffuse wherever
    # the third projection lands. We don't run the full triplanar
    # pipeline, so apply it as a uniform Multiply overlay — that matches
    # the dominant visual of the affected materials (e.g. the dark wood
    # stripes on TrackWall).
    detail_ref = find_texture(spec, TextureRole.DETAIL)
    if detail_ref is not None:
        det_img = load_image(detail_ref.path, TextureRole.DIFFUSE)  # sRGB
        if det_img is not None:
            det_tex = add_image_texture(
                tree, det_img, location=(-600, 480), label="DetailOverlay"
            )
            mix = tree.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = 'MULTIPLY'
            mix.location = (-300, 320)
            mix.label = "Detail × Diffuse"
            mix.inputs["Fac"].default_value = 1.0
            if diffuse_color_socket is not None:
                links.new(diffuse_color_socket, mix.inputs["Color1"])
            else:
                # No diffuse: feed the spec's flat color so the overlay
                # at least multiplies against something meaningful.
                mix.inputs["Color1"].default_value = spec.color
            links.new(det_tex.outputs["Color"], mix.inputs["Color2"])
            # The detail texture's alpha channel encodes how strongly the
            # third-projection overlay should apply (e.g. on TrackWall, the
            # alpha masks the wood-stripe regions). Drive the mix factor
            # with it so areas with alpha=0 keep the original diffuse and
            # areas with alpha=1 get the full overlay.
            links.new(det_tex.outputs["Alpha"], mix.inputs["Fac"])
            diffuse_color_socket = mix.outputs["Color"]

    if diffuse_color_socket is not None:
        diffuse_color_socket = apply_grass_x2(tree, spec, diffuse_color_socket)
        links.new(diffuse_color_socket, bsdf.inputs["Base Color"])

    # ---- Normal ---------------------------------------------------------
    # TM normal maps are typically BC5/ATI2 (only R+G compressed; blue is
    # filler). After RGBA decode, the blue channel is meaningless, so we
    # rebuild Z = sqrt(1 - R² - G²) before feeding Blender's NormalMap
    # node — otherwise upward-facing surfaces render dark because the
    # reconstructed normal points sideways instead of up.
    normal_ref = find_texture(spec, TextureRole.NORMAL)
    if normal_ref is not None:
        img = load_image(
            normal_ref.path,
            TextureRole.NORMAL,
            flip_normal_green=spec.flip_normal_green,
        )
        if img is not None:
            tex = add_image_texture(tree, img, location=(-900, -100), label="Normal")
            attach_funcshader_uv(tree, tex, spec, TextureRole.NORMAL)
            color_out = tex.outputs["Color"]

            if int(img.get("source_channels", 4) or 4) <= 3:
                # Separate → R²,G² → sum → 1-sum → sqrt → Combine(R,G,Z).
                sep_n = tree.nodes.new("ShaderNodeSeparateColor")
                sep_n.location = (-700, -100)
                links.new(color_out, sep_n.inputs["Color"])

                r_sq = tree.nodes.new("ShaderNodeMath")
                r_sq.operation = 'MULTIPLY'
                r_sq.location = (-540, -40)
                r_sq.label = "R²"
                links.new(sep_n.outputs["Red"], r_sq.inputs[0])
                links.new(sep_n.outputs["Red"], r_sq.inputs[1])

                g_sq = tree.nodes.new("ShaderNodeMath")
                g_sq.operation = 'MULTIPLY'
                g_sq.location = (-540, -160)
                g_sq.label = "G²"
                links.new(sep_n.outputs["Green"], g_sq.inputs[0])
                links.new(sep_n.outputs["Green"], g_sq.inputs[1])

                rg_sum = tree.nodes.new("ShaderNodeMath")
                rg_sum.operation = 'ADD'
                rg_sum.location = (-380, -100)
                rg_sum.label = "R²+G²"
                links.new(r_sq.outputs["Value"], rg_sum.inputs[0])
                links.new(g_sq.outputs["Value"], rg_sum.inputs[1])

                one_minus = tree.nodes.new("ShaderNodeMath")
                one_minus.operation = 'SUBTRACT'
                one_minus.location = (-220, -100)
                one_minus.label = "1 - (R²+G²)"
                one_minus.inputs[0].default_value = 1.0
                links.new(rg_sum.outputs["Value"], one_minus.inputs[1])
                # Clamp to avoid sqrt of a negative when R²+G² > 1 due to
                # filtering / mip rounding.
                one_minus.use_clamp = True

                z_sqrt = tree.nodes.new("ShaderNodeMath")
                z_sqrt.operation = 'SQRT'
                z_sqrt.location = (-60, -100)
                z_sqrt.label = "sqrt → Z"
                links.new(one_minus.outputs["Value"], z_sqrt.inputs[0])

                combine_n = tree.nodes.new("ShaderNodeCombineColor")
                combine_n.location = (100, -100)
                links.new(sep_n.outputs["Red"], combine_n.inputs["Red"])
                links.new(sep_n.outputs["Green"], combine_n.inputs["Green"])
                links.new(z_sqrt.outputs["Value"], combine_n.inputs["Blue"])
                color_out = combine_n.outputs["Color"]

            nmap = tree.nodes.new("ShaderNodeNormalMap")
            nmap.location = (260, -100)
            links.new(color_out, nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # ---- RoughMetal -----------------------------------------------------
    # Nadeo's texture list: ``_R`` red = roughness, green = metallic.
    rm_ref = find_texture(spec, TextureRole.ROUGH_METAL)
    if rm_ref is not None:
        img = load_image(rm_ref.path, TextureRole.ROUGH_METAL)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -400), label="RoughMetal")
            attach_funcshader_uv(tree, tex, spec, TextureRole.ROUGH_METAL)
            sep = tree.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-380, -400)
            links.new(tex.outputs["Color"], sep.inputs["Color"])
            links.new(sep.outputs["Red"], bsdf.inputs["Roughness"])

            grass_pbr = bool(spec.params.get("grass_pbr", 0.0))
            if grass_pbr:
                bsdf.inputs["Metallic"].default_value = 0.0

            # Only wire metallic if the source had a real green channel.
            # BC4/ATI1 textures decode as L → after RGBA conversion the
            # green channel is just a copy of red, which would make
            # everything fully metallic — wrong.
            if not grass_pbr and int(img.get("source_channels", 4) or 4) >= 2:
                links.new(sep.outputs["Green"], bsdf.inputs["Metallic"])

    # ---- Emissive -------------------------------------------------------
    em_ref = find_texture(spec, TextureRole.EMISSIVE)
    if em_ref is not None:
        img = load_image(em_ref.path, TextureRole.EMISSIVE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-600, -700), label="Emissive")
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
