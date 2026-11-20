# Importing maps

[Back to user guide](README.md)

Import a map with *File > Import > Nadeo Exporter CE > Map (.Map.Gbx)*.
The file browser's right-hand panel shows the import options, grouped into
collapsible sections in Custom mode. The defaults give a good-looking result; this page
explains every option so you can tune speed, detail and completeness.

The dialog remembers your options. Each import started from it saves the
current mode and settings, and the next import, also after restarting Blender,
opens with them. They are stored in `map_import_options.json` in Blender's user
config folder under `trackmania_blender_bridge`; delete that file to return to
the defaults. Imports run from scripts don't change the saved options.

## Where game files come from

A map file references hundreds of game assets that are not inside the map
itself. The addon reads them from its **game library**:

- With the [Openplanet plugin](openplanet-plugin.md) installed and Trackmania
  running, **Fetch Missing Files from Trackmania** (on by default) requests
  every missing file from the game during the import. The library fills
  itself over time, so later imports get faster.
- Without the game running, the import uses only what is already in the
  library (or your custom GameData library from the
  [preferences](preferences.md)). Anything missing is reported and becomes a
  placeholder.

## Import options

### Mode and common options

The top row selects **Map** (default) or **Custom**.

- **Map** includes surroundings, mood lighting, sky, clouds, map signs/skins,
  texture mods, authored vista trees/bushes and spectators at the game's default
  30% occupancy. Grass is off. It uses LOD 1 and preserves baked blocks and
  decoration height.
- **Custom** shows individual content options below. Switching modes does not
  overwrite your Custom choices.
- **Fetch Missing Files** is always separate: it permits live requests for
  missing GBX files through Openplanet. Switching modes never changes it.
- Incomplete or unreadable GBX files get one automatic hook-extraction retry
  per import when **Fetch Missing Files** is enabled. Known crashers such as
  `PlugResource.Gbx` are blacklisted. Hook extraction can still crash
  Trackmania; save game-editor work before importing. There is no separate
  experimental item/tree switch.
- **Texture Size** stays available in either mode.
- **Download Signs & Mods** independently permits downloads from map URLs.

The sections below describe Custom controls and their initial defaults.
Geometry and Environment start open; Materials & Textures and Extra Detail
start collapsed. Import All LODs is available under Geometry.

### Common options

| Option | Default | What it does |
|---|---|---|
| **Fetch Missing Files** | On | Permits live requests through Openplanet and one hook retry per failed GBX file, except blacklisted crashers. Requires Trackmania and the plugin. Hook extraction can crash Trackmania. |
| **Texture Size** | `512` | Maximum texture size: `Original`, `2048`, `1024`, `512`, `256` or `128`. Lower values save memory and import time; `Original` keeps full game resolution. |
| **Download Signs & Mods** | On | Downloads custom sign/mod files referenced by URL when they are not found locally. |

### Geometry

| Option | Default | What it does |
|---|---|---|
| **LOD Level** | `1` | Mesh level of detail; `1` is the highest detail. Higher numbers pick simpler meshes and import faster. |
| **Import All LODs** | Off | Also builds every other LOD as hidden objects parented to the visible one, with `tm_lod_level` and `tm_lod_distance` custom properties. |

### Materials & Textures

| Option | Default | What it does |
|---|---|---|
| **Map Signs / Skins** | On | Applies the skins the map assigns to blocks and items (flags, signs, …). |
| **Map Texture Mod** | On | Applies the map's texture mod, if it uses one. |
| **Club Textures** | `0` | Club ID for public screen textures; `0` disables these downloads. Map-specific signs keep priority. |

### Environment

| Option | Default | What it does |
|---|---|---|
| **Surroundings** | On | Imports the scenery around the buildable area — stadium, stands, ocean, hills — matching the map's decoration. |
| **Mood Lighting** | On | Recreates the map's mood (Sunrise, Day, Sunset, Night) as sun and world lighting. |
| **Sky** | On | Adds the sky dome with sun, moon and stars matching the mood. |
| **Clouds** | Off | Adds the cloud layer. Requires **Mood Lighting** and **Sky**. |

After the import you can change mood, time of day, sky and clouds at any time
from the **Mood** panel in the *Nadeo Exporter CE* sidebar tab.

Mood lighting is an approximation of Trackmania's renderer: the importer builds
a sun, ambient world light, and optional sky and clouds, but does not reproduce
the game's film curve, auto-exposure, bloom, color-grading LUT, or full scene
fog. Blender's color-management settings are left unchanged.

### Extra Detail

