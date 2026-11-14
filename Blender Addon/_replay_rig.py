"""Replay vehicle motion using authored bind bases and verified graph operations."""

from dataclasses import asdict
import json
import math

import bpy
from mathutils import Matrix, Vector

from GBXBPY.model.common import Vec3
from GBXBPY.replay_animation import (
    AnimationOperator, AxisWorkspace, ScalarExpression, distance_constraint_delta,
    spoiler_states,
)
from GBXBPY.replay_spoiler import SpoilerParameter


RIG_LIMITATIONS = (
    "Animation subset: recorded wheel spin, authored hub travel and connected distance constraints. "
    "Joint alignment and steering/damage/reactor joint poses are not yet evaluated."
)


def suspension_positions(vehicle, sample, constraints=None):
    """Global joint positions for the verified damper and distance-constraint nodes."""
    joints = {joint.name: joint for joint in vehicle.joints}
    positions = {name: Vector(joint.position.as_tuple()) for name, joint in joints.items()}
    if vehicle.animation is None:
        raise ValueError("Vehicle animation graph unavailable")
    graph = vehicle.animation.graphs[0]
    values = dict(graph.defaults)
    for wheel in ("FL", "FR", "RL", "RR"):
        values[wheel + "DamperLen"] = sample.telemetry[f"wheel_{wheel.lower()}_damper_m"]

    def translate(name, delta):
        positions[name] += delta
        for child, joint in joints.items():
            if joint.parent == name:
                translate(child, delta)

    for op in graph.find("SuspSystem_Local").active_operations():
        if op.kind != 3 or not op.joint.endswith("Hub"):
            continue
        expression = ScalarExpression(op.expression)
        if not expression.inputs <= values.keys():
            raise ValueError(f"Unsupported hub inputs: {op.expression}")
        if op.operator != AnimationOperator.ADD or op.workspace != AxisWorkspace.PARENT:
            raise ValueError(f"Unsupported hub operation: {op.joint}")
        axis = Vector(op.axis.as_tuple())
        parent = joints[op.joint].parent
        if parent:
            axis = Matrix(joints[parent].basis) @ axis
        translate(op.joint, axis * expression.evaluate(values))

    if constraints is None:
        constraints = graph.find("SuspSystem_Global_HubDist").active_operations()
    for op in constraints:
        if op.kind != 6:
            continue
        a, b = op.constraint_a, op.constraint_b
        if any(name not in joints for name in (op.joint, a, b)):
            raise ValueError(f"Vehicle distance constraint needs joints: {op.joint}, {a}, {b}")
        reference = (Vector(joints[a].position.as_tuple()) - Vector(joints[b].position.as_tuple())).length
        delta = distance_constraint_delta(Vec3(*positions[a]), Vec3(*positions[b]), reference)
        translate(op.joint, Vector(delta.as_tuple()))
    return positions


