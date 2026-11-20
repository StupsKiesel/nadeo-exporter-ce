# Preferences & paths

[Back to user guide](README.md)

Open *Edit > Preferences > Add-ons*, find **Nadeo Exporter Community
Edition** and expand it. The game dropdown lists detected installations and
the currently selected one. Its menu can search again or browse manually.
Game and Openplanet show an alert when unset or missing.
Preferences follow setup order: Python Dependencies, Directories and Openplanet,
then Updates, Logging and the developer options.

Search checks Trackmania 2020, ManiaPlanet / Trackmania 2, Turbo and Forever
Steam installations and common launcher locations. Detection does not mean
the bundled extractor or older GBX formats are supported. The extractor targets
Trackmania 2020 and ManiaPlanet; ManiaPlanet's full extraction behavior is unverified.

## Directories

| Field | Purpose |
|---|---|
| **Game** | Select an installation; the adjacent menu searches again or browses for a folder. |
| **Openplanet** | Select the matching user folder containing `Plugins` and `Extract`, not the program folder. |
| **User Files** | Resolved from the selected game's `Nadeo.ini`. If unavailable, choose a folder manually. |
| **Custom GameData Libraries** | Optional per-game library paths. When off, the library is `<Openplanet>\Extract\GameData`. ManiaPlanet, Turbo and Forever custom libraries are disabled pending support; saved paths for these games are not used. |

Use the info button beside **Game** to view the resolved user files, resource
cache and read-only game-cache locations.

Game Packs are read from the selected install and, when available, the game's
common-data `PacksCache`. Matching locator downloads may be copied from the
game's cache before downloading. The game cache is read-only and never cleared
by the addon; extracted resources remain in the separate addon cache.

Previous GameData, Packs and custom resource-cache path preferences are retained
but no longer active.
To keep using an old GameData folder, enable **Custom GameData Libraries** and
select it for the game. Existing files are not moved or deleted.

## Updates

Updates, logging level/capture, and dependency status/actions use compact
horizontal rows. Openplanet groups connection status, plugin actions, and
port/library actions into compact rows. Directory fields remain full-width for
readable paths.

**Check for Updates** checks the newest published GitHub release, including
pre-releases, and reports whether it is newer than the installed addon; it does
not download or install updates. When an installable update is found, the
button changes to **Install** with its version; confirm the dialog and restart
Blender afterwards. Otherwise it shows the result or a retry action. The
pre-release checkbox is before it in the same row and is on by default; drafts
are ignored. The link opens release notes when an update is available, or
[nadeo-exporter-ce](https://github.com/StupsKiesel/nadeo-exporter-ce) in a browser.
Update checks require a public repository; the addon does not authenticate to
private repositories.
**Upgrade Folder** is in the Developer > Upgrade sidebar panel for local build ZIPs.

## Python Dependencies

Shows whether the required Python packages (`construct`, `gbx-py`, `Pillow`,
`numpy`) are available, and the **Install Dependencies** button. See
[installation](installation.md#2-install-python-dependencies).

## Openplanet

The **Port** slider selects the local bridge server port; set the same port in
the Blender Bridge Extractor plugin settings. The box also shows the bridge
status and extractor plugin:

- Connection status: *Connected to Trackmania*, *Waiting for Trackmania*, or
  *Idle*.
- Installed plugin version, with an **Install Plugin** / **Update Plugin**
  button for the selected game. If the installed version or game variant differs
  from the bundled one, update it. Turbo and Forever plugins are unavailable.
- **Open Library Folder** opens the game library on disk.

See [Openplanet plugin setup](openplanet-plugin.md) for the full workflow.

## Developer UI

A checkbox at the bottom of the preferences. Off by default and not needed
for normal use. Enabling it adds a **Developer** section to the sidebar with
debugging, manual extraction and maintenance tools, and reveals the
*Developer Settings* section in the map import dialog.

**Developer > Quick Actions > Clear Blend** removes image datablocks as well
as scene data, including images with fake users. Save images to disk or save a
separate blend file first; image files on disk are not deleted.

## Tools panel

The **Nadeo Exporter CE > Tools** panel is available without Developer UI.
Its **Game Library** section shows the active GameData folder and Openplanet
index state, and provides **Check / Repair**, **Fill Missing**, **Refresh
Index** and **Open Folder** actions. The library path setting and Openplanet/plugin status are
available in Addon Preferences, avoiding duplicate controls in the Tools
panel. The selected game's default or custom library is used by imports.
**Check / Repair**, **Fill Missing** and **Refresh Index**
start the local bridge server if needed and wait up to 30 seconds for the
Openplanet plugin. **Fill Missing** extracts files listed in the game index
that the library doesn't have yet. It shows the count and size first and skips
audio, video and shaders unless enabled. Openplanet's `Extract\GameData`
remains the staging location. With a custom library, extracted files from
imports, repairs, fills and manual requests are moved from there into the
library. Files that fail validation or come out shorter than the game reports
stay in staging. Failed files are retried
once normally, then in hook mode for GBX files, with skipped header data
restored from the normal extraction when that is proven correct. Files the game
reports as not available are skipped by later fills of the same game version.
Running library tasks show their progress with a **Cancel** button.
The **Batch Processor** section is disabled until GBX writing is supported.
**Viewport Options** and the GBX Inspector shortcut remain available there.

## Logging

Always available in addon preferences, independently of Developer UI.
Choose the console/import **Logging Level** and enable **Store Import Logs in
Blend** to create one internal Text datablock per import. Read logs in Blender's
Text Editor and save the blend file to retain them. No external log files or
log folders are created. Disabling capture preserves existing embedded logs.

## Where things are stored

| Data | Location |
|---|---|
| Game library (extracted game files) | Openplanet `Extract\GameData` or the selected custom library. Fills on demand; grows over time. |
| Resource cache | The standard addon cache. Safe to delete; rebuilt as needed. |
| Parse cache | Persistent parse caching is disabled; old cache files are not used. |
| Import logs (opt-in) | Internal Text datablocks, retained when the blend file is saved. |
| Addon preferences | Saved by Blender with your user settings; they survive addon updates. |
