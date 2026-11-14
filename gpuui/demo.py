"""Opt-in framework demo: ``from gpuui.demo import open_demo; open_demo(bpy.context)``."""

from . import (
    Button, Checkbox, Column, Dock, FileCard, FolderCard, Label, NumberField,
    Panel, Root, Row, State, TimeAxis, TimeBlock, TimelineLane, TimelineSurface,
    TimeRuler, Keyframe,
)
from .blender import BlenderHost


def create_demo():
    axis = TimeAxis(snap=0.1)
    enabled = State(True)
    number = State(2.0)
    cards = Row(
        FolderCard("Cameras", width=110, shortcut="1", badge="3"),
        FileCard("Camera Path", width=110, shortcut="2"),
    )
    controls = Panel(
        Label("Live Blender theme"),
        Checkbox("Enabled", binding=enabled.binding(), undo=False),
        NumberField("Value", binding=number.binding(), minimum=-100, maximum=100, undo=False),
        cards,
        title="GPUUI Components", width=320, x=30, y=30,
    )
    surface = TimelineSurface(
        TimelineLane(TimeBlock(0, 3, "Camera Path", keys=(1.2, 2.4)), Keyframe(1.5), text="Camera"),
        TimelineLane(TimeBlock(1, 5, "Effect"), text="Effect"),
        axis=axis, height=140, undo=False,
    )
    timeline = Dock(
        Row(Label("Timeline widgets"), Button("Reset view", command=lambda _context, _event: reset_axis(axis))),
        Column(TimeRuler(axis=axis), surface),
        title="Timeline", height=250, resizable=True,
    )
    return Root(controls, timeline)


def reset_axis(axis):
    axis.origin = axis.current_time = 0.0
    axis.pixels_per_second = 60.0


def open_demo(context):
    return BlenderHost(create_demo()).open(context)
