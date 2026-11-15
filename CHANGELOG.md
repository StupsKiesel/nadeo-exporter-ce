# Changelog

All notable user-facing changes to Nadeo Exporter Community Edition.

## [0.0.15] - 2026-11-15

### Fixed

- **Fill Missing on Linux no longer requests bogus files.** Under Wine the game
  also lists files Openplanet extracted below their absolute path
  (`GameData/home/<user>/.wine/.../Extract/GameData/...`). These entries
  are now left out of the game index, also from an index saved earlier, so
  Fill Missing and Check / Repair no longer report them as failed.

## [0.0.14] - 2026-11-15

### Changed

- **Cleaner Openplanet menu bar.** Instead of a separate warning icon, the
  Blender logo turns red for 30 seconds when files failed.

### Fixed

- **The extractor plugin no longer stops on a slow file.** When one file took
  longer than Openplanet's script timeout (1 second by default, often hit
  under Proton/Wine or on slow disks), Openplanet aborted the plugin with
  "Script execution timeout exceeded" until it was reloaded. The plugin
  (2.2.1) now checks that file and continues with the next one. Click
  **Update Plugin** in Blender to install it.

## [0.0.13] - 2026-11-15

### Added

- **Fill Missing** in Tools > Game Library. It extracts every file from the
  game index that your library doesn't have yet. A dialog shows the count and
  size first. Audio, video and shader sources (`.hlsl`, `.PHlsl.Txt`,
  `.VHlsl.Txt`) are skipped unless enabled. Library
  tasks now show their progress with a **Cancel** button.
- **Fill Missing and Check / Repair retry failed files.** A file that fails is
  requested again normally, then with hook extraction for GBX files. Header
  data the hook output lacks is restored from the normal extraction, so files
  such as `BlueBay.Collection.Gbx` can now be recovered. Files the game reports
  as not available are remembered and skipped until the game updates.
- **Map import remembers its options.** The mode and settings of the last
  import from the dialog are saved and used for the next import, also after
  restarting Blender.
- **Openplanet menu bar switch.** The extractor plugin (2.2.0) adds a Blender
  logo to the right end of the Openplanet overlay's menu bar. Its colour shows
  the connection and running requests, a thin line under it shows progress,
  and a warning icon marks failures. Clicking it opens a fixed panel in the
  top-right corner with a clear **Listen for Blender requests** switch, a
  progress bar, and quick settings for the port and per-file logging.
- Blender warns when Trackmania still runs an older extractor plugin than the
  one bundled with the addon, with an **Update Plugin** button.

### Changed

- **Faster extraction.** The plugin extracts several files per frame instead
  of one, about 1.4 times faster. Failures now include a short reason such as
  `truncated` or `nodata`. Files the game lists without data are marked in the
  game index and skipped.
- Cancelling a library task in Blender now also stops the request in
  Trackmania instead of letting it run to the end.
- The extractor plugin no longer shows notification pop-ups in the game;
  status, progress and failures are shown by the menu bar logo and its panel.

### Fixed

- **Custom Game Library folders now get the files they need.** Files extracted
  for imports, Check / Repair and manual requests are moved from Openplanet's
  `Extract\GameData` folder into the custom library. Before, imports couldn't
  see newly fetched files and copies were left behind on the system drive.
  Truncated extractions stay in the Extract folder instead of replacing or
  adding a broken library file.
- The game index no longer lists empty `Name.Gbx?123` alias entries. Fill
  Missing requested them and left `Name.Gbx_123` copies in the Extract folder;
  those copies are removed.
- Sidebar panels now start open and their subpanels start closed. Before, Map
  and Developer started closed while some Selected Asset and Map subpanels
  started open.

## [0.0.12] - 2026-11-15

### Added

- **Install updates from Blender.** When a newer version is found, the
  Check for Updates button turns into an Install button labelled as a release
  or a pre-release. It downloads the addon ZIP from GitHub after a
  confirmation and installs it in place, keeping your settings. Updates are
  checked once automatically at startup. Pre-releases are offered too unless
  you turn off the new **Include Pre-releases** preference.

## [0.0.11] - 2026-11-15

### Fixed

- **Stadium decoration misplaced on maps larger than their decoration.**
  Maps whose size differs from their stadium decoration (for example a
  64×64 or 128×128 map using the 48×48 stadium) had the stands, screens and
  surroundings shifted by hundreds of metres. The stadium is now placed on
  the decoration's own 48×48 field at the map origin, as in game.
- **Custom items with a Scale layer imported at the wrong size.** Scale
  layers set in the item editor's mesh modeller were ignored, so such items
  appeared too big, too small or stretched (for example a rock imported at
  twice its size, covering a whole village). The scale is now applied around
  the item origin, or around each part's centre when "scale each part
  independently" is set.
- **Wrong file requests on Linux (Wine/Proton).** Absolute file paths were
  re-rooted below the library, so Trackmania was asked for files like
  `GameData/home/user/.wine/.../GameData/Stadium/...` that cannot exist.
  Absolute paths are now looked up as they are, falling back to their
  GameData-relative part.

## [0.0.10] - 2026-11-15

### Fixed

- **Map imports no longer stall around 92%.** Maps containing stadium
  warp-flag prefabs could freeze for 10 minutes or more while building flag
  wind animations, because every keyframe was written individually and those
  writes slow down as the scene fills with objects. Wind poses and their
  looping animation are now written in bulk; the affected stadium prefab in a
  large test map dropped from about 11 minutes to about 11 seconds. Flag
  animation timing and shape-key names are unchanged.
- **Linux/macOS: second import failed with "port already in use".** The
  Openplanet bridge server can now restart on the same port right after a
  previous import closed it. Windows behaviour is unchanged.
- **Check for Updates returned "HTTP 404".** The update check now also finds
  preview (pre-)releases instead of only stable releases.

### Documentation

- Added direct download links for the addon ZIP to the README.
- Added a troubleshooting note for bridge port errors.

## [0.0.9] - 2026-11-15

- First public preview release.

[0.0.15]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.15
[0.0.14]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.14
[0.0.13]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.13
[0.0.12]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.12
[0.0.11]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.11
[0.0.10]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.10
[0.0.9]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.9
