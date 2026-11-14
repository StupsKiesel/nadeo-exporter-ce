# Installation & first-run setup

[Back to user guide](README.md)

## Requirements

- **Blender 3.6.2 or newer**
- **Windows** (game and Openplanet folders are discovered automatically on
  Windows; on other systems you must set paths manually)
- Internet access once, for the Python dependency install
- Optional but recommended: **Trackmania (2020)** with
  **[Openplanet](https://openplanet.dev/)** installed, so the addon can fetch
  game files on demand

## 1. Install the addon

You need the addon ZIP, named like
`nadeo_exporter_ce_0.0.9.zip` (release) or
`nadeo_exporter_ce_0.0.9_<timestamp>.zip` (development build).

1. In Blender, open *Edit > Preferences > Add-ons*.
2. Click *Install from Disk…* (Blender 4.2+: the dropdown in the top-right
   corner; earlier versions: the *Install…* button) and select the ZIP.
3. Enable the checkbox next to **Nadeo Exporter Community Edition**.

> The addon installs into a folder named `trackmania_blender_bridge` — that
> is its internal identifier and is expected. Installing a new ZIP over an
> old version keeps your preferences.

## 2. Install Python dependencies

The addon needs a few Python packages that Blender does not ship
(`construct`, `gbx-py`, `Pillow`, `numpy`).

1. Expand the addon's preferences (the arrow next to its name in the
   Add-ons list).
2. Under **Python Dependencies**, the addon lists anything missing.
3. Click **Install Dependencies**. This needs internet access and freezes
   Blender while the packages download — save your work first.
4. When it reports *"Installed. Restart Blender before importing."*, restart
   Blender.

The packages are installed into your Blender user scripts folder, so they
survive addon updates. A new Blender major version may require running the
installer once again.

## 3. First-run preferences

Most people can leave every path empty — the addon auto-detects Ubisoft,
Steam and Epic Trackmania installs and the default Openplanet folder. Check
the [preferences reference](preferences.md) if you need to point the addon at
non-standard locations.

One decision matters for map imports — where game files come from:

- **Recommended: the Openplanet bridge.** Click **Install Plugin** under
  *Openplanet* in the addon preferences and follow the
  [Openplanet plugin setup](openplanet-plugin.md). Imports then fetch any
  missing game file from the running game automatically; you don't need to
  prepare anything.
- **Alternative: an existing GameData folder.** If you already have an
  extracted `GameData` folder, set it as **Game Library** in the preferences.
  Imports then read from it directly and never need the game running.

## 4. First import

1. *File > Import > Nadeo Exporter CE > Map (.Map.Gbx)*.
2. Navigate to a map, for example in `Documents\Trackmania\Maps`.
3. Keep the default options and click the import button.
4. For a first try, prefer a small map — large maps with many custom items
   take noticeably longer.

If you use the Openplanet bridge, keep Trackmania running during the import;
the *External Files* section of the import dialog shows whether the game is
connected.

After the import finishes, the viewport frames the map, and the
**Nadeo Exporter CE** sidebar tab (press `N` in the 3D viewport) shows map
information, mood controls, and details for any imported object you select.

## Updating the addon

Install the newer ZIP the same way as in step 1 — over the existing
installation — then restart Blender. Preferences and installed dependencies
are kept.

## Uninstalling

Disable and remove **Nadeo Exporter Community Edition** from *Edit >
Preferences > Add-ons*. Caches created outside Blender (the skin cache under
`%LOCALAPPDATA%\TrackmaniaBlenderBridge` and any parse cache folder you
configured) can be deleted manually.
