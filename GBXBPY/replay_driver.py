"""CharacterPilot resource-prefix decoding and authored seated driver pose."""

from dataclasses import dataclass
import math
import os
import struct

from .coords import gbx_to_blender_basis, gbx_to_blender_quat, gbx_to_blender_vec3
from .errors import ParseError
from .io import read_gbx
from .model.common import Quat, Vec3
from .model.mesh import MaterialBinding, Mesh
from .registry import dispatch
from .replay_vehicle import VehicleJoint
from .resolve import Resolver


@dataclass
class ReplayDriver:
    mesh: Mesh
    source_path: str
    animation_path: str
    clip_name: str
    joints: list[VehicleJoint]
    bone_names: list[str]
    local_pose: dict[str, tuple[Quat, Vec3]]


def read_driver_resources(path: str):
    """Read only the verified resource prefix; retain the graph suffix as bytes."""
    from construct import Const, Struct, GreedyBytes, ConstructError
    from NICE.src import gbx_structs as g

    prefix = Struct(
        "chunkId" / Const(0x090B0003, g.GbxChunkId),
        "resources" / Struct(*g.body_chunks[0x090B0003].subcons[:6]),
        "unparsedGraphData" / GreedyBytes,
    )
    errors, warns = [], []
    with open(path, "rb") as stream:
        data = stream.read()
    def external(relative):
        raise ParseError(f"Driver animation has unsupported external dependency: {relative}")

    try:
        parsed = g.create_gbx_struct(prefix).parse(
            data, gbx_data={}, nodes=[], filename=path, errors=errors, warns=warns,
            load_external_file=external)
    except (ValueError, ConstructError) as exc:
        raise ParseError(f"Driver animation resources could not be decoded: {path}: {exc}") from exc
    if parsed.classId != 0x090B0000 or errors:
        raise ParseError(f"Invalid driver animation resources: {path}: {errors}")
    return parsed.body.resources


def load_replay_driver(resolver: Resolver) -> ReplayDriver:
    base = os.path.join("Skins", "Models", "CharacterPilot", "Stadium")
    source = resolver.resolve(os.path.join(base, "Male", "Player.Mesh.gbx"))
    animation = resolver.resolve(os.path.join(base, "Male", "Player.Anim.Gbx"))
    if not source or not animation:
        raise ParseError("CharacterPilot Stadium mesh/animation unavailable in GameData")
    resources = read_driver_resources(animation)
    clip_name = "Pilot01_CarSport_Next"
    clips = [r for r in resources.clipResources if r.name == clip_name]
    if len(clips) != 1:
        raise ParseError(f"Driver animation needs one authored clip: {clip_name}")
    clip = clips[0].value.body[0x09135000]
    baked = clip.baked.body[0x09132000]
    group = baked.jointGroup.body[0x09133000]
    if (baked.frameCount != 1 or len(baked.packed) != group.jointCount * 28
            or group.jointCount != len(set(group.names))):
        raise ParseError("Unsupported seated driver pose frame/joint packing")
    local_pose = {}
    for name, values in zip(group.names, struct.iter_unpack("<7f", baked.packed)):
        w, x, y, z, tx, ty, tz = values
        if (not all(math.isfinite(v) for v in values)
                or abs(w*w + x*x + y*y + z*z - 1) > 1e-3):
            raise ParseError(f"Invalid driver pose transform: {name}")
        local_pose[str(name)] = (gbx_to_blender_quat(x, y, z, w),
                                 gbx_to_blender_vec3(tx, ty, tz))
    skeletons = [r.value.body.get(0x090BA000) for r in resources.skelResources]
    if len(skeletons) != 1 or skeletons[0] is None:
        raise ParseError("Driver animation needs one authored skeleton")
    raw_joints = skeletons[0].joints
    joints = []
    for index, joint in enumerate(raw_joints):
        parent_index = joint.parentIndex
        if parent_index < -1 or parent_index >= index:
            raise ParseError(f"Invalid driver skeleton parent: {joint.name}")
        loc = joint.GlobalLoc
        joints.append(VehicleJoint(
            joint.name, gbx_to_blender_vec3(loc.TX, loc.TY, loc.TZ),
            raw_joints[parent_index].name if parent_index >= 0 else "",
            gbx_to_blender_basis(((loc.XX, loc.XY, loc.XZ),
                                  (loc.YX, loc.YY, loc.YZ),
                                  (loc.ZX, loc.ZY, loc.ZZ)))))
    parsed = read_gbx(source, recursive=False)
    if parsed.classId != 0x090BB000 or parsed._errors:
        raise ParseError(f"Driver mesh could not be decoded: {source}: {parsed._errors}")
    main = parsed.body[0x090BB000]
    bone_names = list(main.bonesNames)
    joint_names = {j.name for j in joints}
    if not set(bone_names) <= joint_names or not set(local_pose) <= joint_names:
        raise ParseError("Driver mesh/pose/skeleton names do not match")
    main.skel = None
    mesh = dispatch(parsed, source_path=source, resolver=resolver)
    if not isinstance(mesh, Mesh) or not mesh.lods:
        raise ParseError("Driver has no usable geometry")
    for name in main.materialsNames:
        category = str(name).rsplit("_", 1)[-1]
        textures = {}
        for suffix, role in (("B", "BaseColor"), ("N", "Normal"), ("R", "RoughMetal"),
                             ("E", "Emissive"), ("AO", "AO")):
            path = resolver.resolve(os.path.join(base, "Male", f"{category}_{suffix}.dds"))
            path = path or resolver.resolve(os.path.join(base, "Common", f"{category}_{suffix}.dds"))
            if path:
                textures[role] = path
        mesh.materials.append(MaterialBinding(material_name=str(name),
            shader_model="Glass" if category == "Glass" else "", inline_textures=textures))
    return ReplayDriver(mesh, source, animation, clip_name, joints, bone_names, local_pose)
