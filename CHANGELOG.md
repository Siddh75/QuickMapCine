# Changelog

## v0.2 (in progress)

### Added
- **DEM/raster elevation support**: the focus point, curve auto-fit, and Pick on map now work with any raster layer that has Elevation enabled (Layer Properties → Elevation → Enable, "Represents elevation surface"), not just point clouds.
- **Real terrain rendering in the 3D preview** when a DEM layer is present, instead of a flat drape (QGIS 3.42+; older versions fall back to flat/off terrain with a log message pointing at the manual workaround).
- **CSV orientation-angle import**: a trajectory CSV's orientation can now come from camera angles (Pitch/Yaw) instead of a look-at target, with a choice between QGIS's native convention or an aviation/gimbal-style one. The source is an explicit choice in the mapping form, never inferred from which columns happen to be filled in.
- **Translate X/Y/Z**: shifts the camera's position by a fixed offset without moving the look-at point — a companion to Rotation, applied after it. Works for both generated curves and imported trajectories.
- Plugin icon, and QGIS Plugin Repository metadata (repository/tracker/homepage links) for marketplace submission.

### Changed
- **One coordinate system in the UI**: Focus Point X/Y/Z and CSV trajectory import now always use map/project CRS — the same coordinates the 2D map shows. Scene-local (3D-view-relative) coordinates are no longer displayed or accepted anywhere in the plugin.
- CSV mapping form reorganized: Orientation type and Angle convention moved next to the orientation fields they control, Roll grouped with Pitch/Yaw, Frame/Time kept as a separate, later group.
- Removed the redundant instructional text shown after picking a CSV file.
- Removed the "experimental" plugin flag.

### Fixed
- **Preview Trajectory no longer touches the focus point.** It used to silently auto-center (and could reset a value you'd just typed in) the first time it ran each session; it now only reads whatever focus is already set.
- DEM terrain now actually renders in Preview Trajectory / Preview Camera Feed — previously the 3D view always forced terrain rendering off, a leftover from before DEM support existed.
- Several places that silently swallowed exceptions instead of logging them (flagged by the QGIS Plugin Repository's security scanner) now log properly.

## v0.1 — initial release
- First public release: parametric camera paths (Helix, Lissajous, Torus Knot, Trefoil Knot, Fly Through) that fly around point cloud data.
- Live, in-3D-view preview before committing to a full render.
- Video export via `ffmpeg`.
- Trajectory import/export as JSON or CSV, so a path can be reused or edited outside the plugin.
- Auto-fit the curve to a point cloud's extent, or set it by hand.
- Rotation X/Y/Z controls.
- Works without an open 3D view — preview and path visualization fall back to a headless coordinate conversion instead of forcing one open.
