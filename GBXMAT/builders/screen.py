"""Screen — Tech3 ``*_DispIn`` materials (ad screens, chronos, podium screens).

The game renders these as an LED panel (``BaseColorOp``/``RoughMetal``/
``Normal`` tiled by ``TcScale_BRNH``) through which an emissive *inside*
image is seen. The inside image is the material's ``MulInside`` texture or
a map skin; parallax skins stack several layers at increasing depths.

Roles consumed:

* ``Diffuse``, ``Normal``, ``RoughMetal`` — LED panel surface. The Diffuse
  (``BaseColorOp``) alpha is the panel opacity: the base colour is
  premultiplied by it and the emission is scaled by ``1 - alpha``.
* ``SelfIllumMask`` — LED cell mask (``SI``) multiplied into the emission.
* ``Inside`` — default flat inside image.
* ``InsideLayer{i}`` — stock bitmap-array or skin layers; params ``inside_layer{i}_depth`` and
  ``inside_layer{i}_vtile``. Replace ``Inside`` when present.
* ``InsideForeground`` / ``InsideForeground{i}`` — foreground skin layers
  (images or movies) composited over the inside without parallax, deepest
  first by ``inside_fg{i}_depth``.

Every layer is alpha-composited over the ones below it. File-name suffixes
such as ``+111A`` (white shaped by alpha), ``+000A`` (black fade) and
``+111Y`` (white shaped by luminance) arrive as ``<prefix>_fill_r/g/b`` and
``<prefix>_luma`` params, see :func:`GBXBPY.resolve.skins.layer_blend`.

Params: ``tc_scale_u/v``, ``inside_parallax``, ``meters_uv_parallax``,
``self_illum_scale``, ``meters_depth``, ``meters_uv_u/v``, ``refract_index``,
``double_refract``. ``Height`` drives the eight-step refracted LED-mask
relief lookup, independently of background-image parallax.
The emission follows the decoded DispIn shaders:
``inside × SI × (1 - BaseColorOp.a) × SelfIllum_Scale``; the game samples
``SI`` through a height-map relief lookup.
View-dependent offsets follow the game's
``Tech3/Block_*_DispIn`` pixel shader (decoded from
``GpuCache_D3D11_SM5.zip``): a layer ``d`` metres deep is shifted by
``slope × d / (S + d)`` in image UV, where ``slope`` is the tangent-space
view direction ``(x, y) / z`` and ``S = Meters_UV_Parallax × (aspect, 1)``.
Deep layers saturate to ``slope`` (infinity). Layers without vertical
tiling wrap in U and clamp in V, and every layer is alpha-composited from
black. The game also pulls the eye height toward the layer centre, which
is not reproduced. ``Parallax Scale`` stays exposed for tuning.
View-dependent offsets require ``inside_parallax`` from a resolved
Parallax skin manifest; stock shader depth parameters alone do not enable them.
"""

from __future__ import annotations

import os

try:
    import bpy
    import numpy as np
except Exception:  # pragma: no cover
    bpy = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

from ..spec import MaterialSpec, TextureRole
from ..images import load_image
from ._common import (
    new_material,
    add_output,
    add_principled,
    add_image_texture,
    find_texture,
    func_shader_for_role,
    build_funcshader_uv_chain,
    wire_rough_metal,
    normal_map_color,
)

# Game SelfIllum_Scale values (10–60) are tuned for its tonemapper; this
# maps them to a Blender emission strength that reads similarly next to the
# importer's sun (5.4) under AgX. The game additionally scales the emission
# by ``1 - BaseColorOp.a`` (≈0.65–0.7 on LED panels), applied per pixel.
_EMISSION_PER_SELF_ILLUM = 0.5
_DEFAULT_SELF_ILLUM_SCALE = 4.0
# ``Meters_UV_Parallax`` default of the Tech3 DispIn shaders.
_DEFAULT_METERS_UV_PARALLAX = 32.0


