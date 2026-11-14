"""Vista terrain: per-vertex weighted texture-array layers.

Each used vertex material slot ``i`` contributes
``Layer<i>.Diffuse/Normal/RoughMetal`` (and ``Layer<i>.Mask`` for
BlendMask bindings) from its top (Py) layer, plus optional
``Layer<i>.Pxz<Role>`` images from its side (Pxz) layer. Images are
projected triplanar in world space with their own tile size
(``tile_py_*`` on top, ``tile_pxz_*`` on sides). Slot weights come from
the ``TMLayer<i>`` corner attributes written by the mesh builder;
BlendMask bindings sharpen them with the layer height masks. An optional
UV-mapped ``TBaseColor`` transition texture is overlaid by its alpha.

Blender approximation: the game shaders are not available, so the blend
math is a heuristic tuned for appearance.
"""

from __future__ import annotations

from ..images import load_image
from ._common import new_material, add_output, add_principled, add_image_texture, find_texture

_NEUTRAL_NORMAL = (0.5, 0.5, 1.0, 1.0)
_MISSING_COLOR = (0.35, 0.33, 0.3, 1.0)
# Height-blend sharpness for BlendMask bindings.
_MASK_BIAS = 0.25
_MASK_POWER = 4.0
_ROUGH_CHANNEL = 1


def _used_layers(spec) -> list[int]:
    return sorted({
        int(key.split(":", 1)[1]) for key in spec.params if key.startswith("tile_py_u:")
    })


