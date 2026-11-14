"""Batch editor-crystal workflow; original files are never overwritten."""

import json
import logging
import math
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import bmesh
import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector

from . import _asset_panel, _progress

log = logging.getLogger("trackmania_blender_bridge.batch_items")
GROUP_ATTRIBUTE = "tm_crystal_group"
SMOOTHING_ATTRIBUTE = "tm_crystal_smoothing"
ACTION_TYPES = (
    ("MERGE", "Merge by Distance", "Weld nearby vertices within each geometry layer"),
    ("LOOSE", "Remove Loose Geometry", "Remove edges and vertices not used by faces"),
    ("DEGENERATE", "Remove Degenerate Faces", "Remove collapsed edges and tiny faces"),
    ("NORMALS", "Recalculate Normals", "Recalculate outward face normals"),
    ("SMOOTHING", "Restore Original Smoothing", "Rebuild Blender shading from original GBX smoothing groups"),
    ("LIGHTMAP", "Regenerate Lightmap UVs", "Create and validate a non-overlapping lightmap atlas"),
    ("VALIDATE", "Validate Item", "Check source, geometry, materials, smoothing, layers and lightmap without writing a file"),
    ("RENAME", "Rename", "Rename the Blender item root and output filename; preserved GBX identifiers are unchanged"),
    ("EXPORT", "Export Item Copy", "Write a validated GBX copy to the output folder; must be the last action"),
)
ACTION_LABELS = {identifier: label for identifier, label, _ in ACTION_TYPES}
_running = None


@dataclass(frozen=True)
class ActionStep:
    kind: str
    distance: float = 0.001
    angle: float = math.radians(66)
    margin: float = 0.02
    prefix: str = ""
    suffix: str = ""
    search: str = ""
    replacement: str = ""


def action_steps(batch):
    return [ActionStep(a.kind, a.distance, a.angle, a.margin,
                       a.prefix, a.suffix, a.search, a.replacement)
            for a in batch.actions]


def active_batch(context):
    settings = context.scene.tm_item_batches
    return settings.batches[settings.active_index] if 0 <= settings.active_index < len(settings.batches) else None


def batch_roots(context, batch):
    return [obj for obj in (*context.scene.objects, *context.scene.collection.children_recursive)
            if obj.get("tm_batch_id") == batch.identifier and obj.get("tm_batch_source_hash")]


def create_batch(context, imported, name):
    settings = context.scene.tm_item_batches
    batch = active_batch(context)
    if batch is None:
        batch = settings.batches.add()
        settings.active_index = len(settings.batches) - 1
    batch.identifier = uuid.uuid4().hex
    batch.name = name
    batch.targets.clear()
    for root in imported:
        root["tm_batch_id"] = batch.identifier
        add_target(batch, root=root)
    batch.targets_initialized = True
    return batch


def add_target(batch, *, root=None, path=""):
    source = str(root.get("tm_source_path", "")) if root is not None else str(path)
    if any((root is not None and target.root == root) or
           (root is None and target.source_path and
            Path(target.source_path).resolve() == Path(source).resolve())
           for target in batch.targets):
        return
    target = batch.targets.add()
    target.root = root
    target.source_path = source
    target.from_file = root is None
    target.name = root.name if root is not None else Path(source).name
    target.status = "Ready" if root is not None else "Not checked"


def ensure_targets(context, batch):
    """Migrate batches saved before the explicit target list existed."""
    if not batch.targets_initialized:
        for root in batch_roots(context, batch):
            add_target(batch, root=root)
        batch.targets_initialized = True


def roots(context):
    found = {}
    for selected in context.selected_objects:
        obj = selected
        while obj is not None:
            if obj.get("tm_batch_source_hash") or obj.get("tm_asset_type") == "Item":
                found[obj.as_pointer()] = obj
                break
            obj = obj.parent
        else:
            for collection in selected.users_collection:
                if collection.get("tm_asset_type") == "Item":
                    found[collection.as_pointer()] = collection
    return list(found.values())


def import_processing_item(path, context, progress):
    from GBXBPY.serializer.item import UnsupportedItemError, read_item

    try:
        read_item(path)
    except UnsupportedItemError as exc:
        log.info("Importing with the normal item importer for Blender-only processing: %s (%s)", path, exc)
        before = {coll.as_pointer() for coll in bpy.data.collections}
        result = import_regular_item(path, context, progress)
        imported = [obj for obj in bpy.data.collections
                    if obj.as_pointer() not in before and obj.get("tm_asset_type") == "Item"]
        if result != {"FINISHED"} or len(imported) != 1 or not meshes(imported[0]):
            raise ValueError("Normal item import produced no usable item geometry; see import diagnostics")
        progress(1, f"Imported {path.name} for Blender processing")
        return imported[0]
    return import_item(path, context, progress)


def import_regular_item(path, context, progress):
    import GBXBPY
    from . import TMBRIDGE_OT_ImportItem, _get_gamedata_dir, _gbxmat_adapter

    resolver = GBXBPY.Resolver(_get_gamedata_dir(context))
    asset = GBXBPY.load(str(path), resolver=resolver)
    if not hasattr(asset, "entity_variants") or not hasattr(asset, "default_placement"):
        raise ValueError(f"Loaded asset is not an Item (got {type(asset).__name__})")
    def report(levels, message):
        if "ERROR" in levels:
            log.error("%s", message)
        elif "WARNING" in levels:
            log.warning("%s", message)
        else:
            log.info("%s", message)
    operator = SimpleNamespace(filepath=str(path), lod_level=1, report=report)
    _gbxmat_adapter.set_active_modifier_folders(resolver, (asset.material_modifier_path or "",))
    try:
        return TMBRIDGE_OT_ImportItem._build_item(operator, context, asset, resolver, progress=progress)
    finally:
        _gbxmat_adapter.clear_active_modifier_folders(resolver)


def meshes(root):
    return [obj for obj in item_objects(root) if obj.type == "MESH" and obj.get("tm_part_role") != "trigger"]


def item_objects(root):
    return tuple(root.all_objects) if isinstance(root, bpy.types.Collection) else root.children_recursive


def _report(operator, failures, successes, notes=()):
    text = bpy.data.texts.get("Item Batch Results") or bpy.data.texts.new("Item Batch Results")
    text.clear()
    text.write("\n".join([*successes, *failures, *notes]))
    if failures:
        operator.report({"WARNING"}, f"{len(successes)} succeeded, {len(failures)} failed; see Item Batch Results")
    else:
        operator.report({"INFO"}, f"{len(successes)} succeeded; see Item Batch Results")
    return {"FINISHED"} if successes else {"CANCELLED"}