| Option | Default | What it does |
|---|---|---|
| **Vista Trees & Bushes** | On | Imports the trees and bushes that decorate the surroundings. |
| **Terrain Grass** | Off | Scatters 3D grass decals on grass terrain. |
| **Density** | `10%` | How densely terrain grass is scattered (when enabled). |
| **Stadium Grass Cards** | Off | Grass card strips along stadium grass edges. High polygon count — the dialog warns about it. |
| **Populate Stands** | Off | Fills the stadium stands with spectators. |
| **Occupancy** | `10%` | How full the stands are (when enabled). |

Spectators use the stand prefab's authored spawn positions and rotations.
Map mode fills 30% of these slots, rounded down per placement group. Occupied
seats and stock spectator variants are deterministic, not a reconstruction of
the live game's random crowd; identical stand prefabs share a crowd layout.
Custom mode retains its independent opt-in and occupancy controls.

### Developer Settings

Hidden unless the **Developer UI** preference is enabled; the defaults are
correct for normal use.

| Option | Default | What it does |
|---|---|---|
| **Import Baked Blocks** | On | Includes the map's baked (pre-lit) blocks. |
| **Apply Decoration Height** | On | Keeps freely placed objects at the correct height for the decoration. |
| **Extra Vegetation** | Off | Adds vegetation in slots the game leaves empty — extra greenery that is *not* present in-game. |

Absolute placements use the decoration's collection base plus the map's height
offset. For example, RedIsland's base adds 120 m to anchored items and free
blocks; grid blocks already store their height and are not shifted. The same
resolved height is retained for replay alignment. Missing decoration-size
data is reported rather than silently treating a vista as Stadium.

Vista tree items support vegetation models referenced through either
`EntityModel` variants or `EntityModelEdition`. Baked collection-qualified
clip IDs (such as `StadiumStructurePillarToFlatACB`) resolve to the matching
clip in the vista's nested `Stadium` folder. Standalone `StaticObject` item
references also use the existing mesh decoder, including RoadSign, RoadSignC
and Lamp.

Terrain remains in the source map even when a road connector supplies a
replacement hill. The importer hides a frontier only when one block covers
all its cells and explicitly matches its terrain zone. Replacement follows
the block variant metadata, not the materials in its mesh. This removes
overlapping hills throughout the footprints of `RoadDirtStraightOnDirtHill2`
and `RoadDirtSlope2BaseCurve2RightOnDirtHill2` while preserving partially
covered neighbours and terrain beneath blocks without a matching zone.
Dirt transition stacks such as `Dirt0_Dirt2_Dirt4` are hidden the same way
when a `DecoTerrainHD` block fully covers them.

Ghost blocks can overlap normal blocks, and the map then stores a ghost clip
on the same cell and face as the normal clip (for example stacked
`DecoWallBasePillar` walls). The importer keeps the normal clip and hides
the ghost twin, marked with `tm_ghost_duplicate_of`. Ghost clips without a
normal twin remain visible.

## What to expect after the import

- The 3D viewport frames the imported map automatically.
- The **Nadeo Exporter CE** sidebar tab (press `N`) gains a **Map** panel
  with five collapsible subpanels: **Metadata** (name, author, thumbnail,
  medal times and UID), **Import Statistics** (status, placements, assets,
  block clips and warnings), **Mood** (decoration and lighting controls),
  **MediaTracker** (the five map slots with editor buttons), and
  **Embedded Data** (the map-owned source collection and imported asset list).
  Unavailable MediaTracker slots have disabled editor buttons. These panels
  follow the selected object's map, or the newest map when nothing is selected.
- Selecting an imported object shows its details in the **Selected Asset**
  panel — source file, block/item metadata and placement information.
- Blocks are instanced: many placements share one reusable collection, which
  keeps the scene light. Use **Make Editable** in the *Selected Asset* panel
  to turn a selected block into editable objects. The block's displayed clips
  (connection pieces such as borders and road ends) come along, also when you
  select just one clip, and everything is gathered in one
  `<block> (Editable)` collection. Scenes imported before 0.0.16 find the
  clips from their geometry; re-import the map for exact results.
- Importing a second map reuses already-built assets, so it is much faster.

## Partial imports and placeholders

An import can finish successfully and still skip content:

- Assets that are missing from the library (and could not be fetched) or not
  yet supported are imported as clearly marked **placeholder** objects, so
  you can see where something belongs.
- The import report and the **Map** panel's *Warnings* count tell you how
  much was affected. Selecting a placeholder shows
  *"Geometry unavailable; see import warnings"*.

If placeholders appear, set up the [Openplanet plugin](openplanet-plugin.md),
start Trackmania, and re-import — missing files are then fetched on demand.

## Performance tips

- V4 reuses parsed assets in memory during an import; persistent parse caching
  is currently disabled.
- Lower **Texture Resolution** and raise **LOD Level** for quick previews.
- Leave **Terrain Grass**, **Stadium Grass Fins** and **Populate Spectators**
  off unless you need them; they add many objects and polygons.
