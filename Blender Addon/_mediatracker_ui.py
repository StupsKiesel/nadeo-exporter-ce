"""Experimental GPU MediaTracker workspace over the existing map documents."""

import json
import logging
import math

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

import gpuui as ui
from gpuui.widgets.custom import CustomWidget

from . import ADDON_ID, _mediatracker as media
from ._map_panel import map_root


log = logging.getLogger("trackmania_blender_bridge.mediatracker_ui")
_sessions = {}

# Concrete MediaTracker block classes available in the track palette.
TRACK_TYPES = (
    ("Cameras", (
        (0x03084000, "Game Camera"), (0x030A0000, "Orbital Camera"),
        (0x030A1000, "Camera Path"), (0x030A2000, "Custom Camera"),
        (0x030A4000, "Camera Shake"), (0x03166000, "Inertial Tracking"),
        (0x03161000, "Camera Script"), (0x0316D000, "Camera Blend"),
        (0x03139000, "Camera Map"),
    )),
    ("Media", (
        (0x030A8000, "Text"), (0x030A5000, "Image"), (0x030A7000, "Sound"),
        (0x030A6000, "Music Effect"), (0x030AB000, "Fade"),
    )),
    ("Effects", (
        (0x03126000, "Depth of Field"), (0x03128000, "HDR Bloom"),
        (0x03186000, "Color Grading"), (0x03080000, "Color Effects"),
        (0x03127000, "Tone Mapping"), (0x03199000, "Fog"),
        (0x03165000, "Dirty Lens"), (0x0307F000, "Blur"),
        (0x03082000, "Motion Blur"), (0x03081000, "Depth Blur"),
        (0x03083000, "Bloom"), (0x03024000, "3D Stereo"),
    )),
    ("Time and World", (
        (0x03085000, "Time"), (0x03129000, "Time Speed"),
        (0x0329F000, "Entity / Ghost"), (0x030E5000, "Legacy Ghost"),
        (0x03195000, "Interface"), (0x0312A000, "Manialink"),
        (0x030A9000, "Trails"), (0x030C0000, "Lightmap"),
        (0x030EB000, "Spectators"), (0x03133000, "Vehicle Light"),
        (0x0304B000, "2D Triangles"), (0x0304C000, "3D Triangles"),
    )),
)
_TRACK_TYPE_NAMES = {f"{class_id:08X}": label for _category, types in TRACK_TYPES for class_id, label in types}
_ABSTRACT_TRACK_TYPES = {"0307F000"}
_PLAYBACK_OPTIONS = (
    ("isKeepPlaying", "Keep last block active"),
    ("isCycling", "Loop segment"),
)


def _area_key(context):
    return context.window.as_pointer(), context.area.as_pointer()


def is_open(context):
    session = _sessions.get(_area_key(context))
    return session is not None and session.host.is_open


def close_all():
    for session in list(_sessions.values()):
        session.close()


@bpy.app.handlers.persistent
def _before_load(_unused):
    close_all()


def register():
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)
    bpy.types.OUTLINER_MT_object.append(_outliner_context_menu)


def unregister():
    close_all()
    bpy.types.OUTLINER_MT_object.remove(_outliner_context_menu)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)


def _developer_enabled(context):
    addon = context.preferences.addons.get(ADDON_ID)
    return addon is not None and addon.preferences.developer_ui


def _context_tracker(context):
    obj = getattr(context, "id", None)
    if obj is None:
        obj = context.active_object
    return obj if isinstance(obj, bpy.types.Object) and media._is_tracker(None, obj) else None


def _outliner_context_menu(self, context):
    root = _context_tracker(context)
    if root is not None:
        self.layout.separator()
        op = self.layout.operator(
            TMBRIDGE_OT_EditMediaTracker.bl_idname, text="Edit MediaTracker", icon="NLA",
        )
        op.target = root.name


_ENDPOINT_METADATA = {
    "time", "start", "end", "version", "classId", "PackDescVersion", "recordData",
    "numKeys", "numVerts", "faces",
}


def _endpoint_fields(block, time):
    rows = []
    metadata = _ENDPOINT_METADATA

    def fields(value, path, label):
        if isinstance(value, dict):
            for name, child in value.items():
                if name not in metadata and not str(name).startswith("_"):
                    fields(child, (*path, name), f"{label}.{name}" if label else str(name))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                fields(child, (*path, index), f"{label}[{index}]")
        else:
            if isinstance(value, (str, bool, int, float)):
                rows.append((path, label, value))

    def visit(value, path, label=""):
        if isinstance(value, dict):
            if "time" in value:
                key_time = value["time"]
                if type(key_time) in (int, float) and math.isclose(key_time, time, rel_tol=0, abs_tol=1e-6):
                    fields(value, path, "")
                return
            times = value.get("times")
            positions = value.get("verticesPositions")
            if isinstance(times, list) and isinstance(positions, list):
                for index, key_time in enumerate(times):
                    if index < len(positions) and math.isclose(key_time, time, rel_tol=0, abs_tol=1e-6):
                        fields(positions[index], (*path, "verticesPositions", index), "verticesPositions")
            for name, child in value.items():
                if name not in metadata and name not in {"times", "verticesPositions"} and not str(name).startswith("_"):
                    visit(child, (*path, name), f"{label}.{name}" if label else str(name))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, (*path, index), f"{label}[{index}]")
        else:
            fields(value, path, label)

    if time is not None:
        for class_id, chunk in block.chunks.items():
            visit(chunk, (class_id,))
    return rows


def _endpoint_values(block, time):
    return [
        f"{label}: {value:.6g}" if isinstance(value, float) else f"{label}: {value}"
        for _path, label, value in _endpoint_fields(block, time)
    ]