def build_vehicle_rig(vehicle, marker, root, collection, frames, samples, curve, progress,
                      *, spoiler_parameter: SpoilerParameter | None = None):
    joints = {joint.name: joint for joint in vehicle.joints}
    wheels = ("FL", "FR", "RL", "RR")
    if vehicle.animation is None or not vehicle.animation.graphs:
        raise ValueError("Vehicle needs a supported authored animation graph")
    if len(joints) != len(vehicle.joints) or not vehicle.bone_names:
        raise ValueError("Vehicle needs unique named joints and mesh bone assignments")
    for name, joint in joints.items():
        if joint.parent and joint.parent not in joints:
            raise ValueError(f"Vehicle joint has missing parent: {name}")
        visited = {name}
        parent = joint.parent
        while parent:
            if parent in visited:
                raise ValueError(f"Vehicle skeleton contains a parent cycle: {name}")
            visited.add(parent)
            parent = joints[parent].parent
        matrix = Matrix(joint.basis)
        if (not all(math.isfinite(value) for row in matrix for value in row)
                or abs(matrix.determinant() - 1) > 1e-3):
            raise ValueError(f"Vehicle joint has invalid bind basis: {name}")
    local = vehicle.animation.graphs[0].find("SuspSystem_Local")
    constraints = []
    limitations = [RIG_LIMITATIONS]
    for op in vehicle.animation.graphs[0].find("SuspSystem_Global_HubDist").active_operations():
        missing = [name for name in (op.joint, op.constraint_a, op.constraint_b) if name not in joints]
        if missing:
            limitations.append(f"{op.joint} distance constraint omitted: model lacks {', '.join(missing)}.")
        else:
            constraints.append(op)
    for wheel in wheels:
        name = wheel + "Wheel"
        if name not in joints:
            raise ValueError(f"Vehicle rig needs wheel joint: {name}")
        operation = next((op for op in local.active_operations() if op.kind == 1 and op.joint == name), None)
        if (operation is None or operation.expression.strip() != wheel + "WheelRot"
                or operation.operator != AnimationOperator.ADD or operation.workspace != AxisWorkspace.LOCAL
                or operation.axis.as_tuple() != (1, 0, 0)):
            raise ValueError(f"Unsupported authored wheel operation: {name}")
        for key in ("angle_rad", "damper_m"):
            if not all(f"wheel_{wheel.lower()}_{key}" in sample.telemetry for sample in samples):
                raise ValueError(f"Replay has no decoded {wheel} {key} samples")
    positions = []
    for index, sample in enumerate(samples):
        progress(0.3 * index / max(1, len(samples)), "Evaluating authored suspension travel")
        positions.append(suspension_positions(vehicle, sample, constraints))

    armature = bpy.data.armatures.new("Replay Vehicle Rig")
    rig = bpy.data.objects.new("Replay Vehicle Rig", armature)
    collection.objects.link(rig)
    rig.parent = root
    rig.show_in_front = True
    rig["tm_replay_rig"] = True
    rig["tm_replay_rig_version"] = 3
    rig["tm_replay_rig_note"] = " ".join(limitations)
    rig["tm_replay_animation"] = json.dumps(asdict(vehicle.animation))
    previous = bpy.context.view_layer.objects.active
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        for name, joint in joints.items():
            bone = armature.edit_bones.new(name)
            bone.tail = (0, 0.1, 0)
            matrix = Matrix(joint.basis).to_4x4()
            matrix.translation = Vector(joint.position.as_tuple())
            bone.matrix = matrix
        for name, joint in joints.items():
            if joint.parent:
                armature.edit_bones[name].parent = armature.edit_bones[joint.parent]
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if rig.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")
        rig.select_set(False)
        bpy.context.view_layer.objects.active = previous

    for index, name in enumerate(vehicle.bone_names):
        group = marker.vertex_groups.get(f"TM_Bone_{index}")
        if group is not None:
            if name not in joints:
                raise ValueError(f"Vehicle rig is missing weighted joint: {name}")
            group.name = name
    modifier = marker.modifiers.new("Replay Vehicle Rig", "ARMATURE")
    modifier.object = rig
    action = bpy.data.actions.new("Replay Wheels and Suspension")
    rig.animation_data_create()
    rig.animation_data.action = action
    if hasattr(action, "slots"):
        slot = action.slots.new(id_type="OBJECT", name=rig.name)
        strip = action.layers.new("Vehicle Rig").strips.new(type="KEYFRAME")
        bag = strip.channelbag(slot, ensure=True)
        rig.animation_data.action_slot = slot
    else:
        bag = action
    for wheel in wheels:
        bone = rig.pose.bones[wheel + "Wheel"]
        bone.rotation_mode = "XYZ"
        curve(bag, bone.path_from_id("rotation_euler"), 0, frames,
              [sample.telemetry[f"wheel_{wheel.lower()}_angle_rad"] for sample in samples])
        name = wheel + "Hub"
        if name not in joints:
            raise ValueError(f"Vehicle rig is missing suspension hub: {name}")
        inverse = armature.bones[name].matrix_local.to_3x3().inverted()
        reference = Vector(joints[name].position.as_tuple())
        offsets = [inverse @ (pose[name] - reference) for pose in positions]
        for axis in range(3):
            curve(bag, rig.pose.bones[name].path_from_id("location"), axis, frames,
                  [offset[axis] for offset in offsets])
    if spoiler_parameter is None:
        limitations.append("Spoiler animation unavailable: no verified vehicle presentation parameter.")
    else:
        spoiler = vehicle.animation.graphs[0].find("Spoiler")
        operations = spoiler.active_operations()
        missing = sorted({op.joint for op in operations if op.joint not in joints})
        if missing:
            limitations.append(f"Spoiler animation omitted: model lacks {', '.join(missing)}.")
        else:
            fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
            try:
                states = spoiler_states(vehicle.animation, samples, spoiler_parameter.value, fps)
            except ValueError as exc:
                limitations.append(f"Spoiler animation unavailable: {exc}")
            else:
                times = [frames[0] + (time - samples[0].time_ms) * fps / 1000 for time, _ in states]
                for op in operations:
                    if (op.workspace != AxisWorkspace.LOCAL
                            or op.axis.as_tuple() != (1, 0, 0)
                            or (op.kind == 1 and op.operator != AnimationOperator.SET)
                            or (op.kind == 3 and op.operator != AnimationOperator.ADD)
                            or op.kind not in (1, 3)):
                        raise ValueError(f"Unsupported authored spoiler operation: {op.joint}")
                    bone = rig.pose.bones[op.joint]
                    bone.rotation_mode = "XYZ"
                    path = bone.path_from_id("rotation_euler" if op.kind == 1 else "location")
                    expression = ScalarExpression(op.expression)
                    values = [expression.evaluate(state) for _, state in states]
                    for axis, component in enumerate(op.axis.as_tuple()):
                        if component:
                            curve(bag, path, axis, times, [value * component for value in values])
                rig["tm_replay_spoiler"] = True
                rig["tm_replay_spoiler_open_normed"] = spoiler_parameter.value
                rig["tm_replay_spoiler_source"] = spoiler_parameter.source_path
                for name in ("SpoilerTop", "SpoilerTip", "SpoilerBotTip"):
                    key = f"tm_replay_{name.lower()}"
                    rig[key] = 0.0
                    curve(bag, f'["{key}"]', 0, times, [state[name] for _, state in states])
                limitations.append("Spoiler uses authored dynamics and replay contact/reactor flags, rebuilt at scene FPS; original render cadence is not recorded.")
    rig["tm_replay_rig_note"] = " ".join(limitations)
    progress(1, "Verified vehicle animation subset built")
    return rig