def import_item(path, context, progress):
    import GBXBPY
    from GBXBPY.serializer.item import bake_layers, read_item
    from . import _build_blender_mesh_from_gbxbpy, _get_gamedata_dir
    from .map_import_v4.bridge import _ID_COLLECTIONS, _snapshot

    before = _snapshot()
    try:
        parsed, fingerprint = read_item(path)
        layers = bake_layers(parsed, path)
        asset = GBXBPY.load(str(path))
        resolver = GBXBPY.Resolver(_get_gamedata_dir(context))
        from ._item_geometry import configure_item_collection, configure_part
        from GBXBPY.model.geometry import MeshPart
        root = bpy.data.collections.new(asset.name or path.stem)
        context.collection.children.link(root)
        configure_item_collection(root, asset, str(path))
        root["tm_batch_source_hash"] = fingerprint
        root["tm_batch_cleaned"] = False
        root["tm_batch_deformation_baked"] = any(layer.deformation_baked for layer in layers)
        for i, layer in enumerate(layers):
            reporter = progress.sub(i / len(layers), (i + 1) / len(layers))
            obj = _build_blender_mesh_from_gbxbpy(layer.mesh, resolver=resolver, source_path=str(path),
                                               progress=reporter)
            root.objects.link(obj)
            configure_part(obj, MeshPart(
                name=layer.mesh.name or f"Layer{layer.index}", mesh=layer.mesh,
                visible=layer.visible, collidable=layer.collidable, layer_index=layer.index,
            ))
            obj["tm_batch_layer"] = layer.index
            obj["tm_batch_material_indices"] = json.dumps(layer.material_indices)
            obj["tm_batch_material_slots"] = json.dumps([mat.name if mat else None for mat in obj.data.materials])
            obj["tm_batch_visible"] = layer.visible
            obj["tm_batch_collidable"] = layer.collidable
            obj.hide_render = not layer.visible
            if len(obj.data.polygons) != len(layer.face_groups):
                raise ValueError("Blender changed triangle counts; group mapping cannot be preserved")
            group = obj.data.attributes.new(GROUP_ATTRIBUTE, "INT", "FACE")
            group.data.foreach_set("value", layer.face_groups)
            smoothing = obj.data.attributes.new(SMOOTHING_ATTRIBUTE, "INT", "FACE")
            smoothing.data.foreach_set("value", layer.face_smoothing)
            apply_original_smoothing(obj.data)
            # The normal importer omits all-zero texture UV layers.
            if obj.data.uv_layers.get("BaseMaterial") is None:
                obj.data.uv_layers.new(name="BaseMaterial")
        return root
    except Exception:
        bpy.data.batch_remove([block for name in _ID_COLLECTIONS for block in getattr(bpy.data, name)
                               if block.as_pointer() not in before])
        raise


def apply_original_smoothing(mesh):
    """Preview original smoothing groups across duplicated UV/material vertices."""
    smoothing = mesh.attributes.get(SMOOTHING_ATTRIBUTE)
    if smoothing is None:
        raise ValueError("Missing original smoothing metadata; reimport the original item")
    normals = {}
    for polygon in mesh.polygons:
        group = smoothing.data[polygon.index].value
        polygon.use_smooth = group != 0
        if group != 0:
            weighted = polygon.normal * polygon.area
            for index in polygon.vertices:
                key = (tuple(mesh.vertices[index].co), group)
                normals[key] = normals.get(key, Vector()) + weighted
    corners = []
    for polygon in mesh.polygons:
        group = smoothing.data[polygon.index].value
        for index in polygon.vertices:
            normal = normals.get((tuple(mesh.vertices[index].co), group), polygon.normal)
            # Opposing coincident faces can cancel; retain that face's normal.
            corners.append(normal.normalized() if normal.length_squared else polygon.normal)
    mesh.normals_split_custom_set(corners)
    mesh.update()


def mesh_action(obj, action):
    """Run one topology action without implicitly performing other actions."""
    mesh = obj.data
    if mesh.users > 1:
        raise ValueError(f"{obj.name}: mesh is shared; make it single-user first")
    if mesh.shape_keys or obj.modifiers:
        raise ValueError(f"{obj.name}: shape keys/modifiers are not supported")
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        uv = bm.loops.layers.uv.get("BaseMaterial")
        if uv is None and "tm_batch_layer" in obj:
            raise ValueError(f"{obj.name}: missing BaseMaterial UV layer")
        bm.normal_update()
        if action.kind == "MERGE":
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=action.distance)
        elif action.kind == "DEGENERATE":
            bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=action.distance)
            bad = [face for face in bm.faces if face.calc_area() <= action.distance * action.distance]
            if bad:
                bmesh.ops.delete(bm, geom=bad, context="FACES")
        elif action.kind == "LOOSE":
            loose_edges = [edge for edge in bm.edges if not edge.link_faces]
            if loose_edges:
                bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
            loose = [vertex for vertex in bm.verts if not vertex.link_faces]
            if loose:
                bmesh.ops.delete(bm, geom=loose, context="VERTS")
        elif action.kind == "NORMALS":
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        else:
            raise ValueError(f"Unknown mesh action: {action.kind}")
        if not bm.faces:
            raise ValueError(f"{obj.name}: cleanup would remove all faces")
        bm.to_mesh(mesh)
    finally:
        bm.free()
    if action.kind == "NORMALS" and mesh.has_custom_normals:
        with bpy.context.temp_override(object=obj, active_object=obj):
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
    mesh.update()


def clean_mesh(obj, distance):
    """Compatibility helper for the original combined cleanup operator."""
    for kind in ("MERGE", "DEGENERATE", "LOOSE", "NORMALS"):
        mesh_action(obj, ActionStep(kind, distance))
    if obj.data.attributes.get(SMOOTHING_ATTRIBUTE) is not None:
        apply_original_smoothing(obj.data)
    else:
        for polygon in obj.data.polygons:
            polygon.use_smooth = False


