"""Blender-independent import plan. Matrices are row-major, Blender Z-up."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from typing import TYPE_CHECKING

from ..model.mesh import Mesh
from ..model.prefab import Prefab
from ..model.item import Item, ItemPlacement
from ..model.geometry import IDENTITY, Matrix4, MeshPart
from ..resolve.skins import ResolvedSkin

if TYPE_CHECKING:
    from .environment import Environment
    from ..resolve.club_textures import ClubTextures

Cell = tuple[int, int, int]

log = logging.getLogger("gbxbpy.map_import_v4")


@dataclass(frozen=True, slots=True)
class ImportOptions:
    lod_level: int = 1
    # Also build every other LOD of each mesh as hidden objects parented to
    # the primary one, tagged ``tm_lod_level`` / ``tm_lod_distance``.
    import_all_lods: bool = False
    import_baked: bool = True
    apply_deco_height: bool = True
    # The map stores no population; occupied seats/variants are pseudo-random.
    import_spectators: bool = False
    spectator_occupancy: float = 0.1
    # Vista VegetTreeModels referenced directly by prefabs (JungleForest
    # clumps, mangrove palms); trees without usable geometry become markers.
    import_vista_vegetation: bool = True
    # Tagged NPlugItemPlacement slots. The game leaves them empty by default
    # (flat Land, beaches and LandHill/LandHill2 show no vegetation in-game),
    # so filling them is opt-in; one iLayout per prefab is used.
    fill_vegetation_slots: bool = False
    # GrassFence fins: rows of 0.25 m grass cards over Stadium grass zones.
    # The game draws them only within ~26 m (LOD 1); across a whole map
    # they dominate the polygon count (~2,500 alpha quads per 32 m tile).
    import_grass_fins: bool = False
    # Sun/moon and world colour from the map's decoration mood.
    import_lighting: bool = True
    # Sky dome (SkyColor), sun disc and halos, moon and stars;
    # needs import_lighting. Cloud sprites/shadows are a separate opt-in.
    import_sky: bool = True
    import_clouds: bool = False
    # Decoration surrounds: Stadium's linked map and vista Scene3d oceans.
    import_decoration: bool = True
    # Camera-facing grass billboards (3D decals) on vista terrain layers;
    # density scales the share of the game's decal pattern that is kept.
    import_terrain_grass: bool = False
    terrain_grass_density: float = 0.1
    # Texture mod referenced by the map (chunk 0x03043019): its files replace
    # game textures of the same name.
    apply_texture_mod: bool = True
    # Download skins and mods that are only available through the map's
    # locator URLs (used when the caller passes no SkinLibrary of its own).
    download_locators: bool = True
    club_textures: int = 0

    def __post_init__(self) -> None:
        if type(self.club_textures) is not int or self.club_textures < 0:
            raise ValueError("Club Textures must be a non-negative integer (0 disables downloads)")
        if self.lod_level < 1:
            raise ValueError("LOD level must be at least 1")
        if not 0.0 <= self.spectator_occupancy <= 1.0:
            raise ValueError("Spectator occupancy must be between 0 and 1")
        if not 0.0 <= self.terrain_grass_density <= 1.0:
            raise ValueError("Terrain grass density must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    source: str
    message: str


@dataclass(slots=True)
class GeometryPart:
    name: str
    source_path: str
    payload: Mesh | Prefab | None
    matrix: Matrix4 = IDENTITY
    semantics: MeshPart | None = None


@dataclass(frozen=True, slots=True)
class MaterialContext:
    """Per-placement material redirection shared by one asset definition.

    ``modifier_folders`` are GameData-relative ``RemapFolder`` paths searched
    before a material's own path. ``modifier_materials`` is aligned with
    them: the material stems each folder may redirect (from the modifier's
    GameSkin), or ``None`` when unrestricted. ``modifier_renames`` is also
    aligned: ``(original stem, remap file stem)`` pairs for modifiers whose
    replacement file is named differently (``ItemObstacle`` ->
    ``ItemObstacleLevel2``). ``skin``/``foreground``
    replace the ``MulInside``/``MulInsideForeground`` images of screen
    materials.
    """

    modifier_folders: tuple[str, ...] = ()
    skin: ResolvedSkin | None = None
    foreground: ResolvedSkin | None = None
    modifier_materials: tuple[tuple[str, ...] | None, ...] = ()
    modifier_renames: tuple[tuple[tuple[str, str], ...], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.modifier_folders or self.skin or self.foreground)


@dataclass(slots=True)
class AssetDefinition:
    asset_id: str
    name: str
    source_path: str
    kind: str
    variant_index: int
    mobil_index: int
    footprint: Cell
    mobil_variant_index: int = 0
    parts: list[GeometryPart] = field(default_factory=list)
    material_context: MaterialContext = field(default_factory=MaterialContext)
    item_placement: ItemPlacement | None = None
    item_data: Item | None = None
    # Map-owned resource, not a read-only GameData library definition.
    is_embedded: bool = False


@dataclass(slots=True)
class Instance:
    name: str
    asset_id: str | None
    matrix: Matrix4 = IDENTITY
    metadata: dict[str, str | int | float | bool | tuple[int, int, int]] = field(default_factory=dict)


@dataclass(slots=True)
class Placement(Instance):
    section: str = "Blocks"
    clips: list[Instance] = field(default_factory=list)
    # Kept in the scene but not drawn (e.g. terrain covered by another block).
    hidden: bool = False


@dataclass(slots=True)
class ScenePlan:
    name: str
    source_path: str
    options: ImportOptions
    assets: dict[str, AssetDefinition] = field(default_factory=dict)
    placements: list[Placement] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # Map-wide EMapElemColorPalette name; see GBXBPY.color_tables.
    color_palette: str = "Classic"
    # Decoration mood lighting; None when lighting import is disabled.
    environment: Environment | None = None
    # Map texture mod (GBXBPY.resolve.texture_mod.TextureMod); None when the
    # map has none, it is unavailable, or apply_texture_mod is off.
    texture_mod: object | None = None
    club_textures: ClubTextures | None = None
    # Map header metadata (GBXBPY.model.MapInfo) and identity shown in the
    # addon's Map panel; the decoration fields let it re-resolve the mood.
    map_info: object | None = None
    decoration_height_m: float = 0.0
    thumbnail: bytes = b""
    decoration: str = ""
    decoration_collection: str = ""
    # Vista StadiumOnTerrain material redirection applied to every material
    # the map's blocks and items load: {normcased absolute original path:
    # absolute remap path}. Empty for Stadium decorations.
    terrain_material_remap: dict[str, str] = field(default_factory=dict)
    map_size: Cell = (0, 0, 0)
    day_time: float | None = None

    def issue(self, code: str, source: str, message: str) -> None:
        diagnostic = Diagnostic(code, source, message)
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)
            log.warning("%s [%s]: %s", code, source, message)

    @property
    def partial(self) -> bool:
        return bool(self.diagnostics)

    def validate(self) -> None:
        """Programming errors must fail before any Blender scene mutation."""
        def check(instance: Instance) -> None:
            if instance.asset_id is not None and instance.asset_id not in self.assets:
                raise ValueError(f"Unknown instance target: {instance.asset_id}")
            matrix(instance.matrix)

        def matrix(value: Matrix4) -> None:
            if len(value) != 4 or any(len(row) != 4 for row in value):
                raise ValueError("Expected a 4x4 transform")
            if not all(math.isfinite(v) for row in value for v in row):
                raise ValueError("Non-finite transform in scene plan")

        for key, asset in self.assets.items():
            if key != asset.asset_id:
                raise ValueError(f"Asset identity mismatch: {key}")
            for part in asset.parts:
                matrix(part.matrix)
        for placement in self.placements:
            check(placement)
            for clip in placement.clips:
                check(clip)