def _walk_keyed_values(block, found):
    """Call found(container, name) for every keyed time in the block's chunks."""

    def visit(value):
        if isinstance(value, dict):
            if "time" in value:
                found(value, "time")
                return
            if isinstance(value.get("times"), list) and isinstance(value.get("verticesPositions"), list):
                found(value, "times")
            for name, child in value.items():
                if name not in _ENDPOINT_METADATA and name not in {"times", "verticesPositions"} and not str(name).startswith("_"):
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for chunk in block.chunks.values():
        visit(chunk)


def _key_times(block):
    """Sorted key times strictly inside the block's start/end handles."""
    times = set()

    def found(container, name):
        values = container[name] if name == "times" else [container[name]]
        times.update(float(time) for time in values if type(time) in (int, float))

    _walk_keyed_values(block, found)
    start, end = media._time_range(block)
    return sorted(
        time for time in times if start < time < end
        and not math.isclose(time, start, rel_tol=0, abs_tol=1e-6)
        and not math.isclose(time, end, rel_tol=0, abs_tol=1e-6)
    )


def _move_key_time(block, old, new):
    def matches(time):
        return type(time) in (int, float) and math.isclose(float(time), old, rel_tol=0, abs_tol=1e-6)

    def found(container, name):
        if name == "times":
            container[name] = [new if matches(time) else time for time in container[name]]
        elif matches(container[name]):
            container[name] = new

    _walk_keyed_values(block, found)


class _SelectionSurface(ui.TimelineSurface):
    def __init__(self, *lanes, editor, **settings):
        self.editor = editor
        super().__init__(*lanes, **settings)

    def handle_event(self, host, event, x, y):
        if self._gesture is None and event.type == "LEFTMOUSE" and event.value == "PRESS" and x < self.rect[0] + self.header_width * host._scale:
            for index, lane in enumerate(self.children):
                if host._contains(host._intersection(lane.rect, self.clip), x, y):
                    root = self.editor.tracker()
                    root.tm_mediatracker.selected_track = index
                    root.tm_mediatracker.selected_block = 0
                    for block in self.editor.blocks:
                        block.selected = False
                    self.editor.describe_selection()
                    host.host.invalidate()
                    return True
        if self._gesture is None and event.type == "LEFTMOUSE" and event.value == "PRESS":
            item = self._hit_item(host, x, y)
            if item is not None and not item.timing_editable:
                for block in self.editor.blocks:
                    block.selected = block is item
                item.selected_handle = item.hit_handle(x, host._scale)
                root = self.editor.tracker()
                root.tm_mediatracker.selected_track, root.tm_mediatracker.selected_block = item.identity
                self.editor.describe_selection()
                return True
        handled = super().handle_event(host, event, x, y)
        if handled and event.type == "LEFTMOUSE" and event.value == "PRESS":
            selected = next((
                item for lane in self.children for item in lane.children if item.selected
            ), None)
            if selected is not None:
                root = self.editor.tracker()
                root.tm_mediatracker.selected_track, root.tm_mediatracker.selected_block = selected.identity
                self.editor.describe_selection()
                self.editor.highlight_track()
        return handled

    def draw(self, painter, clip):
        self.editor.highlight_track()
        super().draw(painter, clip)


class _DocumentWatch(CustomWidget):
    """Keep polling inside the host lifecycle, without another Blender timer."""

    def __init__(self, editor):
        self.editor = editor
        super().__init__(height=0)

    def draw(self, painter, clip):
        pass

    def needs_redraw(self):
        return self.editor.poll()


class _TrackActions(ui.Flow):
    def natural_width(self, scale, text):
        return 180 * scale


class _SlotSelector(ui.Dropdown):
    def natural_width(self, scale, text):
        return 200 * scale


class _RightActions(ui.Flow):
    def arrange(self, x, top, width, scale, font_size=13):
        natural = sum(child.natural for child in self.children) + self.gap * scale * max(0, len(self.children) - 1)
        super().arrange(x + max(0, width - natural), top, min(width, natural), scale, font_size)


