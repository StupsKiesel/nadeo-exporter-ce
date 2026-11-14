"""GBXMAT manual material spec UI.

A 3D-viewport sidebar panel that lets the user construct a
:class:`GBXMAT.MaterialSpec` by hand — pick a shader family, type a few
texture paths and roles, hit Build. Useful for iterating on the GBXMAT
builders without driving a full map import each time.

Self-contained: registered from the addon ``__init__`` via :data:`CLASSES`.
"""

from __future__ import annotations

import os
from typing import Iterable

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from . import ADDON_SHORT_NAME


# ---------------------------------------------------------------------------
# GBXMAT lookup helpers (deferred so import errors don't break addon load).
# ---------------------------------------------------------------------------

def _gbxmat():
    """Return the imported ``GBXMAT`` module or ``None`` if unavailable."""
    try:
        import GBXMAT  # noqa: WPS433 - intentional late import
    except Exception:  # noqa: BLE001
        return None
    return GBXMAT


def _family_items(_self, _context):
    """Enum-property item callback listing all registered GBXMAT families."""
    g = _gbxmat()
    if g is None:
        return [("", "GBXMAT unavailable", "GBXMAT module failed to import")]
    families = list(g.list_supported_families()) or []
    if not families:
        return [("", "No families", "No GBXMAT families are registered")]
    # (identifier, name, description) — keep identifier == family name so
    # we can pass it straight into ``MaterialSpec.family``.
    return [(name, name, f"Build a {name} material") for name in families]


_DEFAULT_ROLES = (
    "Diffuse",
    "Normal",
    "RoughMetal",
    "Detail",
    "Lightmap",
    "Emissive",
    "Occlusion",
    "HueMask",
    "Specular",
    "Mask",
)


def _role_items(_self, _context):
    return [(r, r, f"{r} texture role") for r in _DEFAULT_ROLES]


# ---------------------------------------------------------------------------
# PropertyGroups
# ---------------------------------------------------------------------------

class TMBRIDGE_PG_GBXMatTexture(PropertyGroup):
    role: EnumProperty(
        name="Role",
        description="GBXMAT texture role",
        items=_role_items,
    )
    custom_role: StringProperty(
        name="Custom Role",
        description="Used instead of Role when non-empty",
        default="",
    )
    path: StringProperty(
        name="Path",
        description="Absolute path to the texture file (.dds, .png, ...)",
        subtype="FILE_PATH",
        default="",
    )


class TMBRIDGE_PG_GBXMatBuilder(PropertyGroup):
    family: EnumProperty(
        name="Family",
        description="GBXMAT shader family to build",
        items=_family_items,
    )
    name_hint: StringProperty(
        name="Name",
        description="Base name for the resulting Material datablock",
        default="TM_Manual",
    )
    color: FloatVectorProperty(
        name="Color",
        description="Base color (RGBA)",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
    )
    opacity_from_diffuse_alpha: BoolProperty(
        name="Opacity from Diffuse Alpha",
        default=False,
    )
    alpha_cutout: BoolProperty(
        name="Alpha Cutout (Fence/Vegetation)",
        default=False,
    )
    flip_normal_green: BoolProperty(
        name="Flip Normal Green (DirectX→OpenGL Y)",
        default=True,
    )
    physics_id: StringProperty(name="Physics Id", default="")
    gameplay_id: StringProperty(name="Gameplay Id", default="")
    assign_to_active: BoolProperty(
        name="Assign to active object slot",
        description=(
            "After building, replace the active material slot of the "
            "active mesh object with the new material"
        ),
        default=True,
    )

    textures: CollectionProperty(type=TMBRIDGE_PG_GBXMatTexture)
    active_texture_index: IntProperty(default=0)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class TMBRIDGE_OT_GBXMatTextureAdd(Operator):
    bl_idname = "trackmania_blender_bridge.gbxmat_texture_add"
    bl_label = "Add Texture"
    bl_description = "Append a new texture row"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        builder = context.scene.tmbridge_gbxmat
        item = builder.textures.add()
        item.role = "Diffuse"
        builder.active_texture_index = len(builder.textures) - 1
        return {"FINISHED"}


class TMBRIDGE_OT_GBXMatTextureRemove(Operator):
    bl_idname = "trackmania_blender_bridge.gbxmat_texture_remove"
    bl_label = "Remove Texture"
    bl_description = "Remove the selected texture row"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)

    def execute(self, context):
        builder = context.scene.tmbridge_gbxmat
        idx = self.index if self.index >= 0 else builder.active_texture_index
        if 0 <= idx < len(builder.textures):
            builder.textures.remove(idx)
            builder.active_texture_index = max(0, min(idx, len(builder.textures) - 1))
        return {"FINISHED"}


class TMBRIDGE_OT_GBXMatTextureClear(Operator):
    bl_idname = "trackmania_blender_bridge.gbxmat_texture_clear"
    bl_label = "Clear All Textures"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.tmbridge_gbxmat.textures.clear()
        return {"FINISHED"}


def _collect_texture_refs(builder, gbxmat_module) -> list:
    refs = []
    TextureRef = gbxmat_module.TextureRef
    for entry in builder.textures:
        path = bpy.path.abspath(entry.path or "").strip()
        if not path:
            continue
        role = (entry.custom_role or entry.role or "Diffuse").strip()
        if not role:
            role = "Diffuse"
        refs.append(TextureRef(role=role, path=path))
    return refs


