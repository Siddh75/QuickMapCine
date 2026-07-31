Excited to share a side project: QuickMapCine, a QGIS plugin for turning your 3D scenes into cinematic flythrough videos.

Point it at anything loaded in QGIS's 3D Map View — point clouds, meshes, DEM terrain, extruded vectors — and it flies the camera along a parametric path (helix, lissajous, torus knot, trefoil knot, or a straight fly-through) while it always keeps your data in frame. Preview the run live, tweak the path, then export frames and stitch them into a video with ffmpeg.

A few things I focused on:
- Live, in-3D-view preview before you commit to a full render
- Import/export trajectories as JSON or CSV, so a path built in QuickMapCine can be reused or edited elsewhere
- Works even without a 3D view open — no need to keep one pinned just to generate a path
- Auto-fit the path to your point cloud's extent, or dial it in by hand

Just uploaded it to the QGIS Plugin Repository and it's sitting in the review queue now — will share here once it's live and installable straight from QGIS.

Would love feedback from anyone doing 3D/point cloud work in QGIS — curves, controls, whatever's missing.

#QGIS #GIS #OpenSource #PointCloud #Geospatial