def process_item(root, context, actions, destination=None):
    objects = meshes(root)
    if not objects:
        raise ValueError("Item has no meshes")
    if not root.get("tm_batch_source_hash"):
        if destination is not None or any(a.kind in {"EXPORT", "VALIDATE"} for a in actions):
            raise ValueError("GBX validation/export requires an editor-crystal batch import; "
                             "this item supports Blender-only actions")
        if any(a.kind == "SMOOTHING" for a in actions):
            raise ValueError("Restore Original Smoothing requires retained GBX smoothing groups; "
                             "normal imports do not retain them")
    edits_mesh = any(a.kind not in {"VALIDATE", "RENAME", "EXPORT"} for a in actions)
    for obj in objects if edits_mesh else ():
        if obj.data.attributes.get(SMOOTHING_ATTRIBUTE) is None and root.get("tm_batch_source_hash"):
            raise ValueError(f"{obj.name}: missing original smoothing metadata; reimport the original item")
        if obj.data.users > 1:
            raise ValueError(f"{obj.name}: mesh is shared; make it single-user first")
        if obj.data.shape_keys or obj.modifiers:
            raise ValueError(f"{obj.name}: shape keys/modifiers are not supported")
        if obj.hide_select or not obj.visible_get(view_layer=context.view_layer):
            raise ValueError(f"{obj.name}: unhide geometry and enable selection before cleanup")
    backups = [(obj, obj.data, obj.data.copy()) for obj in objects] if edits_mesh else []
    selection = list(context.selected_objects)
    active = context.view_layer.objects.active
    was_cleaned = root.get("tm_batch_cleaned")
    original_name = root.name
    original_output = root.get("tm_batch_output_name")
    try:
        for obj, original, staged in backups:
            obj.data = staged
        for action in actions:
            _progress.update(_progress.snapshot()["fraction"], f"{root.name}: {ACTION_LABELS.get(action.kind, action.kind)}")
            if action.kind == "VALIDATE":
                serialize_item(root)
            elif action.kind == "RENAME":
                name = renamed_filename(output_filename(root), action)
                root["tm_batch_output_name"] = name
                root.name = name[:-len(".Item.Gbx")]
            elif action.kind == "EXPORT":
                if destination is None:
                    raise ValueError("Export Item Copy requires an output folder")
            elif action.kind == "LIGHTMAP":
                regenerate_lightmap(root, objects, context, action)
            elif action.kind == "SMOOTHING":
                for obj in objects:
                    apply_original_smoothing(obj.data)
            else:
                for obj in objects:
                    mesh_action(obj, action)
        if edits_mesh:
            root["tm_batch_cleaned"] = True
        if destination is not None:
            export_item(root, destination)
    except Exception:
        if was_cleaned is None:
            if "tm_batch_cleaned" in root:
                del root["tm_batch_cleaned"]
        else:
            root["tm_batch_cleaned"] = was_cleaned
        root.name = original_name
        if original_output is None:
            if "tm_batch_output_name" in root:
                del root["tm_batch_output_name"]
        else:
            root["tm_batch_output_name"] = original_output
        for obj, original, staged in backups:
            obj.data = original
            bpy.data.meshes.remove(staged)
        raise
    else:
        for _, original, _ in backups:
            if original.users == 0:
                bpy.data.meshes.remove(original)
    finally:
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in selection:
            obj.select_set(True)
        context.view_layer.objects.active = active


def clean_item(root, context, distance):
    process_item(root, context, [
        ActionStep(kind, distance) for kind in
        ("MERGE", "DEGENERATE", "LOOSE", "NORMALS", "SMOOTHING", "LIGHTMAP")
    ])


def regenerate_lightmap(root, objects, context, action):
    from GBXBPY.serializer.item import LightmapOverlapError, validate_lightmap

    active_layers = {obj: obj.data.uv_layers.active.name if obj.data.uv_layers.active else None for obj in objects}
    try:
        for obj in objects:
            mesh = obj.data
            mesh.uv_layers.active = mesh.uv_layers.get("Lightmap") or mesh.uv_layers.new(name="Lightmap")
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=action.angle, island_margin=action.margin,
                                     correct_aspect=False, scale_to_bounds=True)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
        # Thin projected boundaries can cross only after 16-bit quantization.
        # Detach those triangles into padded islands, then revalidate the atlas.
        visible_faces = [(obj, face.index) for obj in objects if obj.get("tm_batch_visible", not obj.hide_render)
                         for face in obj.data.polygons]
        for attempt in range(9):
            triangles = [
                [tuple(obj.data.uv_layers["Lightmap"].data[i].uv)
                 for i in obj.data.polygons[face_index].loop_indices]
                for obj, face_index in visible_faces
            ]
            try:
                validate_lightmap(triangles)
                break
            except LightmapOverlapError as exc:
                if attempt == 8:
                    raise
                log.info("Separating quantization-overlapping lightmap triangles %s on %s",
                         exc.triangles, root.name)
                for offset, index in enumerate(exc.triangles, start=1):
                    obj, face_index = visible_faces[index]
                    uv = obj.data.uv_layers["Lightmap"]
                    for i in obj.data.polygons[face_index].loop_indices:
                        uv.data[i].uv.x += 2 * offset
                bpy.ops.object.mode_set(mode="EDIT")
                try:
                    bpy.ops.mesh.select_all(action="SELECT")
                    bpy.ops.uv.select_all(action="SELECT")
                    bpy.ops.uv.pack_islands(rotate=True, margin=action.margin)
                finally:
                    bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        for obj, name in active_layers.items():
            if name is not None:
                obj.data.uv_layers.active = obj.data.uv_layers[name]