class TMBRIDGE_OT_GBXMatBuild(Operator):
    bl_idname = "trackmania_blender_bridge.gbxmat_build"
    bl_label = "Build Material"
    bl_description = "Construct a MaterialSpec from the panel and run GBXMAT"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        g = _gbxmat()
        if g is None:
            self.report({"ERROR"}, "GBXMAT module is not available")
            return {"CANCELLED"}

        builder = context.scene.tmbridge_gbxmat
        family = (builder.family or "").strip()
        if not family:
            self.report({"ERROR"}, "Pick a shader family first")
            return {"CANCELLED"}

        # Validate texture paths early so the user gets feedback in one go.
        missing = []
        for entry in builder.textures:
            path = bpy.path.abspath(entry.path or "").strip()
            if not path:
                continue
            if not os.path.isfile(path):
                missing.append(path)
        if missing:
            self.report(
                {"ERROR"},
                f"{len(missing)} texture path(s) not found, e.g. {missing[0]}",
            )
            return {"CANCELLED"}

        spec = g.MaterialSpec(
            family=family,
            name_hint=builder.name_hint or "TM_Manual",
            color=tuple(builder.color),
            textures=tuple(_collect_texture_refs(builder, g)),
            physics_id=builder.physics_id,
            gameplay_id=builder.gameplay_id,
            flip_normal_green=builder.flip_normal_green,
            opacity_from_diffuse_alpha=builder.opacity_from_diffuse_alpha,
            alpha_cutout=builder.alpha_cutout,
        )

        try:
            from ._blendermania import get_or_create_material
            mat = get_or_create_material(spec)
        except Exception as exc:  # noqa: BLE001 - surface to UI
            self.report({"ERROR"}, f"GBXMAT build failed: {exc}")
            return {"CANCELLED"}

        if mat is None:
            self.report({"ERROR"}, "GBXMAT returned no material")
            return {"CANCELLED"}

        if builder.assign_to_active:
            obj = context.active_object
            if obj is not None and obj.type == "MESH":
                if not obj.material_slots:
                    obj.data.materials.append(mat)
                else:
                    slot_idx = max(0, obj.active_material_index)
                    obj.data.materials[slot_idx] = mat

        self.report({"INFO"}, f"Built material '{mat.name}'")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI list + Panel
# ---------------------------------------------------------------------------

class TMBRIDGE_UL_GBXMatTextures(UIList):
    def draw_item(
        self, _context, layout, _data, item, _icon, _active_data, _active_propname
    ):
        row = layout.row(align=True)
        role_label = item.custom_role or item.role or "Diffuse"
        row.label(text=role_label, icon="TEXTURE")
        basename = os.path.basename(item.path) if item.path else "<empty>"
        row.label(text=basename)


class TMBRIDGE_PT_GBXMatPanel(Panel):
    bl_idname = "TMBRIDGE_PT_gbxmat_panel"
    bl_label = "GBXMAT"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = ADDON_SHORT_NAME
    bl_parent_id = "TMBRIDGE_PT_developer"
    bl_order = 80
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        builder = context.scene.tmbridge_gbxmat

        if _gbxmat() is None:
            layout.label(text="GBXMAT module unavailable", icon="ERROR")
            return

        col = layout.column(align=True)
        col.prop(builder, "family")
        col.prop(builder, "name_hint")
        col.prop(builder, "color")

        box = layout.box()
        box.label(text="Flags", icon="MODIFIER")
        box.prop(builder, "opacity_from_diffuse_alpha")
        box.prop(builder, "alpha_cutout")
        box.prop(builder, "flip_normal_green")

        box = layout.box()
        box.label(text="Surface Tags", icon="PHYSICS")
        box.prop(builder, "physics_id")
        box.prop(builder, "gameplay_id")

        box = layout.box()
        header = box.row(align=True)
        header.label(text="Textures", icon="TEXTURE_DATA")
        header.operator(
            TMBRIDGE_OT_GBXMatTextureAdd.bl_idname, text="", icon="ADD"
        )
        op = header.operator(
            TMBRIDGE_OT_GBXMatTextureRemove.bl_idname, text="", icon="REMOVE"
        )
        op.index = -1
        header.operator(
            TMBRIDGE_OT_GBXMatTextureClear.bl_idname, text="", icon="TRASH"
        )

        box.template_list(
            "TMBRIDGE_UL_GBXMatTextures",
            "",
            builder,
            "textures",
            builder,
            "active_texture_index",
            rows=3,
        )

        if 0 <= builder.active_texture_index < len(builder.textures):
            entry = builder.textures[builder.active_texture_index]
            sub = box.column(align=True)
            sub.prop(entry, "role")
            sub.prop(entry, "custom_role")
            sub.prop(entry, "path")

        layout.separator()
        layout.prop(builder, "assign_to_active")
        layout.operator(
            TMBRIDGE_OT_GBXMatBuild.bl_idname, icon="MATERIAL", text="Build Material"
        )


CLASSES: tuple = (
    TMBRIDGE_PG_GBXMatTexture,
    TMBRIDGE_PG_GBXMatBuilder,
    TMBRIDGE_OT_GBXMatTextureAdd,
    TMBRIDGE_OT_GBXMatTextureRemove,
    TMBRIDGE_OT_GBXMatTextureClear,
    TMBRIDGE_OT_GBXMatBuild,
    TMBRIDGE_UL_GBXMatTextures,
    TMBRIDGE_PT_GBXMatPanel,
)


def register_scene_props() -> None:
    bpy.types.Scene.tmbridge_gbxmat = PointerProperty(type=TMBRIDGE_PG_GBXMatBuilder)


def unregister_scene_props() -> None:
    if hasattr(bpy.types.Scene, "tmbridge_gbxmat"):
        try:
            del bpy.types.Scene.tmbridge_gbxmat
        except Exception:  # noqa: BLE001
            pass
