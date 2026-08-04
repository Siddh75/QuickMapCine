QuickMapCine update: it now works with DEM terrain, not just point clouds.

Until now, the focus point, auto-fit curve sizing, and click-to-pick-on-map all needed a point cloud loaded to make sense of your scene. That left out a huge chunk of QGIS users — anyone flying a camera over a DEM/elevation raster instead.

Fixed that. Any raster layer with Elevation enabled (Layer Properties → Elevation → Enable) now works as a first-class elevation source, right alongside point clouds:
- Focus point and curve auto-fit read the DEM's real extent and elevation range
- Pick on map samples the DEM's actual height under your click
- The 3D preview renders real terrain shape, not a flat drape

Point clouds are great, but they're not what most people have sitting in a project. DEMs are — SRTM tiles, LiDAR-derived rasters, whatever your agency already publishes. This makes QuickMapCine usable for a lot more terrain flythroughs out of the box.

#QGIS #GIS #OpenSource #Geospatial #DEM