def serialize_item(root):
    """Validate the real writer constraints, without changing Blender data or disk."""
    from GBXBPY.serializer.item import LayerGeometry, crystal, read_item, write_item

    if not root.get("tm_batch_source_hash") or not root.get("tm_source_path"):
        raise ValueError("Item lacks editor-crystal source metadata; import it with the Batch Processor")
    source = Path(root["tm_source_path"])
    parsed, fingerprint = read_item(source)
    if fingerprint != root["tm_batch_source_hash"]:
        raise ValueError("Original source changed since import; reimport before export")
    geometries = {}
    layers = crystal(parsed).body[0x09003005].layers
    for obj in meshes(root):
        if "tm_batch_layer" not in obj:
            raise ValueError("Item contains untracked geometry")
        if obj.modifiers or obj.data.shape_keys:
            raise ValueError("Apply/remove modifiers and shape keys before export")
        mesh = obj.data
        base = mesh.uv_layers.get("BaseMaterial")
        lightmap = mesh.uv_layers.get("Lightmap")
        groups = mesh.attributes.get(GROUP_ATTRIBUTE)
        smoothing = mesh.attributes.get(SMOOTHING_ATTRIBUTE)
        if smoothing is None:
            raise ValueError("Missing original smoothing metadata; reimport the original item")
        if base is None or lightmap is None or groups is None:
            raise ValueError("Missing texture UVs, lightmap UVs or original group metadata; "
                             "add Regenerate Lightmap UVs if the item has no Lightmap channel")
        indices = json.loads(obj["tm_batch_material_indices"])
        slots = [slot.material.name if slot.material else None for slot in obj.material_slots]
        if slots != json.loads(obj["tm_batch_material_slots"]):
            raise ValueError("Original material slots were renamed/replaced/reordered; reimport before export")
        if any(p.material_index >= len(indices) for p in mesh.polygons):
            raise ValueError("Material assignments were changed outside the original slot table")
        origin = Matrix.Identity(4) if isinstance(root, bpy.types.Collection) else root.matrix_world
        transform = origin.inverted() @ obj.matrix_world
        if transform.determinant() <= 0:
            raise ValueError("Mirrored/singular transforms must be applied and normals repaired")
        index = obj["tm_batch_layer"]
        if index in geometries:
            raise ValueError("Duplicate geometry-layer identity")
        if not isinstance(index, int) or not 0 <= index < len(layers) or layers[index].type != "Geometry":
            raise ValueError("Invalid original geometry-layer identity")
        layer = layers[index]
        if (bool(obj.get("tm_batch_visible")) != bool(layer.content.isVisible) or
                bool(obj.get("tm_batch_collidable")) != bool(layer.content.isCollidable) or
                obj.hide_render != (not bool(layer.content.isVisible))):
            raise ValueError("Original layer visibility/collision flags were changed; flag editing is not supported")
        geometries[index] = LayerGeometry(
            positions=[tuple(transform @ vertex.co) for vertex in mesh.vertices],
            faces=[list(p.vertices) for p in mesh.polygons],
            texture_uvs=[[tuple(base.data[i].uv) for i in p.loop_indices] for p in mesh.polygons],
            lightmap_uvs=[[tuple(lightmap.data[i].uv) for i in p.loop_indices] for p in mesh.polygons],
            materials=[indices[p.material_index] for p in mesh.polygons],
            groups=[groups.data[p.index].value for p in mesh.polygons],
            smoothing_groups=[smoothing.data[p.index].value for p in mesh.polygons],
        )
    return write_item(parsed, geometries)


def export_item(root, destination):
    destination = Path(destination)
    if Path(root["tm_source_path"]).resolve() == destination.resolve():
        raise ValueError("Overwriting the source item is prohibited")
    raw = serialize_item(root)
    # Exclusive creation also guards races between the preflight and write.
    stream = destination.open("xb")
    try:
        with stream:
            stream.write(raw)
    except BaseException:
        destination.unlink()
        raise


def output_filename(root):
    return root.get("tm_batch_output_name") or Path(root["tm_source_path"]).name


