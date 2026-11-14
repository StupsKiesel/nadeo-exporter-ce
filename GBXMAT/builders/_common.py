"""Shared helpers for builders.

Tiny utilities for the family builders so each builder doesn't reinvent
material/node-tree boilerplate.
"""

from __future__ import annotations

try:
    import bpy
except Exception:  # pragma: no cover
    bpy = None  # type: ignore[assignment]


def new_material(name_hint: str):
    """Create a fresh ``bpy.types.Material`` with nodes enabled and the
    default Principled-BSDF graph cleared.

    Builders own their node tree from scratch — the default graph is
    convenient for hand-editing but a hindrance for programmatic
    construction.
    """
    mat = bpy.data.materials.new(name=name_hint)
    mat.use_nodes = True
    tree = mat.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    return mat


def add_output(tree, location=(600, 0)):
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    out.location = location
    return out


def add_principled(tree, location=(300, 0)):
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = location
    return bsdf


def add_image_texture(tree, image, location=(-300, 0), label: str = ""):
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    if image.source == "MOVIE":
        user = tex.image_user
        user.frame_duration = max(int(image.frame_duration), 1)
        user.use_cyclic = True
        user.use_auto_refresh = True
    tex.location = location
    if label:
        tex.label = label
        tex.name = label
    return tex


def find_texture(spec, role: str):
    """Return the first :class:`TextureRef` matching ``role``, or ``None``."""
    for tex in spec.textures:
        if tex.role == role:
            return tex
    return None


def wire_rough_metal(tree, bsdf, image, *, location=(-600, -400),
                     vector_socket=None) -> None:
    """Wire a TM RoughMetal-packed texture into ``bsdf``.

    Nadeo's texture list documents ``_R`` as red = roughness and
    green = metallic; both channels are wired directly (non-colour data).

    ``vector_socket`` lets builders share an existing UV / mapping
    output (for triplanar BOX projection); when ``None`` the texture
    uses default UVs.
    """
    tex = add_image_texture(tree, image, location=location, label="RoughMetal")
    if vector_socket is not None:
        tex.projection = "BOX"
        try:
            tex.projection_blend = 0.25
        except Exception:  # noqa: BLE001
            pass
        tree.links.new(vector_socket, tex.inputs["Vector"])

    sep = tree.nodes.new("ShaderNodeSeparateColor")
    sep.location = (location[0] + 220, location[1])
    tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
    tree.links.new(sep.outputs["Red"], bsdf.inputs["Roughness"])

    # BC4 sources decode as L, so their green channel repeats roughness.
    if int(image.get("source_channels", 4) or 4) >= 2:
        tree.links.new(sep.outputs["Green"], bsdf.inputs["Metallic"])
    return tex


