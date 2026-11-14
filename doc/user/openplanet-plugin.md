# Openplanet plugin setup

[Back to user guide](README.md)

Map and asset imports need game files that live inside Trackmania's packed
data. The bundled **Blender Bridge Extractor** Openplanet plugin lets the
addon request exactly the files an import needs from the *running game* —
no manual extraction, no pre-prepared GameData folder.

With the plugin set up, keep the game running while you import; everything
else is automatic.

## How it works

1. During a map import with **Fetch Missing Files from Trackmania** enabled
   (the default), the addon starts a small local server on your own machine
   (`127.0.0.1`, port `1337`).
2. The plugin inside Trackmania connects to it, extracts each requested file
   from the game, and stores it in the addon's game library
   (`<Openplanet folder>\Extract\GameData`).
3. The import continues with the fetched files. The library keeps everything
   it fetched, so later imports need fewer and fewer requests.

Nothing is sent anywhere except between Blender and Trackmania on your own
computer.

## Setup

### 1. Install Openplanet

Install [Openplanet](https://openplanet.dev/) for Trackmania if you haven't
already.

### 2. Install the plugin from Blender

1. Open the addon preferences (*Edit > Preferences > Add-ons >
   Nadeo Exporter Community Edition*).
2. Under **Openplanet**, click **Install Plugin**. This copies the bundled
   plugin into your `OpenplanetNext\Plugins` folder.
3. If Trackmania is already running, reload plugins in Openplanet
   (*Developer > Reload plugin*) or restart the game.

When a new addon version bundles a newer plugin, the preferences show
*"Installed vX, bundled vY"* — click **Update Plugin** and reload again.

### 3. Allow unsigned plugins

The plugin is not signed by Openplanet, so Openplanet must run in **developer
signature mode**: in the game, open the Openplanet overlay (`F3`) and set
*Developer > Signature mode* to **Developer**.

### 4. Verify the connection

Start a map import (or just open the import dialog): the *External Files*
section and the preferences' *Openplanet* section show
**Connected to Trackmania** once the plugin has connected. In the game,
Openplanet shows a *Connected to Blender* notification.

## During imports

- The plugin shows one notification per batch: *Extracted N files for
  Blender*, or a red summary if some files failed (details in
  `Openplanet.log`).
- Files that cannot be provided are reported by the import and become
  [placeholders](importing-maps.md#partial-imports-and-placeholders); you can
  re-import later to fill them in.
- The import does not require you to leave the game on any particular screen;
  it only needs Trackmania (with Openplanet) running.

## Plugin settings

The plugin has no window of its own. Its few settings are in Openplanet's
*Settings* dialog under *Blender Bridge Extractor*:

- **Server URL** — default `http://127.0.0.1:1337`. Change it only if you
  also change the **Port** in the addon's developer preferences; both sides
  must match.
- **Notifications** — turn the toasts off, or show problems only.

## Troubleshooting

| Symptom | Fix |
|---|---|
| *Waiting for Trackmania* never becomes *Connected* | Is the game running with Openplanet? Is signature mode set to *Developer*? Was the plugin reloaded after installing? |
| Plugin not listed in Openplanet | Click **Install Plugin** again and check `OpenplanetNext\Plugins\BlenderBridgeExtractor` exists; then reload plugins. |
| Imports report missing or incomplete files although connected | Check the plugin summary and `Openplanet.log`. Re-importing retries normal extraction; for incomplete or unreadable `VegetTreeModel` or `Item.Gbx` files, enable **Experimental Hooked Extraction** in the map import options. Hook extraction is experimental and may crash Trackmania. |
| You cannot run the game and Blender together | Use a pre-extracted GameData folder as **Game Library** instead — see [preferences](preferences.md). |
