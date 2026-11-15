# Changelog

All notable user-facing changes to Nadeo Exporter Community Edition.

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

[0.0.12]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.12
[0.0.11]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.11
[0.0.10]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.10
[0.0.9]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.9
