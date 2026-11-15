# Changelog

All notable user-facing changes to Nadeo Exporter Community Edition.

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

### Documentation

- Added direct download links for the addon ZIP to the README.
- Added a troubleshooting note for bridge port errors.

## [0.0.9] - 2026-11-15

- First public preview release.

[0.0.10]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.10
[0.0.9]: https://github.com/StupsKiesel/nadeo-exporter-ce/releases/tag/v0.0.9
