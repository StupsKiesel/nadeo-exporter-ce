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
**Connected to Trackmania** once the plugin has connected. In the game, the
Blender logo in the Openplanet menu bar turns green.

## During imports

- The Blender logo turns blue while a request runs, with a thin progress line
  under it. After the batch, its panel shows *Extracted N files*. An orange
  warning icon briefly marks failed files; the panel shows the first one, and
  `Openplanet.log` lists them all.
- Files that cannot be provided are reported by the import and become
  [placeholders](importing-maps.md#partial-imports-and-placeholders); you can
  re-import later to fill them in.
- The import does not require you to leave the game on any particular screen;
  it only needs Trackmania (with Openplanet) running.

## Plugin settings

The plugin adds a Blender logo to the right end of the Openplanet overlay's
menu bar (`F3`). Its colour shows the status:

| Logo | Meaning |
|---|---|
| Green | Connected to Blender |
| Blue | A request is running |
| Light grey | Waiting for Blender |
| Dark grey | Listening is switched off |

An orange warning icon appears next to it for 30 seconds when files failed
in a request; the panel keeps showing the first failure afterwards.
While a request runs, a thin blue line under the logo shows its progress.
Hover over the logo for the status. Click it to open or close a small panel
fixed below the top-right corner:

- **Listen for Blender requests**: the on/off switch for polling Blender.
  While it is off, the plugin stays loaded but ignores Blender completely.
- The connection status, and the running request with a progress bar. When
  nothing is running, it shows the summary and first failure of the last
  request instead.
- **Port**: the port of the Blender bridge (default `1337`). It must match
  the **Port** in the addon's developer preferences.
- **Log every extracted file**: turn it off to keep `Openplanet.log` short.
  Failures are always logged. Openplanet itself still logs one
  "Extracted file" line per file.
- **Show in the menu bar**: turn it off to hide the logo. The panel stays
  available under *Plugins > Blender Bridge Extractor*.
- **Extract folder** and **Copy log path** buttons.

The same settings, including the full **Blender bridge URL**, are also in
Openplanet's *Settings* dialog under *Blender Bridge Extractor*.

Cancelling a library task in Blender also stops the request in the game
within a moment. Since plugin 2.2, Blender warns when Trackmania still runs
an older plugin than the addon bundles: click **Update Plugin** and reload
it in Openplanet.

## Troubleshooting

| Symptom | Fix |
|---|---|
| *Waiting for Trackmania* never becomes *Connected* | Is the game running with Openplanet? Is signature mode set to *Developer*? Was the plugin reloaded after installing? |
| Plugin not listed in Openplanet | Click **Install Plugin** again and check `OpenplanetNext\Plugins\BlenderBridgeExtractor` exists; then reload plugins. |
| Imports report missing or incomplete files although connected | Check the plugin summary and `Openplanet.log`. Re-importing retries normal extraction; for incomplete or unreadable `VegetTreeModel` or `Item.Gbx` files, enable **Experimental Hooked Extraction** in the map import options. Hook extraction is experimental and may crash Trackmania. |
| You cannot run the game and Blender together | Use a pre-extracted GameData folder as **Game Library** instead — see [preferences](preferences.md). |