def normal_map_color(tree, color, image, *, location=(-450, -200)):
    """Return a Normal Map colour for ``image``, rebuilding Z for BC5 sources.

    Two-channel (BC5) normal maps decode with blue = 0, which a Normal Map
    node reads as a normal pointing into the surface: reflections then
    turn into near-mirrors. Z is rebuilt as ``sqrt(1 - x² - y²)`` from the
    signed X/Y; full RGB(A) sources are returned unchanged.
    """
    if int(image.get("source_channels", 4) or 4) > 3:
        return color
    nodes, links = tree.nodes, tree.links
    x0, y0 = location

    def math(op, a, b=None, c=None, dx=0):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        n.location = (x0 + dx, y0)
        n.hide = True
        for i, v in enumerate((a, b, c)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs[0]

    sep = nodes.new("ShaderNodeSeparateColor")
    sep.location = (x0 - 200, y0)
    links.new(color, sep.inputs["Color"])
    x = math("MULTIPLY_ADD", sep.outputs["Red"], 2.0, -1.0, dx=0)
    y = math("MULTIPLY_ADD", sep.outputs["Green"], 2.0, -1.0, dx=0)
    rest = math("SUBTRACT", 1.0, math("ADD", math("MULTIPLY", x, x, dx=100), math("MULTIPLY", y, y, dx=100), dx=150), dx=200)
    rest.node.use_clamp = True
    z = math("MULTIPLY_ADD", math("SQRT", rest, dx=250), 0.5, 0.5, dx=300)
    comb = nodes.new("ShaderNodeCombineColor")
    comb.location = (x0 + 350, y0)
    links.new(sep.outputs["Red"], comb.inputs["Red"])
    links.new(sep.outputs["Green"], comb.inputs["Green"])
    links.new(z, comb.inputs["Blue"])
    return comb.outputs["Color"]

# Placement custom property holding the MapElemColor index (0 = Default).
HUE_INDEX_ATTRIBUTE = "tm_color_index"


def apply_hue_recolour(tree, spec, base_socket, *, source_tex=None, location=(-900, 700), mask_role="HueMask"):
    """Recolour ``base_socket`` toward the placement's palette colour.

    Implements the TM2020 hue-mask pixel shader (reverse engineered in
    vjeux/trackmania-tas ``paktables.rs``) with ``m`` = HueMask texel and
    ``T`` = the linear palette target::

        k     = max(m.g - (m.r + m.b) / 2, 0)
        recol = saturate((m.g - k) * mean(T) + k * T)
        out   = mix(base, recol, m.a)

    ``T`` is chosen per instance from the five ``hue_target{i}_{r,g,b}``
    params by the instancer's :data:`HUE_INDEX_ATTRIBUTE`; index 0 keeps
    ``base`` unchanged unless a ``hue_target0_*`` target is given. Returns
    ``base_socket`` untouched when the spec has no hue mask or targets.
    ``mask_role`` selects a per-projection mask when used by terrain shaders.
    """
    from ..images import load_image

    params = spec.params or {}
    ref = find_texture(spec, mask_role)
    if ref is None or "hue_target1_r" not in params:
        return base_socket
    img = load_image(ref.path, "HueMask")
    if img is None:
        return base_socket
    nodes, links = tree.nodes, tree.links
    x, y = location

    def node(kind, dx, dy, **props):
        n = nodes.new(kind)
        n.location = (x + dx, y + dy)
        for key, value in props.items():
            setattr(n, key, value)
        return n

    def math(op, a, b, dx, dy):
        n = node("ShaderNodeMath", dx, dy, operation=op)
        for i, v in enumerate((a, b)):
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs[0]

    def vmath(op, a, b, dx, dy):
        n = node("ShaderNodeVectorMath", dx, dy, operation=op)
        for i, v in enumerate((a, b)):
            if isinstance(v, tuple):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs["Vector"] if op != "DOT_PRODUCT" else n.outputs["Value"]

    def mix(fac, a, b, dx, dy, label=""):
        n = node("ShaderNodeMixRGB", dx, dy, blend_type="MIX", label=label)
        links.new(fac, n.inputs["Fac"])
        for sock, v in ((n.inputs["Color1"], a), (n.inputs["Color2"], b)):
            if isinstance(v, tuple):
                sock.default_value = v
            else:
                links.new(v, sock)
        return n.outputs["Color"]

    attr = node(
        "ShaderNodeAttribute", 0, 0, attribute_type="INSTANCER",
        attribute_name=HUE_INDEX_ATTRIBUTE, label="Map element colour",
    )
    index = attr.outputs["Fac"]
    # An explicit ``hue_target0_*`` recolours Default placements too;
    # otherwise index 0 keeps the base colour.
    recolour_default = "hue_target0_r" in params
    first = 0 if recolour_default else 1
    targets = [
        tuple(float(params.get(f"hue_target{i}_{c}", 0.0)) for c in "rgb") + (1.0,)
        for i in range(first, 6)
    ]
    target = None
    for i, rgb in enumerate(targets, start=first):
        if target is None:
            rgb_node = node("ShaderNodeRGB", 200, 200, label=f"Palette target {i}")
            rgb_node.outputs[0].default_value = rgb
            target = rgb_node.outputs[0]
            continue
        is_i = math("COMPARE", index, float(i), 200 * i, -200)
        is_i.node.inputs[2].default_value = 0.5
        target = mix(is_i, target, rgb, 200 * i, 0, label=f"Palette target {i}")

    tex = add_image_texture(tree, img, location=(x, y - 400), label=mask_role)
    if source_tex is not None and source_tex.inputs["Vector"].is_linked:
        links.new(source_tex.inputs["Vector"].links[0].from_socket, tex.inputs["Vector"])
    sep = node("ShaderNodeSeparateColor", 300, -400)
    links.new(tex.outputs["Color"], sep.inputs["Color"])
    r, g, b = sep.outputs[0], sep.outputs[1], sep.outputs[2]
    k = math("MAXIMUM", math("SUBTRACT", g, math("MULTIPLY", math("ADD", r, b, 500, -500), 0.5, 700, -500), 900, -450), 0.0, 1100, -450)
    mean_t = vmath("DOT_PRODUCT", target, (1 / 3, 1 / 3, 1 / 3), 1300, -100)
    grey = math("MULTIPLY", math("SUBTRACT", g, k, 1300, -400), mean_t, 1500, -300)
    recol = vmath("ADD", _scale(tree, target, k, (x + 1500, y - 50)), grey, 1700, -100)
    recol = vmath("MAXIMUM", vmath("MINIMUM", recol, (1.0, 1.0, 1.0), 1900, -100), (0.0, 0.0, 0.0), 2100, -100)
    fac = tex.outputs["Alpha"]
    if not recolour_default:
        fac = math("MULTIPLY", fac, math("GREATER_THAN", index, 0.5, 1900, -400), 2100, -400)
    base = base_socket if base_socket is not None else tuple(spec.color)
    return mix(fac, base, recol, 2300, -200, label="Hue recolour")


def _scale(tree, vector, scale, location):
    n = tree.nodes.new("ShaderNodeVectorMath")
    n.operation = "SCALE"
    n.location = location
    tree.links.new(vector, n.inputs[0])
    tree.links.new(scale, n.inputs["Scale"])
    return n.outputs["Vector"]


GRASS_X2_ROLE = "GrassX2"


def grass_x2_coefficients(spec):
    """Blender ``(u, v)`` of the GrassX2 overlay as affine functions of world XY.

    Rotate/scale the world projection, converting TM ``(x, z)`` to Blender
    ``(x, -y)`` and inverting the V direction for decoded images. The wrapper
    offsets are sampler translations, added after that conversion: do not
    invert ``offset_v`` along with the projected coordinate. Stadium's
    1/1024 scale, 0.25 offsets and 180-degree rotation place logo rows at
    TM Z = 256 + 1024*k, not through the playable area's centre.
    Returns ``((a, b, c), (d, e, f))`` with ``u = a*x + b*y + c`` and
    ``v = d*x + e*y + f``, or ``None`` without a transform.
    """
    import math as _math

    params = spec.params
    if "grass_x2_scale_u" not in params:
        return None
    su = float(params["grass_x2_scale_u"])
    sv = float(params.get("grass_x2_scale_v", su))
    ou = float(params.get("grass_x2_offset_u", 0.0))
    ov = float(params.get("grass_x2_offset_v", 0.0))
    theta = _math.radians(float(params.get("grass_x2_rotation", 0.0)))
    cos, sin = _math.cos(theta), _math.sin(theta)
    # a = su*x_bl, b = -sv*y_bl; tc_u = cos*a - sin*b + ou; tc_v = sin*a + cos*b + ov.
    u = (su * cos, sv * sin, ou)
    v = (-su * sin, sv * cos, ov)
    return u, v


def apply_grass_x2(tree, spec, color_socket, *, location=(-900, 900)):
    """Multiply ``color_socket`` by ``2 * GrassX2`` projected in world XY.

    Mirrors ``mDiffuse *= 2*mPyGrassX2`` of the Tech3 GrassX2 shaders (the
    Stadium floor's mowing stripes and logo). Returns the new socket, or
    ``color_socket`` unchanged when the texture or its transform is absent.
    """
    from ..images import load_image

    ref = find_texture(spec, GRASS_X2_ROLE)
    coeffs = grass_x2_coefficients(spec)
    if ref is None or coeffs is None or color_socket is None:
        return color_socket
    image = load_image(ref.path, GRASS_X2_ROLE)
    if image is None:
        return color_socket
    links = tree.links
    x0, y0 = location
    geom = tree.nodes.new("ShaderNodeNewGeometry")
    geom.location = (x0, y0)
    coords = []
    for row, (a, b, c) in enumerate(coeffs):
        dot = tree.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        dot.location = (x0 + 200, y0 - 160 * row)
        links.new(geom.outputs["Position"], dot.inputs[0])
        dot.inputs[1].default_value = (a, b, 0.0)
        add = tree.nodes.new("ShaderNodeMath")
        add.operation = "ADD"
        add.location = (x0 + 400, y0 - 160 * row)
        links.new(dot.outputs["Value"], add.inputs[0])
        add.inputs[1].default_value = c
        coords.append(add.outputs[0])
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (x0 + 600, y0)
    links.new(coords[0], combine.inputs[0])
    links.new(coords[1], combine.inputs[1])
    tex = add_image_texture(tree, image, location=(x0 + 800, y0), label="GrassX2")
    links.new(combine.outputs[0], tex.inputs["Vector"])
    gain = tree.nodes.new("ShaderNodeVectorMath")
    gain.operation = "SCALE"
    gain.location = (x0 + 1100, y0)
    gain.inputs["Scale"].default_value = 2.0
    links.new(tex.outputs["Color"], gain.inputs[0])
    mul = tree.nodes.new("ShaderNodeVectorMath")
    mul.operation = "MULTIPLY"
    mul.location = (x0 + 1300, y0)
    mul.label = "Diffuse × 2·GrassX2"
    links.new(color_socket, mul.inputs[0])
    links.new(gain.outputs[0], mul.inputs[1])
    return mul.outputs[0]

__all__ = [
    "new_material",
    "add_output",
    "add_principled",
    "add_image_texture",
    "find_texture",
    "wire_rough_metal",
    "apply_hue_recolour",
    "apply_grass_x2",
    "grass_x2_coefficients",
    "GRASS_X2_ROLE",
    "HUE_INDEX_ATTRIBUTE",
    "func_shader_for_role",
    "build_funcshader_uv_chain",
    "attach_funcshader_uv",
]


# ---------------------------------------------------------------------------
# FuncShader helpers
# ---------------------------------------------------------------------------

# TM material-slot layer name -> texture role(s) the animation drives.
# Multiple roles map to the same layer when an animation should affect
# all maps (e.g. "BaseColorA" includes diffuse + its bound normal).
_LAYER_TO_ROLES: dict[str, tuple[str, ...]] = {
    "BaseColor": ("Diffuse", "Normal", "RoughMetal"),
    "BaseColorA": ("Diffuse", "Normal", "RoughMetal"),
    "BaseColorB": ("Diffuse",),
    "Diffuse": ("Diffuse", "Normal", "RoughMetal"),
    "Normal": ("Normal",),
    "SelfIllum": ("Emissive",),
    "Emissive": ("Emissive",),
    "Lightmap": ("Lightmap",),
    "Detail": ("Detail",),
    "MulInside": ("Emissive", "Inside"),    # masked emissive (Chrono, screens)
    "Clouds": ("Diffuse",),
}


def func_shader_for_role(spec, role: str):
    """Return the first FuncShader tuple driving ``role``, or ``None``.

    Falls back to the *first* animation when none of the layer-name
    mappings match — covers the common case of a material with one
    animation and one diffuse slot.
    """
    fs_list = getattr(spec, "func_shaders", None) or ()
    if not fs_list:
        return None
    for fs in fs_list:
        layer = fs[1]
        roles = _LAYER_TO_ROLES.get(layer, ())
        if role in roles:
            return fs
    if len(fs_list) == 1:
        # Single animation, no explicit layer match — fall back only for
        # the most common animated slots so we don't accidentally drive
        # unrelated roles (e.g. Lightmap UV2). Diffuse and Emissive are
        # the only slots TM materials commonly animate without a layer
        # tag the table already covers.
        if role in ("Diffuse", "Emissive"):
            return fs_list[0]
    return None


def _func_shaders_for_role(spec, role: str):
    matches = tuple(
        fs for fs in spec.func_shaders
        if role in _LAYER_TO_ROLES.get(fs[1], ())
    )
    if matches:
        return matches
    first = func_shader_for_role(spec, role)
    return (first,) if first is not None else ()


def _drive_mapping_location(mapping, axis: int, expression: str) -> None:
    driver = mapping.inputs["Location"].driver_add("default_value", axis).driver
    driver.type = "SCRIPTED"
    for name, path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
        variable = driver.variables.new()
        variable.name = name
        variable.type = "CONTEXT_PROP"
        variable.targets[0].context_property = "ACTIVE_SCENE"
        variable.targets[0].data_path = path
    driver.expression = expression


def build_funcshader_uv_chain(tree, fs_tuple, *, location=(-1100, 0),
                              label_suffix: str = "", input_vector=None):
    """Build a TexCoord -> Mapping (-> animated frame transform) chain.

    Returns the final ``Vector`` output socket the caller should feed
    into a texture node. Handles three FuncShader kinds:

    * ``Atlas`` — Mapping with scale ``(1/cols, 1/rows)`` and stepped
      translation through ``frames`` tiles per ``period`` seconds.
      Timeline frame zero is time zero; scene FPS including ``fps_base``
      determines elapsed seconds.
    * ``UvScroll`` — Mapping with scale + a Blender driver on
      ``Location.X`` / ``Location.Y`` evaluating
      ``(frame / fps) * speed / period``.
    * ``Composite`` is flattened upstream, so callers see leaves only.

    ``fs_tuple`` is the spec encoding produced by
    :func:`_gbxmat_adapter._flatten_func_shader`.
    """
    (kind, layer, period, _input,
     cols, rows, frames,
     off_x, off_y, scl_x, scl_y, spd_x, spd_y) = fs_tuple

    if input_vector is None:
        coord = tree.nodes.new("ShaderNodeTexCoord")
        coord.location = location
        input_vector = coord.outputs["UV"]
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.location = (location[0] + 220, location[1])
    if label_suffix:
        mapping.label = f"FuncShader {kind} {label_suffix}"
    else:
        mapping.label = f"FuncShader {kind}"
    tree.links.new(input_vector, mapping.inputs["Vector"])

    if kind == "Atlas" and cols > 0 and rows > 0:
        # Frame 0 = top-left tile. UV-mapping convention: scale shrinks
        # the UV span, then we translate so the visible tile is the
        # top row. (TM atlases are packed top-to-bottom.)
        mapping.inputs["Scale"].default_value[0] = 1.0 / max(cols, 1)
        mapping.inputs["Scale"].default_value[1] = 1.0 / max(rows, 1)
        # Top row in V means y-translation = (rows - 1) / rows.
        mapping.inputs["Location"].default_value[0] = 0.0
        mapping.inputs["Location"].default_value[1] = (rows - 1) / float(rows)
        if frames > 1 and period > 0:
            if frames > cols * rows:
                raise ValueError(f"Atlas has {frames} frames but only {cols * rows} tiles")
            # Discard float32 storage noise (0.10000000149 -> 0.1) before
            # it accumulates into clock drift. Epsilon handles exact edges.
            cycle = format(period, ".7g")
            rate = frames / float(cycle)
            # Blender evaluates only "simple expressions" without Python
            # auto-run, and those support fmod() but not the % operator.
            tile = f"fmod(floor(max(frame, 0) * (fps_base / fps) * {rate!r} + 1e-6), {frames})"
            expressions = (
                f"fmod({tile}, {cols}) / {cols}",
                f"({rows - 1} - floor({tile} / {cols})) / {rows}",
            )
            for axis, expression in enumerate(expressions):
                _drive_mapping_location(mapping, axis, expression)
    elif kind == "UvScroll":
        if scl_x or scl_y:
            mapping.inputs["Scale"].default_value[0] = float(scl_x) if scl_x else 1.0
            mapping.inputs["Scale"].default_value[1] = float(scl_y) if scl_y else 1.0
        mapping.inputs["Location"].default_value[0] = float(off_x)
        mapping.inputs["Location"].default_value[1] = float(off_y)
        # Drive Location with a frame-based expression.
        # period <= 0 would divide by zero; guard.
        per = float(period) if period and period > 0.0 else 1.0
        for axis_index, speed in ((0, spd_x), (1, spd_y)):
            if not speed:
                continue
            _drive_mapping_location(
                mapping, axis_index,
                f"{float(off_x) if axis_index == 0 else float(off_y)!r}"
                f" + frame * (fps_base / fps) * ({float(speed)!r} / {float(format(per, '.7g'))!r})",
            )

    return mapping.outputs["Vector"]


def attach_funcshader_uv(tree, tex_node, spec, role: str,
                         *, location=None) -> bool:
    """Build a FuncShader UV chain for ``role`` and link it to ``tex_node``.

    No-op (returns ``False``) when the spec carries no animation that
    affects ``role``. Returns ``True`` when a chain was built and
    connected. Safe to call after :func:`add_image_texture` regardless of
    whether the material is animated — animated specs produce a chain,
    static ones leave the texture on default UVs.
    """
    shaders = _func_shaders_for_role(spec, role)
    if not shaders:
        return False
    if location is None:
        # Place the chain to the upper-left of the texture node.
        loc = getattr(tex_node, "location", None)
        if loc is not None:
            location = (float(loc[0]) - 500.0, float(loc[1]) + 60.0)
        else:
            location = (-1100.0, 0.0)
    out_socket = None
    # Scroll within the selected tile, then place it in the atlas. Applying
    # a scroll after atlas placement would drift through unrelated tiles.
    ordered = sorted(shaders, key=lambda fs: fs[0] == "Atlas")
    for index, fs in enumerate(ordered):
        if fs[0] == "Atlas" and out_socket is not None:
            repeat = tree.nodes.new("ShaderNodeVectorMath")
            repeat.operation = "FRACTION"
            repeat.label = f"FuncShader Tile Repeat {role}"
            repeat.location = (location[0] + 220 * index, location[1] - 180)
            tree.links.new(out_socket, repeat.inputs[0])
            out_socket = repeat.outputs["Vector"]
        out_socket = build_funcshader_uv_chain(
            tree, fs, location=(location[0] + 220 * index, location[1]),
            label_suffix=role, input_vector=out_socket,
        )
    if "Vector" in tex_node.inputs:
        tree.links.new(out_socket, tex_node.inputs["Vector"])
        return True
    return False
