# QuickMapCine

A QGIS plugin that drives the 3D Map View camera along parametric paths and exports the result as a video flythrough — point clouds, meshes, DEM terrain, extruded vectors, anything loaded in the 3D scene.

## Features

- **Parametric camera paths**: Helix, Lissajous, Torus Knot, Trefoil Knot, and Fly Through, each with tunable radius/height/frequency/turns parameters. Curve size can be calculated automatically from the extent of a point cloud layer.
- **Trajectory import**: load a previously-exported `trajectory.json`, or a CSV from another tool with an interactive column-mapping step (position + look-at target per row).
- **Preview**: rehearse the flythrough live in the 3D view (with an optional on-canvas path/camera-position visualization) before committing to an export, without capturing any frames.
- **Export**: render every frame and stitch them into a video via `ffmpeg`.
- **Save trajectory**: export the generated (or imported) keyframes back out to `trajectory.json`/CSV for reuse or editing in another tool.
- **Rotation controls**: apply an extra x/y/z rotation on top of the generated path.
- **Works without an open 3D view**: preview, path visualization, and centering on a point cloud all fall back to a headless coordinate-conversion path when no 3D view is open, instead of forcing one open.

## Requirements

- QGIS 3.36+
- `ffmpeg` available on `PATH` for video export

## Installation

1. Download the latest release zip (or build one from source — see below).
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**, select the zip.
3. Enable **QuickMapCine** in the plugin list.

### From source

Clone (or copy) this repository into your QGIS plugins folder:

- Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
- Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`

Then enable it from **Plugins → Manage and Install Plugins**.

## Usage

1. Load a point cloud (or other 3D-capable layer) and open the 3D Map View.
2. Open the QuickMapCine dock (toolbar icon or Plugins menu).
3. On the **Trajectory** tab, pick a curve and parameters (or import an existing trajectory), set a focus point, and click **Generate Trajectory**.
4. Use **Preview Trajectory** to check the path, and **Save** to export the keyframes to a file if needed.
5. On the **Export** tab, use **Preview Camera Feed** to rehearse the run in the 3D view, then **Export** to render frames and stitch the final video.

## License

MIT — see [LICENSE](LICENSE).