def build(spec):
    mat = new_material(spec.name_hint)
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    def link(value, socket):
        if isinstance(value, (int, float)):
            socket.default_value = value
        elif isinstance(value, tuple):
            socket.default_value = value[:len(socket.default_value)]
        else:
            links.new(value, socket)

    def math(operation, a, b=None, clamp=False):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        node.use_clamp = clamp
        link(a, node.inputs[0])
        if b is not None:
            link(b, node.inputs[1])
        return node.outputs[0]

    def vmath(operation, a, b=None, scale=None):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        link(a, node.inputs[0])
        if b is not None:
            link(b, node.inputs[1])
        if scale is not None:
            link(scale, node.inputs["Scale"])
        return node.outputs["Vector"] if operation != "LENGTH" else node.outputs["Value"]

    def separate(vector):
        node = nodes.new("ShaderNodeSeparateXYZ")
        link(vector, node.inputs[0])
        return tuple(node.outputs)

    def combine(values):
        node = nodes.new("ShaderNodeCombineXYZ")
        for value, socket in zip(values, node.inputs):
            link(value, socket)
        return node.outputs[0]

    def weighted_sum(pairs):
        total = None
        for weight, value in pairs:
            term = vmath("SCALE", value, scale=weight)
            total = term if total is None else vmath("ADD", total, term)
        return total

    geom = nodes.new("ShaderNodeNewGeometry")
    position = separate(geom.outputs["Position"])
    normal = separate(geom.outputs["Normal"])
    magnitudes = [math("POWER", math("ABSOLUTE", axis), 4.0) for axis in normal]
    total = math("ADD", math("ADD", magnitudes[0], magnitudes[1]), magnitudes[2])
    axis_weights = [math("DIVIDE", m, total) for m in magnitudes]
    # (u, v) world axes per projection; X/Y are sides (Pxz), Z is top (Py).
    projections = ((1, 2, "pxz"), (0, 2, "pxz"), (0, 1, "py"))

    def vectors(layer):
        result = []
        for u, v, kind in projections:
            su = float(spec.params.get(f"tile_{kind}_u:{layer}", 64.0)) or 64.0
            sv = float(spec.params.get(f"tile_{kind}_v:{layer}", 64.0)) or 64.0
            result.append(combine((
                math("DIVIDE", position[u], su), math("DIVIDE", position[v], sv), 0.0,
            )))
        return result

    def samples(layer, role, image_role, vecs):
        # Sides (X/Y projections) use the slot's Pxz layer when it differs.
        refs = (find_texture(spec, f"Layer{layer}.Pxz{role}"), find_texture(spec, f"Layer{layer}.{role}"))
        images = {}
        for ref in refs:
            if ref is not None and ref.path not in images:
                images[ref.path] = load_image(ref.path, image_role, flip_normal_green=spec.flip_normal_green)
        py_image = images.get(refs[1].path) if refs[1] else None
        pxz_image = (images.get(refs[0].path) if refs[0] else None) or py_image
        py_image = py_image or pxz_image
        if py_image is None:
            return None
        out = []
        for axis, vec in enumerate(vecs):
            image = py_image if projections[axis][2] == "py" else pxz_image
            tex = add_image_texture(tree, image, label=f"Layer{layer} {role} {'XYZ'[axis]}")
            link(vec, tex.inputs["Vector"])
            out.append(tex.outputs["Color"])
        return out

    def triplanar(per_axis):
        return weighted_sum(zip(axis_weights, per_axis))

    layers = _used_layers(spec)
    raw_weights = {}
    layer_data = {}
    blend_mask = bool(spec.params.get("blend_mask"))
    for layer in layers:
        attr = nodes.new("ShaderNodeAttribute")
        attr.attribute_type = "GEOMETRY"
        attr.attribute_name = f"TMLayer{layer}"
        attr.label = f"TMLayer{layer} weight"
        weight = attr.outputs["Fac"]
        vecs = vectors(layer)
        mask = samples(layer, "Mask", "Mask", vecs) if blend_mask else None
        if mask is not None:
            height = separate(triplanar(mask))[0]
            weight = math("POWER", math("MULTIPLY", weight, math("ADD", height, _MASK_BIAS)), _MASK_POWER)
        raw_weights[layer] = weight
        layer_data[layer] = (
            samples(layer, "Diffuse", "Diffuse", vecs),
            samples(layer, "Normal", "Normal", vecs),
            samples(layer, "RoughMetal", "RoughMetal", vecs),
        )

    weight_total = None
    for weight in raw_weights.values():
        weight_total = weight if weight_total is None else math("ADD", weight_total, weight)
    if weight_total is None:
        bsdf = add_principled(tree)
        bsdf.inputs["Base Color"].default_value = _MISSING_COLOR
        link(bsdf.outputs["BSDF"], add_output(tree).inputs["Surface"])
        return mat
    weight_total = math("MAXIMUM", weight_total, 1.0e-5)
    weights = {layer: math("DIVIDE", w, weight_total) for layer, w in raw_weights.items()}

    base_color = weighted_sum(
        (weights[layer], triplanar(data[0]) if data[0] else _MISSING_COLOR[:3])
        for layer, data in layer_data.items()
    )
    # Terrain-layer _R textures store roughness in G only (R = B = 0);
    # the T-slot RoughMetal textures are greyscale, so G is valid for both.
    roughness = None
    for layer, data in layer_data.items():
        value = separate(triplanar(data[2]))[_ROUGH_CHANNEL] if data[2] else 0.8
        term = math("MULTIPLY", weights[layer], value)
        roughness = term if roughness is None else math("ADD", roughness, term)

    # Normals: decode is linear, so blend encoded colours per projection
    # across layers first, then apply one whiteout per projection.
    projected_normals = []
    for axis, (u, v, _kind) in enumerate(projections):
        encoded = weighted_sum(
            (weights[layer], data[1][axis] if data[1] else _NEUTRAL_NORMAL[:3])
            for layer, data in layer_data.items()
        )
        ex, ey, _ez = separate(encoded)
        x = math("SUBTRACT", math("MULTIPLY", ex, 2.0), 1.0)
        y = math("SUBTRACT", math("MULTIPLY", ey, 2.0), 1.0)
        z = math("SQRT", math("MAXIMUM", math(
            "SUBTRACT", 1.0, math("ADD", math("MULTIPLY", x, x), math("MULTIPLY", y, y)),
        ), 0.0))
        mapped = list(normal)
        mapped[u] = math("ADD", normal[u], x)
        mapped[v] = math("ADD", normal[v], y)
        mapped[axis] = math("MULTIPLY", normal[axis], z)
        projected_normals.append(vmath("NORMALIZE", combine(mapped)))
    terrain_normal = vmath("NORMALIZE", triplanar(projected_normals))

    bsdf = add_principled(tree)
    bsdf.label = "Terrain layers"
    bsdf.inputs["Metallic"].default_value = 0.0
    color_socket, rough_socket, normal_socket = base_color, roughness, terrain_normal

    t_ref = find_texture(spec, "TBaseColor")
    t_image = load_image(t_ref.path, "Diffuse") if t_ref else None
    if t_image is not None:
        uv = nodes.new("ShaderNodeUVMap")
        uv.uv_map = "BaseMaterial"
        t_tex = add_image_texture(tree, t_image, label="TBaseColor")
        link(uv.outputs["UV"], t_tex.inputs["Vector"])
        alpha = t_tex.outputs["Alpha"]
        mix = nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.label = "Terrain / T"

        def rgba(sockets, name):
            return next(s for s in sockets if s.name == name and s.type == "RGBA")

        link(alpha, mix.inputs["Factor"])
        link(color_socket, rgba(mix.inputs, "A"))
        link(t_tex.outputs["Color"], rgba(mix.inputs, "B"))
        color_socket = rgba(mix.outputs, "Result")
        rough_ref = find_texture(spec, "TRoughMetal")
        rough_image = load_image(rough_ref.path, "RoughMetal") if rough_ref else None
        if rough_image is not None:
            t_rough = add_image_texture(tree, rough_image, label="TRoughMetal")
            link(uv.outputs["UV"], t_rough.inputs["Vector"])
            rough_socket = math("ADD", math("MULTIPLY", rough_socket, math("SUBTRACT", 1.0, alpha)),
                                math("MULTIPLY", separate(t_rough.outputs["Color"])[_ROUGH_CHANNEL], alpha))
        normal_ref = find_texture(spec, "TNormal")
        normal_image = load_image(normal_ref.path, "Normal", flip_normal_green=spec.flip_normal_green) if normal_ref else None
        if normal_image is not None:
            t_norm = add_image_texture(tree, normal_image, label="TNormal")
            link(uv.outputs["UV"], t_norm.inputs["Vector"])
            nmap = nodes.new("ShaderNodeNormalMap")
            nmap.uv_map = "BaseMaterial"
            # Transition_N is two-channel (B = 0): rebuild Z before decoding.
            ex, ey, _ez = separate(t_norm.outputs["Color"])
            tx = math("SUBTRACT", math("MULTIPLY", ex, 2.0), 1.0)
            ty = math("SUBTRACT", math("MULTIPLY", ey, 2.0), 1.0)
            tz = math("SQRT", math("MAXIMUM", math(
                "SUBTRACT", 1.0, math("ADD", math("MULTIPLY", tx, tx), math("MULTIPLY", ty, ty)),
            ), 0.0))
            link(combine((ex, ey, math("ADD", math("MULTIPLY", tz, 0.5), 0.5))), nmap.inputs["Color"])
            normal_socket = vmath("NORMALIZE", vmath(
                "ADD", vmath("SCALE", normal_socket, scale=math("SUBTRACT", 1.0, alpha)),
                vmath("SCALE", nmap.outputs["Normal"], scale=alpha),
            ))

    link(color_socket, bsdf.inputs["Base Color"])
    link(rough_socket, bsdf.inputs["Roughness"])
    link(normal_socket, bsdf.inputs["Normal"])
    link(bsdf.outputs["BSDF"], add_output(tree).inputs["Surface"])
    _layout(tree)
    return mat


def _layout(tree) -> None:
    """Spread nodes into columns by longest link distance to the output."""
    depth = {node.name: 0 for node in tree.nodes}
    tree_links = [(l.from_node.name, l.to_node.name) for l in tree.links]
    for _ in range(len(depth)):
        changed = False
        for src, dst in tree_links:
            if depth[src] < depth[dst] + 1:
                depth[src] = depth[dst] + 1
                changed = True
        if not changed:
            break
    rows: dict[int, int] = {}
    for node in tree.nodes:
        d = depth[node.name]
        row = rows.get(d, 0)
        rows[d] = row + 1
        node.location = (-220.0 * d, -180.0 * row)