def _layers(spec: MaterialSpec) -> list[tuple[object, float, bool, str]]:
    """Return ``(TextureRef, depth, vertical_tiling, param_prefix)``, deepest first."""
    layers = []
    for tex in spec.textures:
        if tex.role.startswith("InsideLayer"):
            index = tex.role[len("InsideLayer"):]
            prefix = f"inside_layer{index}"
            depth = float(spec.params.get(f"{prefix}_depth", 1.0))
            vtile = bool(spec.params.get(f"{prefix}_vtile", 1.0))
            layers.append((tex, depth, vtile, prefix))
    if not layers:
        inside = find_texture(spec, "Inside")
        if inside is not None:
            layers.append((inside, 1.0, True, "inside"))
    layers.sort(key=lambda layer: -layer[1])
    return layers


def _foregrounds(spec: MaterialSpec) -> list[tuple[object, str]]:
    """Return ``(TextureRef, param_prefix)`` overlay layers, deepest first."""
    layers = []
    for tex in spec.textures:
        if tex.role.startswith("InsideForeground"):
            index = tex.role[len("InsideForeground"):]
            prefix = f"inside_fg{index}" if index else "inside_fg"
            layers.append((tex, float(spec.params.get(f"{prefix}_depth", 1.0)), prefix))
    layers.sort(key=lambda layer: -layer[1])
    return [(tex, prefix) for tex, _depth, prefix in layers]


def _layer_blend(spec: MaterialSpec, prefix: str):
    """Return ``(fill_rgba | None, alpha_from_luma)`` stored by the adapter."""
    luma = bool(spec.params.get(f"{prefix}_luma", 0.0))
    if f"{prefix}_fill_r" not in spec.params:
        return None, luma
    fill = tuple(float(spec.params[f"{prefix}_fill_{c}"]) for c in "rgb") + (1.0,)
    return fill, luma


