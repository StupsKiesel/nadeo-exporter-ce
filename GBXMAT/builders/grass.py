"""World-projected Py/Pxz terrain, optionally with a UV-mapped T layer.

The T layer is selected by vertex alpha (times the T diffuse alpha when the
``t_alpha_mask`` param is set, for lake shores). Blender approximation for the
verified DecoHill, RoadDirt and DecoCliff texture sets, not a general
implementation of every Tech3 Py/Pxz/T shader. ``Custom.PyPxzHue`` (item
editor ``Material_BlockCustom`` materials) adds the green-to-TargetColor hue
shift and RoughMetal G as metallic.
"""

from __future__ import annotations

from ..images import load_image
from ._common import new_material, add_output, add_principled, add_image_texture, find_texture, apply_hue_recolour


def build(spec):
    mat = new_material(spec.name_hint)
    tree = mat.node_tree
    links = tree.links

    def math(operation, a, b=None):
        node = tree.nodes.new("ShaderNodeMath")
        node.operation = operation
        for index, value in enumerate((a, b)):
            if value is None:
                continue
            if isinstance(value, (int, float)):
                node.inputs[index].default_value = value
            else:
                links.new(value, node.inputs[index])
        return node.outputs[0]

    def separate(vector):
        node = tree.nodes.new("ShaderNodeSeparateXYZ")
        links.new(vector, node.inputs[0])
        return tuple(node.outputs)

    def combine(values):
        node = tree.nodes.new("ShaderNodeCombineXYZ")
        for value, socket in zip(values, node.inputs):
            if isinstance(value, (int, float)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def sample(slot, role, vector, suffix=""):
        ref = find_texture(spec, slot)
        if ref is None:
            return None
        image = load_image(ref.path, role, flip_normal_green=spec.flip_normal_green)
        if image is None:
            return None
        node = add_image_texture(tree, image, label=slot + suffix)
        links.new(vector() if callable(vector) else vector, node.inputs["Vector"])
        return node

    def normal_xy(tex):
        encoded = separate(tex.outputs["Color"])
        x = math("SUBTRACT", math("MULTIPLY", encoded[0], 2.0), 1.0)
        y = math("SUBTRACT", math("MULTIPLY", encoded[1], 2.0), 1.0)
        z = math("SQRT", math("MAXIMUM", math(
            "SUBTRACT", 1.0, math("ADD", math("MULTIPLY", x, x), math("MULTIPLY", y, y)),
        ), 0.0))
        return x, y, z

    geom = tree.nodes.new("ShaderNodeNewGeometry")
    # World-space coordinates keep the projected terrain continuous across
    # prefab instances, including rotated and mirrored block placements.
    position = separate(geom.outputs["Position"])
    normal = separate(geom.outputs["Normal"])
    binding_scale = float(spec.params.get("triplanar_scale", 1.0))
    projection_scale = float(spec.params.get("grass_projection_scale", 1.0 / 32.0))
    rough_scale = float(spec.params.get("projected_roughness_scale", projection_scale))
    vectors = {}

    def projected_vector(slot, u, v):
        # Texture-wrapper UV transforms (when the adapter provides them)
        # win over the family-level fallback scales.
        fallback = rough_scale if slot.endswith("RoughMetal") else projection_scale
        key = (slot, u, v)
        if key not in vectors:
            coords = []
            for axis, name in ((u, "u"), (v, "v")):
                scale = float(spec.params.get(f"uv_scale_{name}:{slot}", fallback))
                offset = float(spec.params.get(f"uv_offset_{name}:{slot}", 0.0))
                value = math("MULTIPLY", position[axis], binding_scale * scale)
                coords.append(math("ADD", value, offset) if offset else value)
            vectors[key] = combine((coords[0], coords[1], 0.0))
        return vectors[key]

    magnitudes = tuple(math("POWER", math("ABSOLUTE", axis), 4.0) for axis in normal)
    total = math("ADD", math("ADD", magnitudes[0], magnitudes[1]), magnitudes[2])
    weights = tuple(math("DIVIDE", axis, total) for axis in magnitudes)
    custom = spec.family == "Custom.PyPxzHue"
    target = None
    if custom:
        # BlenderMania exports (and edits) the item colour from "cus_color".
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.name = rgb.label = "cus_color"
        rgb.outputs[0].default_value = tuple(
            float(spec.params.get(f"custom_target_{c}", 1.0)) for c in "rgb"
        ) + (1.0,)
        target = rgb.outputs[0]

    def custom_hue(color):
        # Item custom textures are authored in green; the PyPxz_Hue shader
        # shifts that hue to TargetColor with the same mask formula as
        # apply_hue_recolour (the texel is its own mask, alpha 1).
        r, g, b = separate(color)
        k = math("MAXIMUM", math("SUBTRACT", g, math("MULTIPLY", math("ADD", r, b), 0.5)), 0.0)
        mean = tree.nodes.new("ShaderNodeVectorMath")
        mean.operation = "DOT_PRODUCT"
        links.new(target, mean.inputs[0])
        mean.inputs[1].default_value = (1 / 3, 1 / 3, 1 / 3)
        grey = math("MULTIPLY", math("SUBTRACT", g, k), mean.outputs["Value"])
        return combine(tuple(
            math("MINIMUM", math("ADD", math("MULTIPLY", k, t), grey), 1.0)
            for t in separate(target)
        ))

    shaders = []
    for axis, (u, v, prefix) in enumerate(((1, 2, "Pxz"), (0, 2, "Pxz"), (0, 1, "Py"))):
        bsdf = add_principled(tree)
        bsdf.label = "Terrain projection " + "XYZ"[axis]
        bsdf.inputs["Base Color"].default_value = spec.color
        bsdf.inputs["Roughness"].default_value = 1.0
        bsdf.inputs["Metallic"].default_value = 0.0
        slot = prefix + "BaseColor"
        tex = sample(slot, "Diffuse", lambda slot=slot, u=u, v=v: projected_vector(slot, u, v), " " + "XYZ"[axis])
        if tex is not None:
            color = tex.outputs["Color"]
            if custom:
                color = custom_hue(color)
            elif spec.family == "Terrain.PyPxz":
                color = apply_hue_recolour(
                    tree, spec, color, source_tex=tex,
                    mask_role=prefix + "BaseColorHueMask", location=(-900, 700 - axis * 1200),
                )
            links.new(color, bsdf.inputs["Base Color"])
        slot = prefix + "RoughMetal"
        rough = sample(slot, "RoughMetal", lambda slot=slot, u=u, v=v: projected_vector(slot, u, v), " " + "XYZ"[axis])
        if rough is not None:
            channels = int(rough.image.get("source_channels", 4) or 4)
            # Vista RGBA Py/Pxz maps pack roughness in G (R is zero).
            # Stadium RGB RoughMetal and BC4 maps retain their R channel.
            channel = 1 if spec.family == "Terrain.PyPxz" and channels == 4 else 0
            rough_metal = separate(rough.outputs["Color"])
            links.new(rough_metal[channel], bsdf.inputs["Roughness"])
            if custom and channels >= 2:
                # CustomMetal/MetalPainted/Ice: G is metallic.
                links.new(rough_metal[1], bsdf.inputs["Metallic"])
        slot = prefix + "Normal"
        tex = sample(slot, "Normal", lambda slot=slot, u=u, v=v: projected_vector(slot, u, v), " " + "XYZ"[axis])
        if tex is not None:
            x, y, z = normal_xy(tex)
            # Whiteout blend with the geometric normal: a neutral map
            # preserves arbitrary slopes instead of snapping to an axis.
            mapped = list(normal)
            mapped[u] = math("ADD", normal[u], x)
            mapped[v] = math("ADD", normal[v], y)
            mapped[axis] = math("MULTIPLY", normal[axis], z)
            normalize = tree.nodes.new("ShaderNodeVectorMath")
            normalize.operation = "NORMALIZE"
            links.new(combine(mapped), normalize.inputs[0])
            links.new(normalize.outputs[0], bsdf.inputs["Normal"])
        shaders.append(bsdf.outputs["BSDF"])

    side = tree.nodes.new("ShaderNodeMixShader")
    side.label = "Terrain side projections"
    links.new(math("DIVIDE", weights[1], math("ADD", weights[0], weights[1])), side.inputs[0])
    links.new(shaders[0], side.inputs[1])
    links.new(shaders[1], side.inputs[2])
    terrain = tree.nodes.new("ShaderNodeMixShader")
    terrain.label = "Terrain top / side projections"
    links.new(weights[2], terrain.inputs[0])
    links.new(side.outputs[0], terrain.inputs[1])
    links.new(shaders[2], terrain.inputs[2])
    if find_texture(spec, "TBaseColor") is None:
        # Py/Pxz-only shaders (DecoCliff) have no UV-mapped T layer.
        links.new(terrain.outputs[0], add_output(tree).inputs["Surface"])
        return mat

    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "BaseMaterial"
    top = add_principled(tree)
    top.label = "Terrain UV layer"
    top.inputs["Base Color"].default_value = spec.color
    top.inputs["Metallic"].default_value = 0.0
    top.inputs["Roughness"].default_value = 1.0
    t_color = sample("TBaseColor", "Diffuse", uv.outputs["UV"])
    if t_color is not None:
        links.new(t_color.outputs["Color"], top.inputs["Base Color"])
    tex = sample("TRoughMetal", "RoughMetal", uv.outputs["UV"])
    if tex is not None:
        links.new(separate(tex.outputs["Color"])[0], top.inputs["Roughness"])
    tex = sample("TNormal", "Normal", uv.outputs["UV"])
    if tex is not None:
        x, y, z = normal_xy(tex)
        encoded = combine(tuple(math("ADD", math("MULTIPLY", axis, 0.5), 0.5) for axis in (x, y, z)))
        nmap = tree.nodes.new("ShaderNodeNormalMap")
        nmap.uv_map = "BaseMaterial"
        links.new(encoded, nmap.inputs["Color"])
        links.new(nmap.outputs[0], top.inputs["Normal"])

    color = tree.nodes.new("ShaderNodeVertexColor")
    color.layer_name = "Color"
    color.label = "Terrain / T vertex alpha"
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.label = "Terrain / T"
    blend = color.outputs["Alpha"]
    if t_color is not None and spec.params.get("t_alpha_mask"):
        # Lake shores (RiverShore_D) fade into the projected bed through the
        # T texture's alpha; their vertex alpha is a constant 1.
        blend = math("MULTIPLY", blend, t_color.outputs["Alpha"])
    links.new(blend, mix.inputs[0])
    links.new(terrain.outputs[0], mix.inputs[1])
    links.new(top.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], add_output(tree).inputs["Surface"])
    return mat
