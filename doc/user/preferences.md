# Preferences & paths

[Back to user guide](README.md)

Open *Edit > Preferences > Add-ons*, find **Nadeo Exporter Community
Edition** and expand it. All paths can stay empty — empty means the addon
discovers the standard location automatically. Set a path only when your
setup differs. Invalid paths are flagged with a *"folder not found"* warning
instead of being silently ignored.

## Directories

| Field | Empty means | Set it when |
|---|---|---|
| **Trackmania** | Auto-detect the Ubisoft, Steam or Epic installation. | Your game is installed somewhere unusual. Its `Packs` subfolder supplies the stock skins. |
| **Openplanet** | `%USERPROFILE%\OpenplanetNext` | Your Openplanet user folder (the one containing `Plugins` and `Extract`) is elsewhere. This is *not* the Openplanet program folder. |
| **User Files** | `Documents\Trackmania` | Your Trackmania user folder (the one containing your custom `Skins`) is elsewhere, e.g. not synced to Documents. |
| **GameData** | `Extract\GameData` under the Openplanet folder. | You want imports to read from an existing extracted GameData copy instead of the Openplanet extract library. |

### Advanced Paths

Click **Advanced Paths** to show three optional overrides:

| Field | Empty means | Notes |
|---|---|---|
| **Game Packs Directory** | Auto-detect the game's `Packs` folder. | Takes precedence over the *Trackmania* path for stock skins. |
| **Parse Cache Directory** | Parse cache disabled. | Point this at any folder to cache parsed assets on disk — repeated map imports become roughly 100× faster. The cache invalidates itself when source files change; safe to delete anytime. |
| **Skin Cache Directory** | `%LOCALAPPDATA%\TrackmaniaBlenderBridge\SkinCache` | Where skin images extracted from the game's pack archives are cached. |

## Updates

Updates, logging level/capture, and dependency status/actions use compact
horizontal rows. Openplanet groups connection status, plugin actions, and
port/library actions into compact rows. Directory fields remain full-width for
readable paths.

**Check for Updates** checks the latest GitHub release and reports whether it
is newer than the installed addon; it does not download or install updates.
**Open GitHub Repository** opens the project page in a browser. Both controls
are disabled until a valid repository URL is configured in `_tools_panel.py`.
**Upgrade Folder** remains developer-only for local build ZIPs.

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
  button. If the installed version differs from the bundled one, update it.
- **Open Library Folder** opens the game library on disk.

See [Openplanet plugin setup](openplanet-plugin.md) for the full workflow.

## Developer UI

A checkbox at the bottom of the preferences. Off by default and not needed
for normal use. Enabling it adds a **Developer** section to the sidebar with
debugging, manual extraction and maintenance tools, and reveals the
*Developer Settings* section in the map import dialog.

## Tools panel

The **Nadeo Exporter CE > Tools** panel is available without Developer UI.
Its **Game Library** section shows the active GameData folder and Openplanet
index state, and provides **Check / Repair**, **Refresh Index** and **Open
Folder** actions. The library path setting and Openplanet/plugin status are
available in Addon Preferences, avoiding duplicate controls in the Tools
panel. Set **Custom Location** there to use a GameData library outside the
default Openplanet extraction directory. The configured folder is the library
used by imports. **Check / Repair** and **Refresh Index** start the local
bridge server if needed and wait up to 30 seconds for the Openplanet plugin.
Openplanet's `Extract\GameData` remains the staging location for repair
downloads; Check / Repair copies validated replacements into the configured
library but does not move or clean up that staging folder.
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
| Game library (extracted game files) | `<Openplanet folder>\Extract\GameData`, unless **GameData** overrides it. Fills on demand; grows over time. |
| Skin cache | `%LOCALAPPDATA%\TrackmaniaBlenderBridge\SkinCache`, unless overridden. Safe to delete; rebuilt as needed. |
| Parse cache | Only where you point **Parse Cache Directory**. Safe to delete; rebuilt as needed. |
| Import logs (opt-in) | Internal Text datablocks, retained when the blend file is saved. |
| Addon preferences | Saved by Blender with your user settings; they survive addon updates. |
