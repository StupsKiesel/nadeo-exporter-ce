"""Resolve replay car geometry and skin resources without Blender dependencies."""

from dataclasses import dataclass, field
import os
import posixpath

from .errors import ParseError
from .io import read_gbx
from .model.mesh import MaterialBinding, Mesh
from .model.replay import ReplayGhost
from .registry import dispatch
from .resolve import Resolver
from .resolve.skins import IMAGE_EXTENSIONS, SkinLibrary
from .coords import gbx_to_blender_basis, gbx_to_blender_vec3
from .model.common import Vec3
from .replay_animation import VehicleAnimation, decode_vehicle_animation


@dataclass
class VehicleJoint:
    name: str
    position: Vec3
    parent: str = ""
    basis: tuple[tuple[float, float, float], ...] = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


@dataclass
class ReplayVehicle:
    mesh: Mesh
    source_path: str
    skin_path: str = ""
    archive_members: tuple[str, ...] = ()
    diagnostics: list[str] = field(default_factory=list)
    joints: list[VehicleJoint] = field(default_factory=list)
    bone_names: list[str] = field(default_factory=list)
    animation: VehicleAnimation | None = None


def _resource(files: dict[str, str], name: str) -> str:
    matches = [path for key, path in files.items() if key.rsplit("/", 1)[-1] == name.casefold()]
    if len(matches) > 1:
        raise ParseError(f"Ambiguous vehicle skin resource: {name}")
    return matches[0] if matches else ""