class MediaTrackerEditor:
    def __init__(self, context):
        self.key = _area_key(context)
        self.scene_name = context.scene.name
        self.collection_name = ""
        self.root_name = ""
        self.signature = None
        self.axis = ui.TimeAxis(snap=context.scene.render.fps_base / context.scene.render.fps)
        self.surface = None
        self.show_track_palette = False
        self.region_space = context.area.spaces.active
        self.region_visibility = None
        self.host = ui.blender.BlenderHost(ui.Root(), on_close=self.restore_regions)
        self.rebuild(context)

    def open(self, context):
        self.region_visibility = (
            self.region_space.show_region_ui, self.region_space.show_region_toolbar,
        )
        self.region_space.show_region_ui = False
        self.region_space.show_region_toolbar = False
        try:
            self.host.open(context)
        except Exception:
            self.close()
            raise

    def restore_regions(self):
        if self.region_visibility is not None:
            visibility, self.region_visibility = self.region_visibility, None
            try:
                self.region_space.show_region_ui, self.region_space.show_region_toolbar = visibility
            except ReferenceError:
                log.warning("MediaTracker viewport was removed before its panels could be restored")
        _sessions.pop(self.key, None)

    def tracker(self):
        scene = bpy.data.scenes.get(self.scene_name)
        return scene.objects.get(self.root_name) if scene else None

    def slots(self):
        from GBXBPY.mediatracker import MAP_MEDIA_SLOTS

        collection = bpy.data.collections.get(self.collection_name)
        return [
            (slot, label, next((
                obj for obj in collection.all_objects
                if obj.type == "EMPTY" and obj.get("tm_mediatracker_slot") == slot
            ), None) if collection else None)
            for slot, label in MAP_MEDIA_SLOTS
        ]

    def choose(self, context, root):
        for obj in context.selected_objects:
            obj.select_set(False)
        root.select_set(True)
        context.view_layer.objects.active = root
        self.root_name = root.name
        self.show_track_palette = False
        self.axis.origin = 0
        self.rebuild(context)

    def choose_slot(self, context, name):
        scene = bpy.data.scenes.get(self.scene_name)
        root = scene.objects.get(name) if scene else None
        if root is None or name not in {obj.name for _slot, _label, obj in self.slots() if obj is not None}:
            raise ValueError("The selected map MediaTracker slot is no longer available")
        self.choose(context, root)

    def _resolve(self, context):
        selected = media._selected_tracker(context)
        collection = map_root(context.scene)
        self.collection_name = collection.name if collection else ""
        slots = self.slots()
        available = [obj for _slot, _label, obj in slots if obj is not None]
        if selected is not None and (selected in available or selected.get("tm_mediatracker_slot") == "standalone"):
            self.root_name = selected.name
        elif self.tracker() not in available:
            self.root_name = available[0].name if available else ""

    def _signature(self):
        root = self.tracker()
        return (
            self.root_name, self.collection_name,
            root.get("tm_mediatracker_document") if root else None,
        )

    def poll(self):
        context = bpy.context
        if context.scene.name != self.scene_name:
            self.close()
            return False
        self.axis.current_time = (context.scene.frame_current_final - 1) * context.scene.render.fps_base / context.scene.render.fps
        playing = context.screen is not None and context.screen.is_animation_playing
        icon = "PAUSE" if playing else "PLAY"
        time_text = self.time_text()
        transport_changed = self.play_button.icon != icon or self.time_label.text != time_text
        self.play_button.icon = icon
        self.play_button.tooltip = "Pause preview" if playing else "Play preview"
        self.time_label.text = time_text
        if self.surface is not None and self.surface._gesture is not None:
            return transport_changed
        self._resolve(context)
        if self._signature() != self.signature:
            # _poll_changes returns immediately after True, so replacing the tree
            # here never mutates its active widget iteration.
            self.rebuild(context)
            return True
        return transport_changed

    def time_text(self):
        seconds = max(0, self.axis.current_time)
        minutes, seconds = divmod(seconds, 60)
        return f"{int(minutes):02d}:{seconds:06.3f}"

    def rebuild(self, context):
        self._resolve(context)
        handles = {
            item.identity: item.selected_handle for item in self.blocks
        } if self.signature is not None and self.signature[0] == self.root_name else {}
        offset = (
            self.surface.offset if self.surface is not None
            and self.signature is not None and self.signature[0] == self.root_name else 0
        )
        root = self.tracker()
        self.signature = self._signature()
        self.document = media._read_document(root) if root is not None else None
        lanes = []
        self.blocks = []
        if self.document is not None:
            for track_index, track in enumerate(self.document.clip.tracks):
                blocks = []
                for block_index, block in enumerate(track.blocks):
                    start, end = media._time_range(block)
                    item = ui.TimeBlock(
                        start, end, _TRACK_TYPE_NAMES.get(f"{block.class_id:08X}", f"0x{block.class_id:08X}"),
                        selected=(track_index, block_index) == (
                            root.tm_mediatracker.selected_track, root.tm_mediatracker.selected_block,
                        ),
                        keys=_key_times(block),
                    )
                    item.key_source = tuple(item.keys)
                    item.identity = track_index, block_index
                    item.selected_handle = handles.get(item.identity)
                    if isinstance(item.selected_handle, int) and item.selected_handle >= len(item.keys):
                        item.selected_handle = None
                    item.timing_editable = (
                        block.timeline_start is not None and block.timeline_end is not None
                        and block.timeline_end > block.timeline_start
                    )
                    if not item.timing_editable:
                        item.text += " (untimed)"
                    blocks.append(item)
                    self.blocks.append(item)
                lanes.append(ui.TimelineLane(*blocks, text=track.name or f"Track {track_index + 1}"))
        self.surface = _SelectionSurface(
            *lanes, editor=self, axis=self.axis, height=180, header_width=180,
            undo=False, on_change=lambda _ctx, _event: self.describe_selection(),
            on_commit=self.commit, key="mt-surface",
        )
        self.surface.offset = offset
        self.inspector = ui.Label("")
        self.block_options = ui.Column(key="mt-block-options")
        self.handle_values = ui.Column(key="mt-handle-values")
        self.status = ui.Label("Map writeback is not supported. Edits are saved in the blend file.")
        slot_items = [(obj.name, label) for _slot, label, obj in self.slots() if obj is not None]
        if root is not None and root.get("tm_mediatracker_slot") == "standalone":
            slot_items.append((root.name, f"Standalone: {root.name}"))
        slot_selector = _SlotSelector(
            "Slot", binding=ui.Binding(lambda _ctx: self.root_name, self.choose_slot),
            items=slot_items or [("", "No MediaTracker slots")], enabled=bool(slot_items),
            tooltip="Select the map MediaTracker slot", undo=False, key="mt-slot-selector",
        )
        inspector_panel = ui.Panel(
            self.inspector,
            ui.ScrollArea(self.block_options, self.handle_values, height=220, key="mt-handle-inspector"),
            title="Block Inspector", width=320, mode="drawer", side="LEFT",
            collapsible=False, closable=False, key="mt-inspector",
        )
        self.play_button = ui.IconButton("PLAY", tooltip="Play preview", command=self.play, key="mt-play")
        self.time_label = ui.Label(self.time_text(), key="mt-time")
        footer = ui.Split(
                ui.Flow(
                    ui.IconButton("ARROW_LEFT", tooltip="Close MediaTracker editor", command=lambda _c, _e: self.close(), key="mt-close"),
                    ui.IconButton("IMPORT", tooltip="Import Clip into selected MediaTracker", command=self.import_clip,
                                  enabled=root is not None, key="mt-import"),
                    ui.IconButton("EXPORT", tooltip="Export standalone Clip", command=self.export_clip,
                                  enabled=root is not None and not root.tm_mediatracker.read_only, key="mt-export"),
                    ui.IconButton("REFRESH", tooltip="Reload saved MediaTracker document", command=lambda ctx, _e: self.rebuild(ctx), key="mt-refresh"),
                    gap=6,
                ),
                _RightActions(ui.IconButton("FIT", tooltip="Fit timeline to all blocks", command=self.fit, key="mt-fit")),
                ratio=0.8, key="mt-footer",
        )
        self.block_buttons = {
            action: ui.IconButton(
                icon, tooltip=tooltip,
                command=lambda ctx, _event, a=action: self.block_action(ctx, a),
                enabled=False, key=f"mt-block-{action.lower()}",
            ) for action, icon, tooltip in (
                ("INSERT", "ADD", "Insert a 3-second block in the selected track at the playhead"),
                ("CUT", "CUT", "Cut selected block at the playhead (linear boundary values)"),
                ("DELETE", "TRASH", "Delete selected block"),
            )
        }
        timeline = ui.Dock(
            ui.Flow(
                _TrackActions(
                    ui.IconButton("ADD", tooltip="Add track", command=lambda ctx, event: self.track_action(ctx, "ADD"), enabled=root is not None, key="mt-track-add"),
                    ui.IconButton("REMOVE", tooltip="Delete selected track", command=lambda ctx, event: self.track_action(ctx, "REMOVE"), enabled=bool(lanes), key="mt-track-remove"),
                    ui.IconButton("RENAME", tooltip="Rename selected track", command=lambda ctx, event: self.track_action(ctx, "RENAME"), enabled=bool(lanes), key="mt-track-rename"),
                    ui.IconButton("TRASH", tooltip="Delete all tracks in this MediaTracker", command=lambda ctx, event: self.track_action(ctx, "CLEAR"), enabled=bool(lanes), key="mt-track-clear"),
                    gap=4,
                ),
                ui.IconButton("EYE", tooltip="View preview camera", command=self.camera, enabled=root is not None, key="mt-camera"),
                ui.IconButton("FIRST", tooltip="Go to start", command=self.start, key="mt-start"),
                self.play_button, self.time_label, slot_selector, gap=4,
            ),
            ui.Split(
                ui.Column(
                    ui.TimeRuler(axis=self.axis, header_width=180, on_change=self.scrub),
                    self.surface, gap=0, padding=0,
                ),
                ui.Column(
                    ui.Label("Block / Key Tools"),
                    ui.Flow(*self.block_buttons.values(), gap=4),
                    key="mt-block-tools",
                ),
                ratio=0.85, key="mt-timeline-body",
            ),
            self.status, footer, _DocumentWatch(self),
            title="MediaTracker - Experimental GPU Editor", height=400,
            collapsible=False, closable=False, key="mt-timeline",
        )
        panels = [timeline, inspector_panel]
        if self.show_track_palette:
            choices = []
            for index, (category, types) in enumerate(TRACK_TYPES):
                choices.append(ui.Section(
                    *(ui.Button(
                        label, command=lambda ctx, _event, type_id=f"{class_id:08X}": self.add_track_type(ctx, type_id),
                        tooltip=("Abstract base class; choose Motion Blur or Depth Blur" if f"{class_id:08X}" in _ABSTRACT_TRACK_TYPES
                                 else f"Add {label} with a 3-second block at the playhead"),
                        enabled=f"{class_id:08X}" not in _ABSTRACT_TRACK_TYPES, key=f"mt-type-{class_id:08X}",
                    ) for class_id, label in types),
                    text=category, expanded=False, key=f"mt-track-category-{index}",
                ))
            region = next(region for region in context.area.regions if region.type == "WINDOW")
            scale = max(0.5, context.preferences.system.ui_scale)
            panels.append(ui.Panel(
                ui.Label("Select a track type"),
                ui.ScrollArea(*choices, height=280, key="mt-track-type-list"),
                ui.Button("Cancel", command=lambda ctx, _event: self.toggle_track_palette(ctx), key="mt-track-type-cancel"),
                title="Add Track", width=290, x=max(20, region.width / scale - 320),
                y=40, closable=False, key="mt-track-palette",
            ))
        self.host.root = ui.Root(*panels)
        self.describe_selection()
        self.highlight_track()
        self.host.invalidate()

    def highlight_track(self):
        root = self.tracker()
        for index, lane in enumerate(self.surface.children):
            lane.selected = root is not None and index == root.tm_mediatracker.selected_track

    def track_action(self, context, action):
        root = self.tracker()
        if root is None:
            raise ValueError("Select a MediaTracker first")
        if action == "ADD":
            self.toggle_track_palette(context)
            return
        index = root.tm_mediatracker.selected_track
        name = self.document.clip.tracks[index].name if index < len(self.document.clip.tracks) else "Track"
        bpy.ops.trackmania_blender_bridge.mediatracker_track_action(
            "INVOKE_DEFAULT", target=root.name, expected=root["tm_mediatracker_document"],
            action=action, track_index=index, track_name="Track" if action == "ADD" else name,
        )

    def toggle_track_palette(self, context):
        self.show_track_palette = not self.show_track_palette
        self.rebuild(context)

    def add_track_type(self, context, type_id):
        root = self.tracker()
        if root is None:
            raise ValueError("The target MediaTracker no longer exists")
        result = bpy.ops.trackmania_blender_bridge.mediatracker_track_action(
            target=root.name, expected=self.signature[2], action="ADD",
            track_name=_TRACK_TYPE_NAMES[type_id], track_type=type_id,
        )
        if result != {"FINISHED"}:
            raise RuntimeError("Could not add MediaTracker track")
        self.show_track_palette = False
        self.rebuild(context)
        item = self.blocks[-1]
        item.selected_handle = "start"
        scale = max(0.5, context.preferences.system.ui_scale)
        self.host._runtime._refresh()
        self.surface.offset = self.surface.max_scroll(scale)
        width = self.surface.rect[2]
        if self.axis.pixel(item.start) < 0 or self.axis.pixel(item.end) > width - self.surface.header_width * scale:
            self.axis.origin = item.start
        self.describe_selection()

    def describe_selection(self):
        root = self.tracker()
        selected = next((item for item in self.blocks if item.selected), None)
        side = selected.selected_handle if selected is not None else None
        if isinstance(side, int):
            self.inspector.text = f"{selected.text} | Key {side + 1}"
        elif side:
            self.inspector.text = f"{selected.text} | {side.title()} handle"
        else:
            self.inspector.text = "Select a handle or key"
        for child in list(self.handle_values.children):
            self.handle_values.remove(child)
        for child in list(self.block_options.children):
            self.block_options.remove(child)
        if selected is not None:
            track = self.document.clip.tracks[selected.identity[0]]
            for option, label in _PLAYBACK_OPTIONS:
                state = ui.State(bool((track.settings or {}).get(option, False)))
                self.block_options.add(ui.Checkbox(
                    label, binding=state.binding(), undo=False, key=f"mt-{option}",
                    tooltip="Playback option for this block's track",
                    on_commit=lambda ctx, _event, o=option, s=state, identity=selected.identity, e=self.signature[2]:
                        self.edit_playback_option(ctx, identity, o, s.value, e),
                ))
        if selected is not None and side is not None:
            track_index, block_index = selected.identity
            block = self.document.clip.tracks[track_index].blocks[block_index]
            if isinstance(side, int):
                time = selected.keys[side]
                commit_time = self.edit_key_time
            else:
                time = block.timeline_start if side == "start" else block.timeline_end
                commit_time = self.edit_handle_time
            time_state = ui.State(selected.keys[side] if isinstance(side, int) else getattr(selected, side))
            self.handle_values.add(ui.NumberField(
                "Time (s)", binding=time_state.binding(), minimum=-1e12, maximum=1e12, undo=False,
                enabled=selected.timing_editable, key="mt-handle-time",
                on_commit=lambda ctx, _event: commit_time(ctx, selected.identity, side, time_state.value),
            ))
            fields = _endpoint_fields(block, time)
            for index, (path, label, value) in enumerate(fields):
                state = ui.State(value)
                control = ui.Checkbox if isinstance(value, bool) else ui.TextField if isinstance(value, str) else ui.NumberField
                settings = {} if isinstance(value, (str, bool)) else {"minimum": -1e20, "maximum": 1e20, "step": 1 if isinstance(value, int) else 0.1}
                expected = self.signature[2]
                self.handle_values.add(control(
                    label, binding=state.binding(), undo=False, tooltip=label,
                    key=f"mt-handle-field-{index}", **settings,
                    on_commit=lambda ctx, _event, p=path, s=state, identity=selected.identity, e=expected:
                        self.edit_handle_value(ctx, identity, side, p, s.value, e),
                ))
            if not fields:
                self.handle_values.add(ui.Label("No decoded endpoint values"))
        if root is not None and selected is not None:
            root.tm_mediatracker.selected_track, root.tm_mediatracker.selected_block = selected.identity
        track_index = root.tm_mediatracker.selected_track if root is not None else -1
        track = self.document.clip.tracks[track_index] if (
            self.document is not None and 0 <= track_index < len(self.document.clip.tracks)
        ) else None
        self.block_buttons["INSERT"].enabled = track is not None and bool(track.blocks or track.properties.get("gpuui_track_class_id"))
        self.block_buttons["CUT"].enabled = selected is not None and selected.timing_editable
        self.block_buttons["DELETE"].enabled = selected is not None
        self.host.invalidate()

    def block_action(self, context, action):
        root = self.tracker()
        if root is None:
            raise ValueError("Select a MediaTracker first")
        selected = next((item for item in self.blocks if item.selected), None)
        if action != "INSERT" and selected is None:
            raise ValueError("Select a block first")
        result = bpy.ops.trackmania_blender_bridge.mediatracker_block_action(
            target=root.name, expected=self.signature[2], action=action,
            track_index=root.tm_mediatracker.selected_track,
            block_index=selected.identity[1] if selected is not None else 0,
        )
        if result != {"FINISHED"}:
            raise RuntimeError("MediaTracker block action failed; see Blender's error report")
        self.rebuild(context)
        selected = next((item for item in self.blocks if item.selected), None)
        if selected is not None:
            selected.selected_handle = "start"
        self.describe_selection()

    def edit_playback_option(self, context, identity, option, value, expected):
        result = bpy.ops.trackmania_blender_bridge.mediatracker_playback_option(
            target=self.root_name, expected=expected, track_index=identity[0],
            option=option, value=value,
        )
        if result != {"FINISHED"}:
            raise RuntimeError("MediaTracker playback edit failed; see Blender's error report")
        self.rebuild(context)

    def edit_handle_time(self, context, identity, side, value):
        item = next(item for item in self.blocks if item.identity == identity)
        if not math.isfinite(value) or (side == "start" and value >= item.end) or (side == "end" and value <= item.start):
            raise ValueError("Handle time must preserve a positive segment duration")
        setattr(item, side, value)
        self.commit(context, None)

    def edit_key_time(self, context, identity, index, value):
        result = bpy.ops.trackmania_blender_bridge.edit_mediatracker_key_time(
            target=self.root_name, expected=self.signature[2], track_index=identity[0],
            block_index=identity[1], key_index=index, value=json.dumps(value),
        )
        if result != {"FINISHED"}:
            raise RuntimeError("MediaTracker key edit failed; see Blender's error report")
        self.rebuild(context)

    def edit_handle_value(self, context, identity, side, path, value, expected):
        result = bpy.ops.trackmania_blender_bridge.edit_mediatracker_handle(
            target=self.root_name, expected=expected, track_index=identity[0], block_index=identity[1],
            side="key" if isinstance(side, int) else side,
            key_index=side if isinstance(side, int) else 0,
            path=json.dumps(path), value=json.dumps(value),
        )
        if result != {"FINISHED"}:
            raise RuntimeError("MediaTracker handle edit failed; see Blender's error report")
        self.rebuild(context)

    def commit(self, context, _event):
        for item in self.blocks:
            block = self.document.clip.tracks[item.identity[0]].blocks[item.identity[1]]
            if (item.start, item.end) != media._time_range(block):
                continue
            # Only key times moved; range retiming below would not persist them.
            index = next((i for i, (old, new) in enumerate(zip(item.key_source, item.keys))
                          if not math.isclose(old, new, rel_tol=0, abs_tol=1e-9)), None)
            if index is not None:
                self.edit_key_time(context, item.identity, index, item.keys[index])
                return
        ranges = [[*item.identity, item.start, item.end] for item in self.blocks]
        result = bpy.ops.trackmania_blender_bridge.commit_mediatracker_gpu(
            target=self.root_name, expected=self.signature[2], ranges=json.dumps(ranges),
        )
        if result != {"FINISHED"}:
            raise RuntimeError("MediaTracker edit failed; see Blender's error report")
        self.rebuild(context)

    def _select_target(self, context):
        root = self.tracker()
        if root is None:
            raise ValueError("The MediaTracker no longer exists")
        for obj in context.selected_objects:
            obj.select_set(False)
        root.select_set(True)
        context.view_layer.objects.active = root
        return root

    def import_clip(self, context, _event):
        self._select_target(context)
        bpy.ops.trackmania_blender_bridge.import_media_clip("INVOKE_DEFAULT")

    def export_clip(self, context, _event):
        self._select_target(context)
        bpy.ops.trackmania_blender_bridge.export_media_clip("INVOKE_DEFAULT")

    def camera(self, context, _event):
        self._select_target(context)
        media._update_preview_camera(context.scene)
        bpy.ops.trackmania_blender_bridge.view_mediatracker_camera()

    def play(self, context, _event):
        bpy.ops.screen.animation_play()

    def start(self, context, event):
        self.axis.current_time = 0
        self.scrub(context, event)

    def scrub(self, context, _event):
        frame = media._frame(self.axis.current_time, context.scene)
        context.scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))

    def fit(self, context, _event):
        start = min((item.start for item in self.blocks), default=0)
        end = max((item.end for item in self.blocks), default=10)
        self.axis.origin = start
        width = max(1, self.surface.rect[2] - self.surface.header_width * max(0.5, context.preferences.system.ui_scale) - 20)
        self.axis.pixels_per_second = max(0.01, width / max(1, end - start))
        self.host.invalidate()

    def close(self):
        self.host.close()
        self.restore_regions()


