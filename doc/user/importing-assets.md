# Importing items, meshes, prefabs & materials

[Back to user guide](README.md)

Besides whole maps, the addon imports individual GBX files. All entries live
under *File > Import > Nadeo Exporter CE*:

| Menu entry | File type | Use it for |
|---|---|---|
| **Item / Crystal (.Gbx)** | `.Item.Gbx`, `.Crystal.Gbx` | Custom or stock items, e.g. from [item.exchange](https://item.exchange/), and standalone Mesh Modeller saves. |
| **Prefab (.Prefab.Gbx)** | `.Prefab.Gbx` | Game prefab hierarchies (groups of meshes/entities). |
| **Mesh (.Mesh.Gbx)** | `.Mesh.Gbx` | Individual game meshes. |
| **Vegetation Tree (.VegetTreeModel.Gbx)** | `.Gbx` | Game vegetation tree/bush models. |
| **Block/Clip (.ED\*.Gbx)** | `ED*.Gbx` | Editor block definitions with their variants. |
| **Material (.Material.Gbx)** | `.Material.Gbx` | A single game material. |
| **MediaTracker Clip (.Clip.Gbx)** | `.Clip.Gbx` | A standalone MediaTracker clip with an approximate camera preview. Right-click its Empty in the Outliner and choose **Edit MediaTracker** to open the GPU editor. |

Like map imports, these read referenced game files (materials, textures,
nested meshes) from the game library — see
[where game files come from](importing-maps.md#where-game-files-come-from).

Item import progress appears in the 3D Viewport tool header. Parsing and some
individual build steps run synchronously, so Blender can pause between updates
on large files.

## Imported lights

Embedded and library-referenced lights use decoded point/spot types, color,
intensity, range, emitting radius, shadow-generation flags and spot cones
where available. Compiled custom point/spot lights are also imported when
there are no source-light entries: each user-model instance uses its skeleton
socket and linked joint transform. These lights are independent of embedded
or library-referenced lights, not overrides of them. Blender energy and
falloff remain approximations, not game-calibrated rendering.

Select a light and open **Light Data Properties > Custom Properties** to see
`tm_light_*` metadata: raw flags, animation periods/timer, and flare, projector,
function and color-target references. `tm_night_only` is retained too.
These do not enable animation, flares, projections or mood-dependent switching.
Cones outside Blender's supported range are clamped with a warning; the
original angles remain in the metadata. Missing or undecodable library
lights retain generic settings and report the problem in the log and
`tm_light_data_warning`.

When a library `.Light.Gbx` fails to parse and on-demand fetching is enabled
with Trackmania and the Openplanet plugin connected, the addon retries that
file once per import using hook extraction. The replacement must parse as a
complete light before it is accepted; a failed retry restores the previous
file and logs the problem. Other unreadable GBX files also get one hook
retry, except blacklisted crashers such as `PlugResource.Gbx`. Hook
extraction can still crash Trackmania. This does not change light animation
settings.

## Common option: LOD Level

Most asset importers share one option:

- **LOD Level** (default `1`) — mesh level of detail. `1` is the highest
  detail; higher numbers select simpler meshes where the asset provides them.

## Item / Crystal (.Gbx)

Imports an item-definition collection with separate geometry, collision and
trigger objects, materials, and pivot/placement metadata. Standalone items do
not get a map-instance Empty; map imports still use Empties to instance these
definition collections.

Standalone `.Crystal.Gbx` saves use the same importer and keep separate
geometry layers, material assignments, texture UVs, visibility and collision
flags. Editable geometry uses the Mesh Modeller's element pools rather than
the embedded mesh layout used by finished items; versions 32–37 are supported.
This adds import support, not GBX export or a lossless editor round-trip.

Invisible collision geometry uses wire display and is excluded from renders.
Triggers have no materials or UV maps. Object labels and supported material/
waypoint settings follow Blendermania conventions; this does not certify a
lossless export through NadeoImporter.

- **LOD Level** — `1` = highest detail for the embedded mesh.

After the import, select the item to see its metadata in the **Selected
Asset** sidebar panel; items with placement data additionally get a
**Placement** panel (grid steps, pivots, ghost mode and similar values from
the item's definition).

## Batch Custom Items (Experimental)

The Batch Custom Items entry has been removed from the File > Import menu,
and the **Batch Processor** section in the Tools panel is disabled until GBX
writing is supported. The workflow below is retained as implementation
reference.

When available, it imports several supported editor-crystal items in one go, baking their
deformation; the original files remain untouched. This is experimental and
limited to items built from editor crystals. Open
**Nadeo Exporter CE > Tools > Batch Processor**. There is one configuration per scene. Choose
**Selected Items** (selection icon) or **Files** (folder icon) to the right of
**Run Batch**; hover for tooltips. Selected Items uses the current selection at run time; Files
opens a multi-file import picker when you press **Run Batch**.
Use **+**, **-** and arrows to add/remove/reorder actions; every action runs.
The six independent cleanup/UV actions are joined by
**Validate Item**, **Rename** (root/output filename only) and **Export Item Copy**.
The gear button in the right-hand action controls swaps the list for the selected
action's settings, including the output folder for Export Item Copy. Click it
again to return to the list; settings and list are never shown together.
Execution is a single Run Batch button. Results appear
only after processing, with a summary and the selected item's details.
Export is optional and must be the final action; without it, Run Batch
edits only Blender data. Originals/existing outputs are never overwritten.
Failures roll back that item's edits while other items continue. Progress and
Cancel work between items; details are stored in **Item Batch Results**.
Normal Item imports, including runtime meshes, can use cleanup, normals,
lightmap UV regeneration and Rename in both input modes. Original GBX smoothing,
GBX validation and export still require the restricted editor-crystal batch
import; unsupported actions report an error and roll back the item.
See the [support restrictions](../import-pipelines.md#experimental-batch-custom-item-round-trips)
before relying on exported copies.

## Prefab (.Prefab.Gbx)

Imports a prefab hierarchy: meshes, nested prefabs and lights, arranged as in
the game.

- **LOD Level** — `1` = highest detail for embedded meshes.

## Mesh (.Mesh.Gbx)

Imports a single mesh with its materials.

- **LOD Level** — `1` = highest detail.

## Vegetation Tree (.VegetTreeModel.Gbx)

Imports a vegetation tree or bush model.

- **LOD Level** — `1` = highest detail.

## Block/Clip (.ED\*.Gbx)

Imports an editor block definition — the reusable block you place in the map
editor — including its ground/air variants.

| Option | Default | What it does |
|---|---|---|
| **LOD Level** | `1` | Mesh detail, `1` = highest. |
| **Import Air Variants** | On | Also imports the air variants, not only ground. |
| **Variant Kind** | `ALL` | Restrict to a specific variant kind. |
| **Variant Index** | `-1` | Import only one variant by index; `-1` imports all. |

## Material (.Material.Gbx)

Builds the game material as a Blender material with its textures.

- **Assign to Active Object** (default off) — additionally appends the
  imported material to the active mesh's material slots.

World-projected plastic platforms use their separate top/side hue masks and
referenced colour table (for example PlatformPlastic's **Fun** table). The
placement colour selects the map palette target on each projection; **Default**
keeps the original texture colour.

### Experimental trimsheet candidates

In the **Image/UV Editor** sidebar, open **Trimsheet (Experimental)**.
Select a Blender material, then **Generate Candidates**. Its diffuse image
is used and displayed in the Image/UV Editor; if several images feed Base
Color, label the intended image node `Diffuse`. Generating again asks before
replacing existing regions.

The image reference and editable regions persist in the scene. Save or pack
generated or modified images to preserve their pixels; the tool does not pack
them automatically. The image overlay highlights the selected rectangle in
orange; unconfirmed regions are blue, confirmed ones green. Use **+ / -** to
add/remove rectangles and edit the name, UV bounds, repeat direction and
confirmation below the list.

These are **inferred candidates, not authored GBX trim definitions**. The
prototype bounds non-black texture silhouettes or strong colour bands;
rectangles can include unused pixels and need manual correction. Inference
samples at most 256 pixels per axis; fine-tune bounds against the full-resolution
image (the image itself is not downscaled by the prototype). Selected
meshes with matching image textures and `BaseMaterial` UVs contribute island
bounds. Out-of-tile/degenerate islands are reported and skipped, not wrapped.
Names are placeholders and repeat directions start as **Unknown**.
TechnicsSpecials is a packed parts atlas rather than a regular repeating
trimsheet; confirming a rectangle does not make it authored game metadata.

**Export Trimsheet JSON** writes schema version 1, the Blender material and
image information, diagnostics and editable regions, preserving evidence and
confirmation. GBX-specific metadata is unavailable and exported as `null`.
UV bounds are `[u_min, v_min, u_max, v_max]`, with bottom-left origin.
Export rejects inverted/empty rectangles. Details also appear in the
**Trimsheet Diagnostics** text datablock. This prototype does not assign UVs,
modify existing materials, or write back to GBX files.

With ZenUV enabled, **Load into ZenUV** copies the current region names and
rectangles onto the preview image's ZenUV trimsheet and switches ZenUV to
**Image** mode. Existing ZenUV trims on that image require replacement
confirmation; other images and scene trims are untouched. Names must be unique.
Repeat directions, evidence and confirmation remain in this panel/JSON; they
are not translated into ZenUV settings. The button does not regenerate regions.

Imported materials are reused: importing the same material twice, or
importing assets that share materials, links to the existing Blender
material instead of duplicating it. Selecting an object using an imported
material shows its source in the **Material** sidebar panel.

## Tips

- Assets are built as reusable collections; importing the same file again
  reuses what already exists in the blend file.
- If textures come in too large or too small, re-import after adjusting the
  texture options — for standalone assets the texture size follows the
  material defaults; for full maps use the map import's
  [Materials & Textures options](importing-maps.md#materials--textures).
- Unsupported parts are reported in the import messages rather than failing
  the whole import; check the system console for details.