def _math(tree, operation, a, b=None, *, location, label=""):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    if label:
        node.label = label
    for index, value in enumerate((a, b)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = float(value)
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs["Value"]


def _vector_math(tree, operation, a, b=None, *, location):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    node.location = location
    tree.links.new(a, node.inputs[0])
    if b is not None:
        tree.links.new(b, node.inputs[1])
    return node.outputs["Value" if operation == "DOT_PRODUCT" else "Vector"]


def _value(tree, value: float, *, label: str, location):
    node = tree.nodes.new("ShaderNodeValue")
    node.outputs[0].default_value = float(value)
    node.label = label
    node.name = label
    node.location = location
    return node.outputs[0]


def _mix(tree, blend: str, fac, color1, color2, *, location, label=""):
    node = tree.nodes.new("ShaderNodeMixRGB")
    node.blend_type = blend
    node.location = location
    if label:
        node.label = label
    if isinstance(fac, (int, float)):
        node.inputs["Fac"].default_value = float(fac)
    else:
        tree.links.new(fac, node.inputs["Fac"])
    for socket, value in ((node.inputs["Color1"], color1), (node.inputs["Color2"], color2)):
        if isinstance(value, tuple):
            socket.default_value = value
        else:
            tree.links.new(value, socket)
    return node.outputs["Color"]


def _parallax_base(tree):
    """Tangent-space view slope ``-(I·T, I·B) / (I·N)`` (view dependent)."""
    geo = tree.nodes.new("ShaderNodeNewGeometry")
    geo.location = (-2600, -900)
    tangent = tree.nodes.new("ShaderNodeTangent")
    tangent.direction_type = "UV_MAP"
    tangent.location = (-2600, -1150)
    incoming, normal = geo.outputs["Incoming"], geo.outputs["Normal"]
    tan = tangent.outputs["Tangent"]
    bitangent = _vector_math(tree, "CROSS_PRODUCT", normal, tan, location=(-2400, -1200))
    i_n = _vector_math(tree, "DOT_PRODUCT", incoming, normal, location=(-2400, -900))
    i_n = _math(tree, "MAXIMUM", i_n, 0.05, location=(-2200, -900))
    i_t = _vector_math(tree, "DOT_PRODUCT", incoming, tan, location=(-2400, -1000))
    i_b = _vector_math(tree, "DOT_PRODUCT", incoming, bitangent, location=(-2200, -1100))
    du = _math(tree, "DIVIDE", i_t, i_n, location=(-2000, -1000))
    du = _math(tree, "MULTIPLY", du, -1.0, location=(-1850, -1000))
    dv = _math(tree, "DIVIDE", i_b, i_n, location=(-2000, -1100))
    dv = _math(tree, "MULTIPLY", dv, -1.0, location=(-1850, -1100))
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (-1700, -1050)
    combine.label = "Parallax View Slope"
    tree.links.new(du, combine.inputs["X"])
    tree.links.new(dv, combine.inputs["Y"])
    return combine.outputs["Vector"]


def parallax_factors(depth: float, meters_uv_parallax: float, aspect: float) -> tuple[float, float]:
    """UV offset per unit of view slope for a layer ``depth`` metres deep.

    Game formula (``Tech3/Block_*_DispIn`` pixel shader):
    ``offset = slope × d / (S + d)`` with ``S = Meters_UV_Parallax ×
    (aspect, 1)``. Shallow layers move ``slope × d / S``; very deep layers
    saturate to ``slope`` and behave like infinity.
    """
    if depth <= 0.0:
        return 0.0, 0.0
    s_v = max(float(meters_uv_parallax), 1e-4)
    s_u = s_v * max(float(aspect), 1e-4)
    return depth / (s_u + depth), depth / (s_v + depth)


def _layer_vector(tree, uv, parallax, scale, factors, vtile: bool, height: int, *, location):
    vector = uv
    if parallax is not None and any(factors):
        node = tree.nodes.new("ShaderNodeVectorMath")
        node.operation = "MULTIPLY"
        node.label = "Layer Depth Factor"
        node.location = (location[0] - 450, location[1] - 40)
        node.inputs[1].default_value = (factors[0], factors[1], 0.0)
        tree.links.new(parallax, node.inputs[0])
        scaled = tree.nodes.new("ShaderNodeVectorMath")
        scaled.operation = "SCALE"
        scaled.location = (location[0] - 300, location[1] - 40)
        tree.links.new(node.outputs["Vector"], scaled.inputs[0])
        tree.links.new(scale, scaled.inputs["Scale"])
        vector = _vector_math(tree, "ADD", uv, scaled.outputs["Vector"], location=(location[0] - 150, location[1]))
    if not vtile:
        # U wraps (REPEAT); V is clamped to the edge texel centres like the game's sampler.
        sep = tree.nodes.new("ShaderNodeSeparateXYZ")
        sep.location = (location[0] - 120, location[1] - 200)
        tree.links.new(vector, sep.inputs["Vector"])
        half = 0.5 / max(int(height), 1)
        clamp = tree.nodes.new("ShaderNodeClamp")
        clamp.label = "Clamp V"
        clamp.location = (location[0] - 60, location[1] - 260)
        clamp.inputs["Min"].default_value = half
        clamp.inputs["Max"].default_value = 1.0 - half
        tree.links.new(sep.outputs["Y"], clamp.inputs["Value"])
        comb = tree.nodes.new("ShaderNodeCombineXYZ")
        comb.location = (location[0] - 20, location[1] - 200)
        tree.links.new(sep.outputs["X"], comb.inputs["X"])
        tree.links.new(clamp.outputs["Result"], comb.inputs["Y"])
        vector = comb.outputs["Vector"]
    return vector


def _image_tex(tree, ref, *, location, label):
    img = load_image(ref.path, TextureRole.EMISSIVE)
    if img is None:
        return None
    return add_image_texture(tree, img, location=location, label=label)


def _layer_color_alpha(tree, spec, prefix, tex, *, location):
    """Return ``(color, alpha, special)`` honouring the layer's blend suffix."""
    color, alpha = tex.outputs["Color"], tex.outputs["Alpha"]
    fill, luma = _layer_blend(spec, prefix)
    if luma:
        bw = tree.nodes.new("ShaderNodeRGBToBW")
        bw.location = (location[0] + 280, location[1] - 160)
        bw.label = "Alpha = Luminance"
        tree.links.new(color, bw.inputs["Color"])
        alpha = bw.outputs["Val"]
    if fill is not None:
        color = fill
    return color, alpha, luma or fill is not None


def _over(tree, inside, color, alpha, *, location, label):
    """Alpha-over ``color`` onto ``inside`` (onto black when nothing is below)."""
    if inside is None:
        return _mix(tree, "MULTIPLY", 1.0, color, alpha, location=location, label=label)
    return _mix(tree, "MIX", alpha, inside, color, location=location, label=label)


def _atlas_tile_image(img, cols: int, rows: int):
    """First (top-left) tile of an atlas as its own image; cached by name."""
    name = f"{img.name}__tile0_{cols}x{rows}"
    existing = bpy.data.images.get(name)
    if existing is not None:
        return existing
    width, height = img.size
    tw, th = width // cols, height // rows
    if tw <= 0 or th <= 0:
        return None
    channels = img.channels
    pixels = np.empty(width * height * channels, dtype=np.float32)
    img.pixels.foreach_get(pixels)
    tile = pixels.reshape(height, width, channels)[height - th:, :tw, :]
    rgba = np.ones((th, tw, 4), dtype=np.float32)
    rgba[:, :, :min(channels, 4)] = tile[:, :, :4]
    out = bpy.data.images.new(name, tw, th, alpha=True, float_buffer=False)
    out.colorspace_settings.name = img.colorspace_settings.name
    out.pixels.foreach_set(rgba.ravel())
    out.pack()
    return out


def _solid_preview(tree, tex, fs, *, location) -> None:
    """Make Solid view show one static atlas tile instead of the whole sheet.

    Solid view samples the material's active Image Texture node with the
    raw mesh UVs and ignores the animated mapping, so an unlinked node
    holding the first tile is made active. Rendering is unaffected.
    """
    kind, _layer, _period, _input, cols, rows = fs[:6]
    if kind != "Atlas" or cols * rows <= 1 or tex.image is None:
        return
    try:
        tile = _atlas_tile_image(tex.image, int(cols), int(rows))
    except Exception:  # noqa: BLE001 - preview only
        return
    if tile is None:
        return
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = tile
    node.label = "Solid View Preview"
    node.location = location
    tree.nodes.active = node


def _normal(tree, bsdf, ref, vector, spec):
    img = load_image(ref.path, TextureRole.NORMAL, flip_normal_green=spec.flip_normal_green)
    if img is None:
        return
    tex = add_image_texture(tree, img, location=(-900, -300), label="Normal")
    tree.links.new(vector, tex.inputs["Vector"])
    color = tex.outputs["Color"]
    if spec.flip_normal_green:
        # DispIn cell lenses use authored +Y normals; undo the image loader's
        # generic DirectX conversion without mutating a shared image.
        restore = tree.nodes.new("ShaderNodeVectorMath")
        restore.operation = "MULTIPLY_ADD"
        restore.label = "Authored Cell Normal Y"
        restore.location = (-900, -500)
        tree.links.new(color, restore.inputs[0])
        restore.inputs[1].default_value = (1.0, -1.0, 1.0)
        restore.inputs[2].default_value = (0.0, 1.0, 0.0)
        color = restore.outputs["Vector"]
    color = normal_map_color(tree, color, img, location=(-700, -300))
    nmap = tree.nodes.new("ShaderNodeNormalMap")
    nmap.location = (-200, -450)
    tree.links.new(color, nmap.inputs["Color"])
    tree.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return nmap.outputs["Normal"]


def _cell_relief_vector(tree, uv, normal, roughness, spec):
    """Refract through the cell lens, then intersect Height in eight steps."""
    ref = find_texture(spec, "Height")
    depth = float(spec.params.get("meters_depth", 0.2))
    if ref is None or depth <= 0.0:
        return uv
    image = load_image(ref.path, "Height")
    if image is None:
        return uv
    meters = (
        float(spec.params.get("meters_uv_u", 4.0)),
        float(spec.params.get("meters_uv_v", 4.0)),
    )
    if min(meters) <= 0.0:
        raise ValueError("Screen Meters_UV components must be positive")
    eta = float(spec.params.get("refract_index", 0.8))
    if eta <= 0.0:
        raise ValueError("Screen RefractIndex must be positive")
    geo = tree.nodes.new("ShaderNodeNewGeometry")
    geo.label = "LED Cell View"
    geo.location = (-2800, -2300)
    tangent = tree.nodes.new("ShaderNodeTangent")
    tangent.direction_type = "UV_MAP"
    tangent.location = (-2800, -2500)
    basis_normal = geo.outputs["Normal"]
    normal = normal if normal is not None else basis_normal
    bitangent = _vector_math(tree, "CROSS_PRODUCT", basis_normal, tangent.outputs["Tangent"],
                            location=(-2600, -2500))
    cosine = _vector_math(tree, "DOT_PRODUCT", geo.outputs["Incoming"], normal,
                          location=(-2600, -2300))
    cosine2 = _math(tree, "MULTIPLY", cosine, cosine, location=(-2400, -2300))
    sin2 = _math(tree, "SUBTRACT", 1.0, cosine2, location=(-2200, -2300))
    sin2 = _math(tree, "MULTIPLY", sin2, eta * eta, location=(-2000, -2300))
    k = _math(tree, "SUBTRACT", 1.0, sin2, location=(-1800, -2300))
    valid = _math(tree, "GREATER_THAN", k, 0.0, location=(-1600, -2450))
    root = _math(tree, "SQRT", _math(tree, "MAXIMUM", k, 0.0, location=(-1600, -2300)),
                 location=(-1400, -2300))
    correction = _math(tree, "SUBTRACT",
                       _math(tree, "MULTIPLY", cosine, eta, location=(-1600, -2200)),
                       root, location=(-1200, -2300))
    lens = _vector_math(tree, "SCALE", normal, location=(-1000, -2300))
    tree.links.new(correction, lens.node.inputs["Scale"])
    incident = _vector_math(tree, "SCALE", geo.outputs["Incoming"], location=(-1000, -2500))
    incident.node.inputs["Scale"].default_value = -eta
    ray = _vector_math(tree, "ADD", incident, lens, location=(-800, -2300))
    rough4 = _math(tree, "POWER", roughness, 4.0, location=(-800, -2600))
    amplitude = _math(tree, "SUBTRACT", 1.0, rough4, location=(-600, -2600))
    amplitude = _math(tree, "MULTIPLY", amplitude, valid, location=(-400, -2600))
    double = float(spec.params.get("double_refract", 1.0)) > 0.5
    if double:
        z = _vector_math(tree, "DOT_PRODUCT", ray, basis_normal, location=(-600, -2800))
        z2 = _math(tree, "MULTIPLY", z, z, location=(-400, -2800))
        sin2 = _math(tree, "SUBTRACT", 1.0, z2, location=(-200, -2800))
        second_k = _math(tree, "SUBTRACT", 1.0,
                         _math(tree, "MULTIPLY", sin2, 1.0 / (eta * eta),
                               location=(0, -2800)), location=(200, -2800))
        second_valid = _math(tree, "GREATER_THAN", second_k, 0.0, location=(400, -2800))
        amplitude = _math(tree, "MULTIPLY", amplitude, second_valid, location=(600, -2800))
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.label = "LED Relief Ray"
    combine.location = (-200, -2300)
    for axis, basis, size in (
        ("X", tangent.outputs["Tangent"], meters[0]), ("Y", bitangent, meters[1]),
    ):
        component = _vector_math(tree, "DOT_PRODUCT", ray, basis, location=(-600, -2300))
        component = _math(tree, "MULTIPLY", component, depth / size / (eta if double else 1.0),
                          location=(-400, -2300))
        component = _math(tree, "MULTIPLY", component, amplitude, location=(-200, -2500))
        tree.links.new(component, combine.inputs[axis])
    delta = combine.outputs["Vector"]

    def sample(vector, index):
        tex = add_image_texture(tree, image, location=(200 + index * 240, -2300),
                                label=f"LED Height {index}")
        tree.links.new(vector, tex.inputs["Vector"])
        sep = tree.nodes.new("ShaderNodeSeparateColor")
        tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
        return sep.outputs["Red"]

    current_uv, travel = uv, 0.0
    height = sample(current_uv, 0)
    previous_error = height
    for index in range(1, 9):
        loc = (index * 240, -2700)
        error = _math(tree, "SUBTRACT", height, travel, location=loc)
        active = _math(tree, "GREATER_THAN", error, 0.0, location=loc)
        # Freeze the intersection bracket after the first crossing.
        change = _math(tree, "SUBTRACT", error, previous_error, location=loc)
        change = _math(tree, "MULTIPLY", change, active, location=loc)
        previous_error = _math(tree, "ADD", previous_error, change, location=loc)
        step = _math(tree, "MULTIPLY", active, 0.125, location=loc)
        travel = _math(tree, "ADD", travel, step, location=loc)
        shift = _vector_math(tree, "SCALE", delta, location=loc)
        tree.links.new(travel, shift.node.inputs["Scale"])
        current_uv = _vector_math(tree, "ADD", uv, shift, location=loc)
        height = sample(current_uv, index)
    current_error = _math(tree, "SUBTRACT", height, travel, location=(2400, -2700))
    denominator = _math(tree, "SUBTRACT", previous_error, current_error, location=(2600, -2700))
    denominator = _math(tree, "MAXIMUM", denominator, 1e-6, location=(2800, -2700))
    fraction = _math(tree, "DIVIDE", previous_error, denominator, location=(3000, -2700))
    fraction.node.use_clamp = True
    back = _math(tree, "SUBTRACT", 1.0, fraction, location=(3200, -2700))
    back = _math(tree, "MULTIPLY", back, 0.125, location=(3400, -2700))
    travel = _math(tree, "MAXIMUM",
                   _math(tree, "SUBTRACT", travel, back, location=(3600, -2700)),
                   0.0, location=(3800, -2700))
    shift = _vector_math(tree, "SCALE", delta, location=(4000, -2700))
    tree.links.new(travel, shift.node.inputs["Scale"])
    result = _vector_math(tree, "ADD", uv, shift, location=(4200, -2700))
    result.node.label = "LED Cell Relief UV"
    return result


def build(spec: MaterialSpec):
    mat = new_material(spec.name_hint or "TM_Screen")
    tree = mat.node_tree
    links = tree.links

    bsdf = add_principled(tree, location=(300, 0))
    out = add_output(tree, location=(600, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    bsdf.inputs["Base Color"].default_value = spec.color

    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.location = (-2600, 0)
    uv = coord.outputs["UV"]
    panel = tree.nodes.new("ShaderNodeMapping")
    panel.location = (-1500, 200)
    panel.label = "TcScale_BRNH"
    panel.inputs["Scale"].default_value[0] = float(spec.params.get("tc_scale_u", 1.0))
    panel.inputs["Scale"].default_value[1] = float(spec.params.get("tc_scale_v", 1.0))
    links.new(uv, panel.inputs["Vector"])
    panel_uv = panel.outputs["Vector"]

    # ---- LED panel surface -------------------------------------------
    # BaseColorOp alpha is the panel opacity: the game premultiplies the
    # panel colour by it and lets the LEDs shine through ``1 - alpha``.
    panel_alpha = None
    diffuse = find_texture(spec, TextureRole.DIFFUSE)
    if diffuse is not None:
        img = load_image(diffuse.path, TextureRole.DIFFUSE)
        if img is not None:
            tex = add_image_texture(tree, img, location=(-900, 400), label="Diffuse")
            links.new(panel_uv, tex.inputs["Vector"])
            panel_alpha = tex.outputs["Alpha"]
            base = _mix(
                tree, "MULTIPLY", 1.0, tex.outputs["Color"], panel_alpha,
                location=(-500, 400), label="BaseColorOp × Alpha",
            )
            links.new(base, bsdf.inputs["Base Color"])
    rough = find_texture(spec, TextureRole.ROUGH_METAL)
    if rough is not None:
        img = load_image(rough.path, TextureRole.ROUGH_METAL)
        if img is not None:
            tex = wire_rough_metal(tree, bsdf, img, location=(-900, -100))
            links.new(panel_uv, tex.inputs["Vector"])
    normal = find_texture(spec, TextureRole.NORMAL)
    cell_normal = None
    if normal is not None:
        cell_normal = _normal(tree, bsdf, normal, panel_uv, spec)

    # ---- Inside image (parallax layers) ------------------------------
    layers = _layers(spec)
    meters_uv_parallax = float(spec.params.get("meters_uv_parallax", _DEFAULT_METERS_UV_PARALLAX))
    parallax = None
    scale = None
    if spec.params.get("inside_parallax", 0.0) and any(
        ref.role.startswith("InsideLayer") and depth > 0.0
        for ref, depth, _vtile, _prefix in layers
    ):
        parallax = _parallax_base(tree)
        scale = _value(tree, 1.0, label="Parallax Scale", location=(-2000, -1300))

    inside = None
    y = -700
    for ref, depth, vtile, prefix in layers:
        location = (-900, y)
        tex = _image_tex(tree, ref, location=location, label=ref.role)
        y -= 320
        if tex is None:
            continue
        tex.extension = "REPEAT"
        width, height = (tuple(tex.image.size) + (0, 0))[:2] if tex.image is not None else (0, 0)
        aspect = width / height if width > 0 and height > 0 else 1.0
        factors = parallax_factors(depth, meters_uv_parallax, aspect) if parallax is not None else (0.0, 0.0)
        vector = _layer_vector(
            tree, uv, parallax, scale, factors, vtile, height, location=location,
        )
        fs = func_shader_for_role(spec, "Inside") if ref.role == "Inside" else None
        if fs is not None:
            anim = build_funcshader_uv_chain(
                tree, fs, location=(location[0] - 900, location[1] + 100), label_suffix="Inside",
            )
            links.new(vector, anim.node.inputs["Vector"])
            vector = anim
            _solid_preview(tree, tex, fs, location=(location[0] + 250, location[1] - 150))
        links.new(vector, tex.inputs["Vector"])
        color, alpha, special = _layer_color_alpha(tree, spec, prefix, tex, location=location)
        # Parallax layers composite from black (``lerp(rgb, layer, alpha)``) like the game.
        if inside is None and not special and parallax is None:
            inside = tex.outputs["Color"]
        else:
            inside = _over(
                tree, inside, color, alpha, location=(-500, y + 320), label=f"Over {ref.role}",
            )

    for ref, prefix in _foregrounds(spec):
        tex = _image_tex(tree, ref, location=(-900, y), label=ref.role)
        y -= 320
        if tex is None:
            continue
        links.new(uv, tex.inputs["Vector"])
        color, alpha, _special = _layer_color_alpha(tree, spec, prefix, tex, location=(-900, y + 320))
        inside = _over(tree, inside, color, alpha, location=(-500, y + 320), label=f"Over {ref.role}")

    # ---- Emission = inside × (1 - panel alpha) × LED mask × strength --
    em_socket = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
    if inside is not None and em_socket is not None:
        if panel_alpha is not None:
            see_through = _math(
                tree, "SUBTRACT", 1.0, panel_alpha, location=(-500, 150), label="1 - BaseColorOp Alpha",
            )
            inside = _mix(
                tree, "MULTIPLY", 1.0, inside, see_through,
                location=(-350, -100), label="Inside × (1 - Alpha)",
            )
        mask = find_texture(spec, "SelfIllumMask")
        if mask is not None:
            img = load_image(mask.path, TextureRole.EMISSIVE)
            if img is not None:
                tex = add_image_texture(tree, img, location=(-900, 0), label="SelfIllumMask")
                roughness = bsdf.inputs["Roughness"]
                roughness = (roughness.links[0].from_socket if roughness.is_linked
                             else float(roughness.default_value))
                relief_uv = _cell_relief_vector(tree, panel_uv, cell_normal, roughness, spec)
                links.new(relief_uv, tex.inputs["Vector"])
                inside = _mix(
                    tree, "MULTIPLY", 1.0, inside, tex.outputs["Color"],
                    location=(-200, -200), label="Inside × SI",
                )
        links.new(inside, em_socket)
        if "Emission Strength" in bsdf.inputs:
            strength = _value(
                tree,
                float(spec.params.get("self_illum_scale", _DEFAULT_SELF_ILLUM_SCALE))
                * _EMISSION_PER_SELF_ILLUM,
                label="Screen Emission Strength",
                location=(0, -600),
            )
            links.new(strength, bsdf.inputs["Emission Strength"])

    return mat


__all__ = ["build"]