class TMBRIDGE_OT_CommitMediaTrackerGPU(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.commit_mediatracker_gpu"
    bl_label = "Edit MediaTracker Timing"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    ranges: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        try:
            root = context.scene.objects.get(self.target)
            if root is None or not root.get("tm_mediatracker_slot"):
                raise ValueError("MediaTracker target no longer exists")
            if root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed during the gesture; refresh and try again")
            document = media._read_document(root)
            ranges = json.loads(self.ranges)
            identities = {(ti, bi) for ti, track in enumerate(document.clip.tracks) for bi, _block in enumerate(track.blocks)}
            seen = set()
            for track_index, block_index, start, end in ranges:
                if type(track_index) is not int or type(block_index) is not int:
                    raise ValueError("Block identities must be integer indices")
                identity = track_index, block_index
                if identity not in identities or identity in seen:
                    raise ValueError("Invalid or duplicate block identity")
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (start, end)) or end <= start:
                    raise ValueError("Block times must be finite, with end greater than start")
                seen.add(identity)
                block = document.clip.tracks[track_index].blocks[block_index]
                old_start, old_end = media._time_range(block)
                if (block.timeline_start is None or block.timeline_end is None or block.timeline_end <= block.timeline_start) and (start, end) != (old_start, old_end):
                    raise ValueError("Blocks without a duration cannot be retimed")
                for chunk in block.chunks.values():
                    media._retime(chunk, old_start, old_end, start, end)
            if seen != identities:
                raise ValueError("Timing edits must include every existing block")
            media._build_preview(context.scene, document, root=root)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            log.exception("Could not commit GPU MediaTracker edit")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_EditMediaTrackerHandle(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.edit_mediatracker_handle"
    bl_label = "Edit MediaTracker Handle"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    track_index: IntProperty(min=0)
    block_index: IntProperty(min=0)
    side: EnumProperty(items=[("start", "Start", ""), ("end", "End", ""), ("key", "Key", "")])
    key_index: IntProperty(min=0)
    path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    value: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        try:
            root = context.scene.objects.get(self.target)
            if root is None or root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed; refresh before editing the handle")
            document = media._read_document(root)
            block = document.clip.tracks[self.track_index].blocks[self.block_index]
            path = tuple(json.loads(self.path))
            value = json.loads(self.value)
            if self.side == "key":
                keys = _key_times(block)
                if self.key_index >= len(keys):
                    raise ValueError("The selected key no longer exists")
                time = keys[self.key_index]
            else:
                time = block.timeline_start if self.side == "start" else block.timeline_end
            fields = {p: old for p, _label, old in _endpoint_fields(block, time)}
            if path not in fields:
                raise ValueError("Field is not part of the selected endpoint")
            old = fields[path]
            if isinstance(old, bool):
                valid = isinstance(value, bool)
            elif isinstance(old, str):
                valid = isinstance(value, str)
            else:
                valid = type(value) in (int, float) and math.isfinite(value)
                if valid and isinstance(old, int):
                    valid = value == int(value)
                    value = int(value)
            if not valid:
                raise ValueError("Handle value has an invalid type or is not finite")
            container = block.chunks
            for part in path[:-1]:
                container = container[part]
            container[path[-1]] = value
            media._build_preview(context.scene, document, root=root)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            log.exception("MediaTracker handle edit failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_EditMediaTrackerKeyTime(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.edit_mediatracker_key_time"
    bl_label = "Edit MediaTracker Key Time"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    track_index: IntProperty(min=0)
    block_index: IntProperty(min=0)
    key_index: IntProperty(min=0)
    value: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        try:
            root = context.scene.objects.get(self.target)
            if root is None or root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed; refresh before moving the key")
            document = media._read_document(root)
            block = document.clip.tracks[self.track_index].blocks[self.block_index]
            keys = _key_times(block)
            if self.key_index >= len(keys):
                raise ValueError("The selected key no longer exists")
            value = json.loads(self.value)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Key time must be a finite number")
            value = float(value)
            start, end = media._time_range(block)
            low = keys[self.key_index - 1] if self.key_index else start
            high = keys[self.key_index + 1] if self.key_index + 1 < len(keys) else end
            if not low < value < high:
                raise ValueError("Key time must stay between its neighbors inside the block")
            _move_key_time(block, keys[self.key_index], value)
            media._build_preview(context.scene, document, root=root)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            log.exception("MediaTracker key time edit failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_MediaTrackerBlockAction(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.mediatracker_block_action"
    bl_label = "MediaTracker Block Action"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    action: EnumProperty(items=[(name, label, "") for name, label in (
        ("INSERT", "Insert block"), ("CUT", "Cut block at playhead"), ("DELETE", "Delete block"),
    )])
    track_index: IntProperty(min=0)
    block_index: IntProperty(min=0)

    def execute(self, context):
        try:
            root = context.scene.objects.get(self.target)
            if root is None or root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed; refresh before editing blocks")
            document = media._read_document(root)
            track = document.clip.tracks[self.track_index]
            index = self.block_index
            time = (context.scene.frame_current_final - 1) * context.scene.render.fps_base / context.scene.render.fps
            if self.action == "INSERT":
                from GBXBPY.mediatracker_defaults import create_media_block

                class_id = track.properties.get("gpuui_track_class_id")
                if class_id is None:
                    if not track.blocks:
                        raise ValueError("The empty track has no known block type; add a typed track first")
                    class_id = track.blocks[min(index, len(track.blocks) - 1)].class_id
                block = create_media_block(class_id, time)
                index = next((i for i, existing in enumerate(track.blocks)
                              if existing.timeline_start is not None and existing.timeline_start > time),
                             len(track.blocks))
                track.blocks.insert(index, block)
            elif self.action == "CUT":
                from GBXBPY.mediatracker_editing import split_media_block

                track.blocks[index:index + 1] = split_media_block(track.blocks[index], time)
                index += 1
            elif self.action == "DELETE":
                block = track.blocks.pop(index)
                if not track.blocks:
                    track.properties["gpuui_track_class_id"] = block.class_id
                index = min(index, max(0, len(track.blocks) - 1))
            document.editable = False
            root.tm_mediatracker.selected_track = self.track_index
            root.tm_mediatracker.selected_block = index
            media._build_preview(context.scene, document, root=root)
            for session in list(_sessions.values()):
                if session.scene_name == context.scene.name and session.root_name == root.name:
                    session.rebuild(context)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            log.exception("MediaTracker block action failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_MediaTrackerPlaybackOption(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.mediatracker_playback_option"
    bl_label = "Edit MediaTracker Playback Option"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    track_index: IntProperty(min=0)
    option: EnumProperty(items=[(name, label, "") for name, label in _PLAYBACK_OPTIONS])
    value: BoolProperty()

    def execute(self, context):
        try:
            root = context.scene.objects.get(self.target)
            if root is None or root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed; refresh before editing playback options")
            document = media._read_document(root)
            track = document.clip.tracks[self.track_index]
            if track.settings is None:
                track.settings = {
                    "version": 0, "isKeepPlaying": False, "isReadOnly": False, "isCycling": False,
                }
                document.editable = False
            track.settings[self.option] = self.value
            media._build_preview(context.scene, document, root=root)
            for session in list(_sessions.values()):
                if session.scene_name == context.scene.name and session.root_name == root.name:
                    session.rebuild(context)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            log.exception("MediaTracker playback option edit failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_MediaTrackerTrackAction(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.mediatracker_track_action"
    bl_label = "MediaTracker Track Action"
    bl_options = {"INTERNAL", "UNDO"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    expected: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    action: EnumProperty(items=[(name, label, "") for name, label in (
        ("ADD", "Add track"), ("REMOVE", "Delete selected track"),
        ("RENAME", "Rename track"), ("CLEAR", "Delete all tracks"),
    )])
    track_index: IntProperty(min=0)
    track_name: StringProperty(name="Track name", default="Track")
    track_type: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        if self.action in {"ADD", "RENAME"}:
            return context.window_manager.invoke_props_dialog(self)
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        self.layout.prop(self, "track_name")

    def execute(self, context):
        try:
            from GBXBPY.model.mediatracker import MediaTrackerTrack

            root = context.scene.objects.get(self.target)
            if root is None or not root.get("tm_mediatracker_slot"):
                raise ValueError("MediaTracker no longer exists")
            if root.get("tm_mediatracker_document") != self.expected:
                raise ValueError("MediaTracker changed while the dialog was open; try again")
            document = media._read_document(root)
            tracks = document.clip.tracks
            if self.action in {"ADD", "RENAME"} and not self.track_name.strip():
                raise ValueError("Track name must not be empty")
            if self.track_type and self.track_type not in _TRACK_TYPE_NAMES:
                raise ValueError("Unknown MediaTracker track type")
            if self.action in {"RENAME", "REMOVE"} and self.track_index >= len(tracks):
                raise ValueError("Selected track no longer exists")
            index = self.track_index
            if self.action == "ADD":
                index = len(tracks)
                properties = {"gpuui_track_class_id": int(self.track_type, 16)} if self.track_type else {}
                blocks = []
                if self.track_type:
                    from GBXBPY.mediatracker_defaults import create_media_block

                    start = (context.scene.frame_current_final - 1) * context.scene.render.fps_base / context.scene.render.fps
                    blocks.append(create_media_block(int(self.track_type, 16), start))
                tracks.append(MediaTrackerTrack(name=self.track_name.strip(), blocks=blocks, properties=properties))
            elif self.action == "RENAME":
                tracks[index].name = self.track_name.strip()
            elif self.action == "REMOVE":
                del tracks[index]
                index = min(index, max(0, len(tracks) - 1))
            elif self.action == "CLEAR":
                tracks.clear()
                index = 0
            if self.action != "RENAME":
                # The preserving standalone writer cannot rebuild node topology.
                document.editable = False
            root.tm_mediatracker.selected_track = index
            root.tm_mediatracker.selected_block = 0
            media._build_preview(context.scene, document, root=root)
            for session in list(_sessions.values()):
                if session.scene_name == context.scene.name and session.root_name == root.name:
                    session.rebuild(context)
            return {"FINISHED"}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            log.exception("MediaTracker track action failed")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_EditMediaTracker(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.edit_mediatracker"
    bl_label = "Edit MediaTracker"
    bl_description = "Edit this MediaTracker in the GPU timeline in a 3D View"
    bl_options = {"REGISTER"}

    target: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return context.window is not None

    def execute(self, context):
        from ._render_view import _find_view

        try:
            root = context.scene.objects.get(self.target) if self.target else _context_tracker(context)
            if root is None or not media._is_tracker(None, root):
                raise ValueError("Select a MediaTracker Empty in this scene")
            if not root.get("tm_mediatracker_document"):
                raise ValueError("The MediaTracker has no document to edit")
            area, region, _space = _find_view(context)
            if area is None or region is None:
                raise ValueError("Open a 3D View to edit this MediaTracker")
            with context.temp_override(area=area, region=region):
                session = _sessions.get(_area_key(context))
                if session is not None and session.host.is_open:
                    session.choose(context, root)
                else:
                    for obj in context.selected_objects:
                        obj.select_set(False)
                    root.select_set(True)
                    context.view_layer.objects.active = root
                    session = MediaTrackerEditor(context)
                    session.open(context)
                    _sessions[session.key] = session
            area.tag_redraw()
            return {"FINISHED"}
        except (ValueError, RuntimeError) as exc:
            log.exception("Could not edit MediaTracker from the Outliner")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class TMBRIDGE_OT_ToggleMediaTrackerGPU(bpy.types.Operator):
    bl_idname = "trackmania_blender_bridge.toggle_mediatracker_gpu"
    bl_label = "Toggle MediaTracker GPU Editor"
    bl_description = "Open or close the experimental MediaTracker workspace in this 3D View"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D" and _developer_enabled(context)

    def execute(self, context):
        if is_open(context):
            _sessions[_area_key(context)].close()
            return {"FINISHED"}
        try:
            session = MediaTrackerEditor(context)
            session.open(context)
            _sessions[session.key] = session
            return {"FINISHED"}
        except Exception as exc:
            log.exception("Could not open GPU MediaTracker editor")
            self.report({"ERROR"}, f"Could not open MediaTracker: {exc}")
            return {"CANCELLED"}


CLASSES = (
    TMBRIDGE_OT_CommitMediaTrackerGPU, TMBRIDGE_OT_EditMediaTrackerHandle,
    TMBRIDGE_OT_EditMediaTrackerKeyTime,
    TMBRIDGE_OT_MediaTrackerBlockAction, TMBRIDGE_OT_MediaTrackerPlaybackOption,
    TMBRIDGE_OT_MediaTrackerTrackAction, TMBRIDGE_OT_EditMediaTracker,
    TMBRIDGE_OT_ToggleMediaTrackerGPU,
)