def load_replay_vehicle(ghost: ReplayGhost, resolver: Resolver, skins: SkinLibrary,
                        *, skin_override: str = "") -> ReplayVehicle:
    if ghost.player_model != "CarSport":
        raise ParseError(f"Unsupported replay vehicle model: {ghost.player_model}")
    files: dict[str, str] = {}
    members: tuple[str, ...] = ()
    diagnostics = []
    skin_path = skin_override
    if skin_path:
        if not os.path.isfile(skin_path):
            raise ParseError(f"Vehicle skin ZIP not found: {skin_path}")
    else:
        for index, path in enumerate(ghost.skins):
            url = ghost.skin_urls[index] if index < len(ghost.skin_urls) else ""
            found = skins.locate_file(path, url)
            if found:
                if skin_path:
                    diagnostics.append(f"Additional vehicle skin not applied: {path or url}")
                else:
                    skin_path = found
            else:
                diagnostics.append(f"Replay skin unavailable: {path or url}: "
                                   f"{skins.problems.get(path or url, 'not found')}; using stock resources")
    if skin_path:
        files, members = skins.vehicle_files(skin_path)
    variant = "Stadium"
    # Stock alternative cars use the same CarSport model with a named skin.
    selected_skin = skin_path or next(iter(ghost.skins), "")
    skin_name = os.path.splitext(selected_skin.replace("\\", "/").rsplit("/", 1)[-1])[0].casefold()
    if skin_name in ("snow", "rally", "desert"):
        variant = skin_name.title()
    base = os.path.join("Skins", "Models", "CarSport", variant)
    stock_model = os.path.join(base, "Standard", "MainBody.Mesh.gbx") if variant == "Stadium" else os.path.join(base, "MainBody.Mesh.gbx")
    source = _resource(files, "MainBody.Mesh.gbx") or resolver.resolve(stock_model)
    if not source:
        raise ParseError(f"Default replay car mesh unavailable: {stock_model}")

    def external(relative):
        key = posixpath.normpath(relative.replace("\\", "/")).casefold()
        source_member = next((key for key, path in files.items() if path == source), "")
        local_key = posixpath.normpath(posixpath.join(posixpath.dirname(source_member), key))
        path = files.get(local_key) or files.get(key) or resolver.resolve(relative, source_path=source)
        if not path:
            raise ParseError(f"Vehicle mesh dependency unavailable: {relative}")
        return read_gbx(path, recursive=False)

    parsed = read_gbx(source, external_loader=external)
    if parsed.classId != 0x090BB000 or any("Parse chunk failed" in error for error in parsed._errors):
        raise ParseError(f"Vehicle mesh could not be decoded: {source}: {parsed._errors}")
    main = parsed.body[0x090BB000]
    bone_names = list(main.get("bonesNames") or [])
    skeleton = main.get("skel")
    joints = []
    if not skeleton or not skeleton.get("body"):
        folder = os.path.join(base, "Common") if variant == "Stadium" else base
        skeleton_path = _resource(files, "MainBody.Skel.Gbx") or resolver.resolve(
            os.path.join(folder, "MainBody.Skel.Gbx"))
        if skeleton_path:
            skeleton = read_gbx(skeleton_path, recursive=False)
    if skeleton and skeleton.get("body"):
        raw_joints = skeleton.body.get(0x090BA000, {}).get("joints", [])
        for joint in raw_joints:
            loc = joint.get("GlobalLoc")
            if loc is not None:
                parent_index = joint.get("parentIndex", -1)
                parent = raw_joints[parent_index].name if 0 <= parent_index < len(raw_joints) else ""
                # Serialized skeleton rows match the client's matrix-to-quaternion
                # conversion; C R C^-1 maps game Y-up to Blender Z-up.
                basis = gbx_to_blender_basis(((loc.XX, loc.XY, loc.XZ),
                                             (loc.YX, loc.YY, loc.YZ),
                                             (loc.ZX, loc.ZY, loc.ZZ)))
                joints.append(VehicleJoint(joint.name, gbx_to_blender_vec3(loc.TX, loc.TY, loc.TZ),
                                           parent, basis))
    if not joints:
        diagnostics.append("Vehicle skeleton unavailable; wheel and suspension rig cannot be built")
    folder = os.path.join(base, "Common") if variant == "Stadium" else base
    animation_path = _resource(files, "MainBody.Anim.Gbx") or resolver.resolve(
        os.path.join(folder, "MainBody.Anim.Gbx"))
    animation = None
    if animation_path:
        try:
            animation = decode_vehicle_animation(read_gbx(animation_path, recursive=False), animation_path)
        except (ParseError, ValueError) as exc:
            diagnostics.append(f"Vehicle animation unavailable: {exc}")
    else:
        diagnostics.append("Vehicle animation graph unavailable; authored suspension cannot be evaluated")
    # Car skin vertices are already in vehicle bind space. Applying the joint
    # global transforms here would translate wheels and body parts a second time.
    main.skel = None
    mesh = dispatch(parsed, source_path=source, resolver=resolver)
    if not isinstance(mesh, Mesh) or not mesh.lods:
        raise ParseError(f"Vehicle has no usable mesh geometry: {source}")
    common = os.path.join(resolver.gamedata_dir, base, "Common") if variant == "Stadium" else os.path.join(resolver.gamedata_dir, base)
    stock_files = {}
    if os.path.isdir(common):
        stock_files = {name.casefold(): os.path.join(common, name)
                       for name in os.listdir(common) if os.path.isfile(os.path.join(common, name))}
    if not mesh.materials:
        for name in main.materialsNames:
            category = str(name).rsplit("_", 1)[-1]
            textures = {}
            for suffix, role in (("B", "BaseColor"), ("D", "Diffuse"), ("N", "Normal"),
                                 ("R", "RoughMetal"), ("I", "Emissive"), ("AO", "AO")):
                candidates = [f"{category}_{suffix}{ext}" for ext in IMAGE_EXTENSIONS]
                texture = next((path for candidate in candidates
                                if (path := _resource(files, candidate))), "")
                if not texture and category == "Wheels" and skin_path:
                    texture = next((path for ext in IMAGE_EXTENSIONS
                                    if (path := _resource(files, f"Details_{suffix}{ext}"))), "")
                texture = texture or next((stock_files[c.casefold()] for c in candidates
                                           if c.casefold() in stock_files), "")
                if texture:
                    textures[role] = texture
            mesh.materials.append(MaterialBinding(
                material_name=str(name), shader_model="Glass" if category == "Glass" else "",
                inline_textures=textures))
            if not textures:
                diagnostics.append(f"Vehicle material has no supported textures: {name}")
        # DmgDecal groups also own unique body panels and rear display geometry.
        # Their shader name is not a safe geometry-visibility filter.
    return ReplayVehicle(mesh, source, skin_path, members, diagnostics, joints, bone_names, animation)