def renamed_filename(filename: str, action: ActionStep) -> str:
    if not filename.lower().endswith(".item.gbx"):
        raise ValueError("Expected an .Item.Gbx output filename")
    stem = filename[:-len(".Item.Gbx")]
    if action.search:
        stem = stem.replace(action.search, action.replacement)
    stem = action.prefix + stem + action.suffix
    if (not stem or stem.endswith((" ", ".")) or
            any(ord(c) < 32 or c in '<>:"/\\|?*' for c in stem) or
            stem.split(".")[0].upper() in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError("Rename produces an invalid output filename")
    return stem + ".Item.Gbx"


class TMBRIDGE_OT_BatchImportItems(bpy.types.Operator, ImportHelper):
    bl_idname = "trackmania_blender_bridge.batch_import_items"
    bl_label = "Batch Custom Items (Experimental)"
    bl_description = "Import supported editor-crystal items, baking deformation; originals remain untouched"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".Gbx"
    filter_glob: StringProperty(default="*.Item.Gbx;*.item.gbx", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active()

    @_progress.tracked("Batch Importing Items")
    def execute(self, context):
        paths = [Path(self.directory) / entry.name for entry in self.files] or [Path(self.filepath)]
        failures, successes, imported = [], [], []
        for index, path in enumerate(paths):
            try:
                root = import_item(path, context, _progress.Progress(
                    0.02 + 0.96 * index / len(paths), 0.02 + 0.96 * (index + 1) / len(paths)))
                imported.append(root)
                note = " (approximate deformation bake; review shape in game)" if root["tm_batch_deformation_baked"] else ""
                successes.append(f"Imported: {path}{note}")
            except Exception as exc:
                log.exception("Batch item import failed: %s", path)
                failures.append(f"FAILED {path}: {exc}")
        if imported:
            create_batch(context, imported, Path(self.directory or self.filepath).name or "Items")
            for obj in context.selected_objects:
                obj.select_set(False)
            for root in imported:
                for obj in item_objects(root):
                    obj.select_set(True)
            context.view_layer.objects.active = next(iter(item_objects(imported[-1])), None)
        result = _report(self, failures, successes)
        if any(root["tm_batch_deformation_baked"] for root in imported):
            self.report({"WARNING"}, "Deformation baked approximately; verify shape and collision in Trackmania")
        return result


class _SelectedItems:
    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active() and bool(roots(context))


class TMBRIDGE_OT_CleanItems(_SelectedItems, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.clean_batch_items"
    bl_label = "Clean Meshes + Lightmap UVs"
    bl_options = {"REGISTER", "UNDO"}
    merge_distance: FloatProperty(name="Merge Distance", default=0.001, min=0.00000001,
                                 subtype="DISTANCE")

    @_progress.tracked("Cleaning Items")
    def execute(self, context):
        selected = roots(context)
        failures, successes = [], []
        for index, root in enumerate(selected):
            _progress.update(0.02 + 0.96 * index / len(selected), f"Cleaning {root.name}")
            try:
                before = sum(len(obj.data.vertices) for obj in meshes(root))
                clean_item(root, context, self.merge_distance)
                after = sum(len(obj.data.vertices) for obj in meshes(root))
                successes.append(
                    f"Cleaned: {root.name}; vertices {before} -> {after}; "
                    f"merge distance {self.merge_distance:g} m"
                )
            except Exception as exc:
                log.exception("Batch cleanup failed: %s", root.name)
                failures.append(f"FAILED {root.name}: {exc}")
        return _report(self, failures, successes)


class TMBRIDGE_OT_ExportItems(_SelectedItems, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.export_batch_items"
    bl_label = "Export Items to GBX (Experimental)"
    directory: StringProperty(name="Output Folder", subtype="DIR_PATH")

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    @_progress.tracked("Exporting Items")
    def execute(self, context):
        output = Path(bpy.path.abspath(self.directory))
        if not self.directory or not output.is_dir():
            self.report({"ERROR"}, "Choose an existing, separate output folder")
            return {"CANCELLED"}
        failures, successes = [], []
        selected = roots(context)
        for index, root in enumerate(selected):
            _progress.update(0.02 + 0.96 * index / len(selected), f"Exporting {root.name}")
            target = output / Path(root["tm_source_path"]).name
            try:
                export_item(root, target)
                successes.append(f"Exported (in-game verification required): {target}")
            except Exception as exc:
                log.exception("Batch item export failed: %s", root.name)
                failures.append(f"FAILED {root.name}: {exc}")
        return _report(self, failures, successes)


class TMBRIDGE_BatchAction(bpy.types.PropertyGroup):
    kind: EnumProperty(name="Action", items=ACTION_TYPES)
    enabled: BoolProperty(default=True, options={"HIDDEN"})
    distance: FloatProperty(name="Distance", default=0.001, min=0.00000001,
                            subtype="DISTANCE")
    angle: FloatProperty(name="Projection Angle", default=math.radians(66),
                         min=math.radians(1), max=math.radians(89), subtype="ANGLE")
    margin: FloatProperty(name="Island Margin", default=0.02, min=0.001, max=0.1)
    prefix: StringProperty(name="Prefix")
    suffix: StringProperty(name="Suffix")
    search: StringProperty(name="Search")
    replacement: StringProperty(name="Replace With")


class TMBRIDGE_BatchTarget(bpy.types.PropertyGroup):
    name: StringProperty()
    object_root: PointerProperty(type=bpy.types.Object)
    collection_root: PointerProperty(type=bpy.types.Collection)

    @property
    def root(self):
        return self.collection_root or self.object_root

    @root.setter
    def root(self, value):
        self.collection_root = value if isinstance(value, bpy.types.Collection) else None
        self.object_root = value if isinstance(value, bpy.types.Object) else None
    source_path: StringProperty(subtype="FILE_PATH")
    from_file: BoolProperty(default=False)
    included: BoolProperty(name="Process", default=True)
    status: StringProperty(default="Not checked")
    message: StringProperty()


class TMBRIDGE_ItemBatch(bpy.types.PropertyGroup):
    identifier: StringProperty()
    name: StringProperty(name="Batch Name", default="Items")
    actions: CollectionProperty(type=TMBRIDGE_BatchAction)
    active_action: IntProperty(default=0, min=0)
    directory: StringProperty(name="Output Folder", subtype="DIR_PATH")
    targets: CollectionProperty(type=TMBRIDGE_BatchTarget)
    targets_initialized: BoolProperty(default=False)
    active_target: IntProperty(default=0, min=0)


class TMBRIDGE_ItemBatches(bpy.types.PropertyGroup):
    batches: CollectionProperty(type=TMBRIDGE_ItemBatch)
    active_index: IntProperty(default=0, min=0)
    show_action_settings: BoolProperty(
        name="Action Settings",
        description="Switch between the action list and the selected action's settings",
        default=False, options={"SKIP_SAVE"},
    )
    source_mode: EnumProperty(name="Input", items=(
        ("SELECTED", "Selected Items", "Process the currently selected imported item roots", "RESTRICT_SELECT_OFF", 0),
        ("FILES", "Files", "Choose .Item.Gbx files when Run Batch is pressed", "FILE_FOLDER", 1),
    ), default="SELECTED")


class TMBRIDGE_UL_Batches(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname):
        layout.prop(item, "name", text="", emboss=False, icon="OUTLINER_COLLECTION")


class TMBRIDGE_UL_BatchActions(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname):
        layout.label(text=ACTION_LABELS[item.kind])


class TMBRIDGE_UL_BatchTargets(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname):
        layout.prop(item, "included", text="")
        layout.label(text=item.root.name if item.root is not None else item.name,
                     icon="OBJECT_DATA" if item.root is not None else "FILE")
        if item.status not in {"Ready", "Not checked"}:
            layout.label(text=item.status, icon="ERROR" if item.status == "Failed" else "BLANK1")


class _BatchOptions:
    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active() and active_batch(context) is not None


class TMBRIDGE_OT_NewBatch(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.new_item_batch"
    bl_label = "New Batch"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active()

    def execute(self, context):
        create_batch(context, [], "Items")
        return {"FINISHED"}


class TMBRIDGE_OT_SelectBatchFiles(bpy.types.Operator, ImportHelper):
    bl_idname = "trackmania_blender_bridge.select_batch_files"
    bl_label = "Select Item Files"
    bl_description = "Queue .Item.Gbx files; supported editor-crystal items are imported when Run Batch is pressed"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".Gbx"
    filter_glob: StringProperty(default="*.Item.Gbx;*.item.gbx", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active()

    def execute(self, context):
        paths = [Path(self.directory) / entry.name for entry in self.files] or [Path(self.filepath)]
        if any(not path.is_file() or not path.name.lower().endswith(".item.gbx") for path in paths):
            self.report({"ERROR"}, "Select existing .Item.Gbx files")
            return {"CANCELLED"}
        batch = active_batch(context) or create_batch(context, [], "Items")
        ensure_targets(context, batch)
        for path in paths:
            add_target(batch, path=path.resolve())
        self.report({"INFO"}, f"{len(batch.targets)} target(s); compatibility is checked when processing")
        return {"FINISHED"}


class TMBRIDGE_OT_SelectBatchItems(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.select_batch_items"
    bl_label = "Use Selected Scene Items"
    bl_description = "Add selected imported items; GBX export remains limited to editor-crystal batch imports"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active() and bool(context.selected_objects)

    def execute(self, context):
        selected = roots(context)
        if not selected:
            self.report({"ERROR"}, "Select imported item roots or their child meshes")
            return {"CANCELLED"}
        # Reject unrelated selected objects instead of silently dropping them.
        if any(obj not in selected and not any(obj in item_objects(root) for root in selected)
               for obj in context.selected_objects):
            self.report({"ERROR"}, "Selection contains objects outside imported items")
            return {"CANCELLED"}
        batch = active_batch(context) or create_batch(context, [], "Items")
        ensure_targets(context, batch)
        for root in selected:
            add_target(batch, root=root)
        return {"FINISHED"}


class TMBRIDGE_OT_RemoveBatchTarget(_BatchOptions, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.remove_batch_target"
    bl_label = "Remove Target"
    bl_description = "Remove the target from this batch; leave scene objects and source files untouched"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        batch = active_batch(context)
        ensure_targets(context, batch)
        if not 0 <= batch.active_target < len(batch.targets):
            self.report({"ERROR"}, "Select a target to remove")
            return {"CANCELLED"}
        batch.targets.remove(batch.active_target)
        batch.active_target = max(0, min(batch.active_target, len(batch.targets) - 1))
        return {"FINISHED"}


def prepare_batch(context, batch):
    ensure_targets(context, batch)
    actions = action_steps(batch)
    if not actions:
        raise ValueError("Add at least one action")
    exports = [i for i, action in enumerate(actions) if action.kind == "EXPORT"]
    if exports and exports != [len(actions) - 1]:
        raise ValueError("Export Item Copy must occur once, as the last action")
    targets = [target for target in batch.targets if target.included]
    if not targets:
        raise ValueError("Select at least one included item")
    output = Path(bpy.path.abspath(batch.directory))
    if exports and (not batch.directory or not output.is_dir()):
        raise ValueError("Choose an existing, separate output folder")
    source_paths = {
        str(Path(bpy.path.abspath(target.root.get("tm_source_path", "") if target.root is not None
                                  else target.source_path)).resolve()).casefold()
        for target in batch.targets
    }
    jobs, filenames = [], set()
    for target in targets:
        name = output_filename(target.root) if target.root is not None else Path(target.source_path).name
        for action in actions:
            if action.kind == "RENAME":
                name = renamed_filename(name, action)
        destination = output / name if exports else None
        if destination is not None:
            renamed_filename(name, ActionStep("RENAME"))
            key = str(destination.resolve()).casefold()
            if key in filenames:
                raise ValueError("Batch contains duplicate output filenames; use Rename or separate batches")
            if key in source_paths:
                raise ValueError("Overwriting any source item is prohibited")
            filenames.add(key)
        jobs.append((target, destination))
    for target in batch.targets:
        target.status = "Queued" if target.included else "Skipped"
        target.message = ""
    return actions, jobs


def run_batch_items(context, batch, actions, jobs, successes, failures):
    """Each yield commits one whole item; cancellation never leaves a partial edit."""
    from .map_import_v4.bridge import _ID_COLLECTIONS, _snapshot

    for index, (target, destination) in enumerate(jobs):
        target.status = "Running"
        root = target.root
        before = None
        try:
            if destination is not None and destination.exists():
                raise FileExistsError(f"Output already exists: {destination}")
            progress = _progress.Progress(index / len(jobs), (index + 1) / len(jobs))
            progress(0, f"Processing {target.name}")
            if root is None:
                if not target.from_file:
                    raise ValueError("Scene item was deleted; remove it from the target list")
                before = _snapshot()
                root = import_processing_item(Path(target.source_path), context, progress.sub(0, .4))
                root["tm_batch_id"] = batch.identifier
            owners = context.scene.collection.children_recursive if isinstance(root, bpy.types.Collection) else context.scene.objects
            if root.name not in {owner.name for owner in owners}:
                raise ValueError("Scene item is not in the current scene")
            process_item(root, context, actions, destination=destination)
        except Exception as exc:
            log.exception("Batch processing failed: %s", target.name)
            if before is not None:
                bpy.data.batch_remove([block for name in _ID_COLLECTIONS for block in getattr(bpy.data, name)
                                       if block.as_pointer() not in before])
            target.status = "Failed"
            target.message = str(exc)
            failures.append(f"FAILED {target.name}: {exc}")
        else:
            target.root = root
            target.name = root.name
            target.status = "Done"
            target.message = (f"Exported: {destination}; in-game verification required" if destination is not None
                              else "Processed in Blender; no file written")
            if not root.get("tm_batch_source_hash"):
                target.message += "; GBX export/original smoothing unavailable for this import"
            if root.get("tm_batch_deformation_baked"):
                target.message += "; approximate deformation bake: review shape/collision in game"
            successes.append(f"{target.name}: {target.message}; actions: " +
                             ", ".join(ACTION_LABELS[action.kind] for action in actions))
        _progress.update((index + 1) / len(jobs), target.message)
        yield


class TMBRIDGE_OT_RunBatch(_BatchOptions, bpy.types.Operator, ImportHelper):
    bl_idname = "trackmania_blender_bridge.run_item_batch"
    bl_label = "Run Batch"
    bl_description = "Process selected items or open the file picker, running every listed action in order"
    bl_options = {"REGISTER", "UNDO"}
    _timer = None
    _owner = None
    _iterator = None
    filename_ext = ".Gbx"
    filter_glob: StringProperty(default="*.Item.Gbx;*.item.gbx", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype="DIR_PATH")
    input_mode: EnumProperty(items=(
        ("BATCH", "Stored Targets", "Compatibility for scripts using queued targets"),
        ("SELECTED", "Selected Items", "Use the current scene selection"),
        ("FILES", "Files", "Import the chosen files"),
    ), default="BATCH", options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, _event):
        self.input_mode = context.scene.tm_item_batches.source_mode
        if self.input_mode == "FILES":
            self.files.clear()
            self.filepath = ""
            context.window_manager.fileselect_add(self)
            return {"RUNNING_MODAL"}
        return self.execute(context)

    def execute(self, context):
        global _running
        batch = active_batch(context)
        try:
            if self.input_mode == "SELECTED":
                selected = roots(context)
                if not selected:
                    raise ValueError("Select imported item roots or their child meshes")
                if any(obj not in selected and not any(obj in item_objects(root) for root in selected)
                       for obj in context.selected_objects):
                    raise ValueError("Selection contains objects outside imported items")
                batch.targets.clear()
                for root in selected:
                    add_target(batch, root=root)
                batch.targets_initialized = True
            elif self.input_mode == "FILES":
                paths = [Path(self.directory) / entry.name for entry in self.files] or [Path(self.filepath)]
                if any(not path.is_file() or not path.name.lower().endswith(".item.gbx") for path in paths):
                    raise ValueError("Select existing .Item.Gbx files")
                batch.targets.clear()
                for path in paths:
                    add_target(batch, path=path.resolve())
                batch.targets_initialized = True
            actions, self._jobs = prepare_batch(context, batch)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self._successes, self._failures = [], []
        self._scene = context.scene
        self._cancel_requested = False
        self._iterator = run_batch_items(context, batch, actions, self._jobs, self._successes, self._failures)
        if bpy.app.background or context.window is None:
            with _progress.task("Batch Processor"):
                for _ in self._iterator:
                    pass
            return _report(self, self._failures, self._successes)
        try:
            self._owner = _progress.begin("Batch Processor", modal=True,
                                         cancel_operator=TMBRIDGE_OT_CancelBatch.bl_idname)
            self._timer = context.window_manager.event_timer_add(.05, window=context.window)
            context.window_manager.modal_handler_add(self)
            _running = self
        except Exception as exc:
            log.exception("Batch Processor could not start")
            self._stop(context)
            self.report({"ERROR"}, f"Batch Processor could not start: {exc}")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if self._cancel_requested or (event.type == "ESC" and event.value == "PRESS"):
            return self._abort(context, "Batch cancelled; completed items were kept")
        if context.scene != self._scene:
            return self._abort(context, "Batch cancelled because the active scene changed")
        if context.mode != "OBJECT":
            return self._abort(context, "Batch cancelled because Object mode was left")
        if event.type == "TIMER":
            try:
                next(self._iterator)
            except StopIteration:
                self._stop(context)
                return _report(self, self._failures, self._successes)
            except Exception as exc:
                log.exception("Batch Processor failed")
                return self._abort(context, f"Batch Processor failed: {exc}")
        if event.type in {"MOUSEMOVE", "MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}:
            return {"PASS_THROUGH"}
        if event.type == "LEFTMOUSE" and context.window is not None:
            for area in context.window.screen.areas:
                for region in area.regions:
                    if (region.type in {"UI", "TOOL_HEADER"} and
                            region.x <= event.mouse_x < region.x + region.width and
                            region.y <= event.mouse_y < region.y + region.height):
                        return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}

    def _abort(self, context, message):
        for target, _ in self._jobs:
            if target.status == "Queued":
                target.status = "Cancelled"
                target.message = message
        self._stop(context)
        log.warning(message)
        _report(self, self._failures, self._successes, [message])
        self.report({"WARNING"}, message)
        # FINISHED preserves undo for completed item edits.
        return {"FINISHED"} if self._successes else {"CANCELLED"}

    def cancel(self, context):
        if self._iterator is not None:
            self._abort(context, "Batch cancelled; completed items were kept")

    def _stop(self, context):
        global _running
        if self._iterator is not None:
            self._iterator.close()
            self._iterator = None
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._owner is not None:
            _progress.end(self._owner)
            self._owner = None
        if _running is self:
            _running = None


class TMBRIDGE_OT_CancelBatch(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.cancel_item_batch"
    bl_label = "Cancel Batch"
    bl_description = "Stop before the next item; completed items remain processed"

    @classmethod
    def poll(cls, _context):
        return _running is not None

    def execute(self, _context):
        _running._cancel_requested = True
        return {"FINISHED"}


class TMBRIDGE_OT_AddBatchAction(_BatchOptions, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.add_batch_action"
    bl_label = "Add Action"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "kind"
    kind: EnumProperty(name="Action", items=ACTION_TYPES)

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active()

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        batch = active_batch(context) or create_batch(context, [], "Items")
        action = batch.actions.add()
        action.kind = self.kind
        batch.active_action = len(batch.actions) - 1
        context.scene.tm_item_batches.show_action_settings = False
        return {"FINISHED"}


class TMBRIDGE_OT_RemoveBatchAction(_BatchOptions, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.remove_batch_action"
    bl_label = "Remove Action"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return super().poll(context) and bool(active_batch(context).actions)

    def execute(self, context):
        batch = active_batch(context)
        if not 0 <= batch.active_action < len(batch.actions):
            self.report({"ERROR"}, "Select an action to remove")
            return {"CANCELLED"}
        batch.actions.remove(batch.active_action)
        batch.active_action = max(0, min(batch.active_action, len(batch.actions) - 1))
        context.scene.tm_item_batches.show_action_settings = False
        return {"FINISHED"}


class TMBRIDGE_OT_MoveBatchAction(_BatchOptions, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.move_batch_action"
    bl_label = "Move Action"
    bl_options = {"REGISTER", "UNDO"}
    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    @classmethod
    def poll(cls, context):
        return super().poll(context) and len(active_batch(context).actions) > 1

    def execute(self, context):
        batch = active_batch(context)
        index = batch.active_action
        target = index + (-1 if self.direction == "UP" else 1)
        if not 0 <= index < len(batch.actions) or not 0 <= target < len(batch.actions):
            self.report({"WARNING"}, "Action is already at the end of the list")
            return {"CANCELLED"}
        batch.actions.move(index, target)
        batch.active_action = target
        return {"FINISHED"}


class TMBRIDGE_OT_AdoptBatchItems(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.adopt_batch_items"
    bl_label = "Create Batch from Selected Items"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and not _progress.active() and any(
            not root.get("tm_batch_id") for root in roots(context)
        )

    def execute(self, context):
        selected = [root for root in roots(context) if not root.get("tm_batch_id")]
        if not selected:
            self.report({"ERROR"}, "Select previously imported batch items without a batch")
            return {"CANCELLED"}
        create_batch(context, selected, "Selected Items")
        return {"FINISHED"}


class TMBRIDGE_OT_ExportBatch(_BatchOptions, bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.export_item_batch"
    bl_label = "Export Batch"
    bl_description = "Run actions in order on all items in this batch, then export separate GBX copies"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and bool(action_steps(active_batch(context))) and bool(
            batch_roots(context, active_batch(context))
        )

    @_progress.tracked("Exporting Batch")
    def execute(self, context):
        batch = active_batch(context)
        if batch is None or not batch.actions:
            self.report({"ERROR"}, "Add at least one batch action before export")
            return {"CANCELLED"}
        output = Path(bpy.path.abspath(batch.directory))
        if not batch.directory or not output.is_dir():
            self.report({"ERROR"}, "Choose an existing, separate output folder")
            return {"CANCELLED"}
        items = batch_roots(context, batch)
        if not items:
            self.report({"ERROR"}, "This batch has no remaining items in the current scene")
            return {"CANCELLED"}
        actions = action_steps(batch)
        if not actions:
            self.report({"ERROR"}, "Add at least one action")
            return {"CANCELLED"}
        exports = [i for i, action in enumerate(actions) if action.kind == "EXPORT"]
        if exports and exports != [len(actions) - 1]:
            self.report({"ERROR"}, "Export Item Copy must occur once, as the last action")
            return {"CANCELLED"}
        targets = []
        try:
            for root in items:
                name = output_filename(root)
                for action in actions:
                    if action.kind == "RENAME":
                        name = renamed_filename(name, action)
                targets.append(output / name)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if len({str(path.resolve()).casefold() for path in targets}) != len(targets):
            self.report({"ERROR"}, "Batch contains duplicate output filenames; split or reimport the batch")
            return {"CANCELLED"}
        failures, successes = [], []
        source_paths = {Path(root["tm_source_path"]).resolve() for root in items}
        for index, (root, target) in enumerate(zip(items, targets)):
            _progress.update(0.02 + 0.96 * index / len(items), f"Processing {root.name}")
            try:
                if target.resolve() in source_paths:
                    raise ValueError("Overwriting a source item is prohibited")
                if target.exists():
                    raise FileExistsError(f"Output already exists: {target}")
                before = sum(len(obj.data.vertices) for obj in meshes(root))
                process_item(root, context, actions, destination=target)
                after = sum(len(obj.data.vertices) for obj in meshes(root))
                successes.append(
                    f"Exported (in-game verification required): {target}; vertices {before} -> {after}; "
                    f"actions: {', '.join(ACTION_LABELS[action.kind] for action in actions)}"
                )
            except Exception as exc:
                log.exception("Batch action/export failed: %s", root.name)
                failures.append(f"FAILED {root.name}: {exc}")
        return _report(self, failures, successes)


def draw_batch_processor(layout, context):
    settings = context.scene.tm_item_batches
    batch = active_batch(context)
    controls = layout.column()
    controls.enabled = context.mode == "OBJECT" and not _progress.active()
    actions = controls.box()
    actions.label(text="Actions", icon="MODIFIER")
    row = actions.row()
    selected = batch is not None and 0 <= batch.active_action < len(batch.actions)
    content = row.column()
    if settings.show_action_settings and selected:
        action = batch.actions[batch.active_action]
        box = content.column(align=True)
        box.use_property_split = True
        box.use_property_decorate = False
        box.label(text=ACTION_LABELS[action.kind], icon="PREFERENCES")
        if action.kind in {"MERGE", "DEGENERATE"}:
            box.prop(action, "distance")
        elif action.kind == "LIGHTMAP":
            box.prop(action, "angle", text="Angle")
            box.prop(action, "margin", text="Margin")
        elif action.kind == "SMOOTHING":
            box.label(text="Uses original GBX smoothing groups")
        elif action.kind == "RENAME":
            for name in ("search", "replacement", "prefix", "suffix"):
                box.prop(action, name)
            box.label(text="Blender name / filename only")
            box.label(text="Internal GBX IDs stay unchanged")
        elif action.kind == "VALIDATE":
            box.label(text="Read-only; needs lightmap UVs")
        elif action.kind == "EXPORT":
            box.prop(batch, "directory")
            box.label(text="Export must be last")
            box.label(text="Never overwrites originals")
        else:
            box.label(text="No settings for this action.")
    elif batch is not None:
        content.template_list("TMBRIDGE_UL_BatchActions", "", batch, "actions", batch, "active_action", rows=4)
    buttons = row.column(align=True)
    buttons.operator(TMBRIDGE_OT_AddBatchAction.bl_idname, text="", icon="ADD")
    remove = buttons.row()
    remove.enabled = batch is not None and bool(batch.actions)
    remove.operator(TMBRIDGE_OT_RemoveBatchAction.bl_idname, text="", icon="REMOVE")
    buttons.separator()
    up = buttons.row()
    up.enabled = batch is not None and batch.active_action > 0
    up.operator(TMBRIDGE_OT_MoveBatchAction.bl_idname, text="", icon="TRIA_UP").direction = "UP"
    down = buttons.row()
    down.enabled = batch is not None and batch.active_action < len(batch.actions) - 1
    down.operator(TMBRIDGE_OT_MoveBatchAction.bl_idname, text="", icon="TRIA_DOWN").direction = "DOWN"
    buttons.separator()
    config = buttons.row()
    config.enabled = selected
    config.prop(settings, "show_action_settings", text="", icon="PREFERENCES", toggle=True)

    footer = controls.row(align=True)
    footer.scale_y = 1.4
    run = footer.row(align=True)
    run.enabled = batch is not None and bool(batch.actions)
    run.operator_context = "INVOKE_DEFAULT"
    run.operator(TMBRIDGE_OT_RunBatch.bl_idname, icon="PLAY")
    footer.prop(settings, "source_mode", text="", expand=True, icon_only=True)
    if _running is not None:
        running = layout.box()
        _progress.draw(running)
        running.label(text="Cancellation stops before the next item.")

    finished = [target for target in batch.targets if target.status in {"Done", "Failed", "Cancelled"}] if batch else []
    if finished:
        results = layout.box()
        results.label(text="Results", icon="TEXT")
        done = sum(target.status == "Done" for target in finished)
        failed = sum(target.status == "Failed" for target in finished)
        cancelled = len(finished) - done - failed
        results.label(text=f"{done} done / {failed} failed / {cancelled} cancelled")
        if 0 <= batch.active_target < len(batch.targets):
            target = batch.targets[batch.active_target]
            if target.message:
                region = context.region
                width = max(24, int(region.width / (7 * context.preferences.system.ui_scale)) - 8) if region else 48
                for line in textwrap.wrap(target.message, width=width):
                    results.label(text=line)
        results.label(text="Full report: Item Batch Results", icon="INFO")


CLASSES = (TMBRIDGE_OT_BatchImportItems, TMBRIDGE_OT_CleanItems, TMBRIDGE_OT_ExportItems,
           TMBRIDGE_BatchAction, TMBRIDGE_BatchTarget, TMBRIDGE_ItemBatch, TMBRIDGE_ItemBatches,
           TMBRIDGE_UL_Batches, TMBRIDGE_UL_BatchActions, TMBRIDGE_UL_BatchTargets,
           TMBRIDGE_OT_NewBatch, TMBRIDGE_OT_SelectBatchFiles, TMBRIDGE_OT_SelectBatchItems,
           TMBRIDGE_OT_RemoveBatchTarget, TMBRIDGE_OT_RunBatch, TMBRIDGE_OT_CancelBatch,
           TMBRIDGE_OT_AddBatchAction, TMBRIDGE_OT_RemoveBatchAction, TMBRIDGE_OT_MoveBatchAction,
           TMBRIDGE_OT_AdoptBatchItems, TMBRIDGE_OT_ExportBatch)


def register_scene_props():
    bpy.types.Scene.tm_item_batches = PointerProperty(type=TMBRIDGE_ItemBatches)


def unregister_scene_props():
    del bpy.types.Scene.tm_item_batches
