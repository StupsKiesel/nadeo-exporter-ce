"""Bake the authored seated pilot pose with all recorded mesh blend influences."""

from dataclasses import replace
import math

from mathutils import Matrix, Quaternion, Vector

from GBXBPY.model.common import Vec3


def seated_mesh(driver):
    bind, posed = {}, {}
    for joint in driver.joints:
        reference = Matrix(joint.basis).to_4x4()
        reference.translation = Vector(joint.position.as_tuple())
        bind[joint.name] = reference
        if joint.name in driver.local_pose:
            rotation, position = driver.local_pose[joint.name]
            local = Quaternion((rotation.w, rotation.x, rotation.y, rotation.z)).to_matrix().to_4x4()
            local.translation = Vector(position.as_tuple())
        else:
            local = bind[joint.parent].inverted() @ reference if joint.parent else reference
        posed[joint.name] = posed[joint.parent] @ local if joint.parent else local
    skin = [posed[name] @ bind[name].inverted() for name in driver.bone_names]
    lods = []
    for lod in driver.mesh.lods:
        vertices = []
        for vertex in lod.vertices:
            influences = vertex.bone_weights or ((vertex.bone_index, 1.0),)
            if (abs(sum(weight for _, weight in influences) - 1) > 1e-4
                    or any(bone < 0 or bone >= len(skin) for bone, _ in influences)):
                raise ValueError("Driver vertex has invalid skin influences")
            position, normal = Vector(), Vector()
            for bone, weight in influences:
                position += (skin[bone] @ Vector(vertex.position.as_tuple())) * weight
                normal += (skin[bone].to_3x3() @ Vector(vertex.normal.as_tuple())) * weight
            if not all(math.isfinite(value) for value in (*position, *normal)):
                raise ValueError("Driver pose contains non-finite geometry")
            normal.normalize()
            vertices.append(replace(vertex, position=Vec3(*position), normal=Vec3(*normal),
                                    bone_index=-1, bone_weights=()))
        lods.append(replace(lod, vertices=vertices))
    return replace(driver.mesh, lods=lods)


def build_driver(driver, root, collection, resolver, mesh_builder, progress):
    progress(0, "Applying authored seated driver pose")
    obj = mesh_builder(seated_mesh(driver), resolver=resolver, source_path=driver.source_path,
                       progress=progress)
    collection.objects.link(obj)
    obj.parent = root
    obj.name = "Replay Driver"
    obj["tm_replay_driver"] = True
    obj["tm_replay_driver_source"] = driver.source_path
    obj["tm_replay_driver_animation"] = driver.animation_path
    obj["tm_replay_driver_clip"] = driver.clip_name
    obj["tm_replay_driver_note"] = "Authored seated pose; steering, gestures and corrective graph animation are not evaluated."
    progress(1, "Seated driver built")
    return obj
