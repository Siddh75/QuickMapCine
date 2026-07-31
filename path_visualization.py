"""Draws the camera path, camera positions, look-direction ticks, and focus
point as real (temporary, in-memory) vector layers, colour-graded by time_s --
so they show up in both the 2D map canvas and, via each layer's 3D symbol, in
the 3D Map View too.

Real layers (not QgsRubberBand, which the first version of this feature used)
are what make 3D visibility possible at all -- a rubber band is a 2D-canvas-
only overlay with no equivalent in the 3D scene. That's also exactly why these
layers get explicitly hidden (layer-tree visibility, not deleted) for the
duration of any Export run: a real layer visible in the 3D view WOULD be
captured by the export's screen grab (animator.py's _advance()) if left
checked on. dockwidget.py handles that hide/restore around a run via
CameraPathAnimator's on_finished callback -- this module only owns show/hide,
not when to call it.

2D coloring gradients by time_s via QgsGraduatedSymbolRenderer. 3D coloring is
solid per-layer instead -- QGIS 3.44's Python bindings don't expose any
data-defined-property API on 3D material settings at all (confirmed against
QGIS's own auto-generated .sip bindings; see _style_3d_solid's docstring), so
a real per-vertex 3D gradient isn't currently reachable from PyQGIS. Those 3D
symbol classes (qgis._3d) are also explicitly documented by QGIS itself as
"tech preview, not stable API" -- every 3D-specific call here is wrapped so a
failure degrades to 2D-only display (which still works, still gradient-
coloured) rather than breaking the plugin.
"""
import math

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsGradientColorRamp,
    QgsGraduatedSymbolRenderer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsMessageLog,
    QgsPoint,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVector3D,
    QgsVectorLayer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

try:
    from qgis._3d import (
        QgsLine3DSymbol,
        QgsPhongMaterialSettings,
        QgsPoint3DSymbol,
        QgsVectorLayer3DRenderer,
    )
    _HAVE_3D_SYMBOLS = True
except ImportError:
    _HAVE_3D_SYMBOLS = False


def _log(msg, level=Qgis.Info):
    QgsMessageLog.logMessage(msg, "QuickMapCine", level)


_START_COLOR = QColor("#1e78ff")  # t=0 -- matches the previous rubber-band blue
_END_COLOR = QColor("#ff3c3c")    # t=duration
_FOCUS_COLOR = QColor(255, 0, 0)  # matches the previous QgsVertexMarker red
_LOOK_COLOR = QColor("#ffa500")   # 3D look-direction vectors -- see _style_3d_solid;
                                   # distinct from both the path (blue) and the
                                   # focus marker (red) so the three don't blend
                                   # together now that none of them gradient in 3D.
_PATH_3D_COLOR = _START_COLOR

# Camera positions can only be ONE solid color per layer in the 3D view (no
# working per-feature/data-defined color API on this QGIS version -- see
# _style_3d_solid). N_SHADE_BUCKETS splits the sampled positions into this many
# time-ordered layers, each one solid, progressively blue->red color, to
# approximate the smooth 2D time gradient as a step gradient in 3D.
_N_SHADE_BUCKETS = 6

# Look-direction vectors are drawn at a fixed fraction of that frame's own
# camera->look-at distance, not a full line to the look-at point -- a full
# line converges every sampled tick to a single point for any constant-focus
# curve (reads as spokes on a wheel, not as each position's facing direction),
# and would be a near-zero-length line for Fly Through's forward/sideways
# modes (whose "look-at" is a virtual point ~1 unit from the camera, an
# implementation detail of _pose_for() -- see animator.py -- not a meaningful
# distance to draw). A fixed fraction of distance reads correctly in both cases.
_LOOK_VECTOR_FRACTION = 0.18

# 3D point marker radius, as a fraction of the path's own bounding diagonal --
# scales sensibly whether the path spans metres or kilometres, unlike QGIS's
# default point3D shape (a cylinder, radius=10 *map units*, confirmed against
# QGIS's source -- enormous next to a typical camera path) or a fixed absolute
# radius that would be wrong at a different scale.
_MARKER_RADIUS_FRACTION = 0.008
_MIN_MARKER_RADIUS = 0.3

_POINTS_2D_SIZE_MM = 2.0
_FOCUS_2D_SIZE_MM = 2.5

_LAYER_NAMES = {
    "path": "QuickMapCine: Camera Path",
    "points": "QuickMapCine: Camera Positions",
    "look": "QuickMapCine: Look Direction",
    "focus": "QuickMapCine: Focus Point",
}


def _shade_layer_name(i, n):
    return f"QuickMapCine: Camera Positions (shade {i + 1}/{n})"


def _pt(vec3d):
    """QgsVector3D (what worldToMapCoordinates returns) -> QgsPoint with Z, the
    vertex type LineStringZ/PointZ geometries need."""
    return QgsPoint(vec3d.x(), vec3d.y(), vec3d.z())


def _lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
    )


class PathVisualizer:
    """Owns the four temporary layers and their lifecycle. One instance per
    dock widget; layers aren't added to the project until first needed
    (ensure_added), so nothing appears just from the plugin loading."""

    def __init__(self):
        self._added = False
        self._create_layers()

    def _create_layers(self):
        self.path_layer = self._make_layer("LineString", _LAYER_NAMES["path"])
        self.points_layer = self._make_layer("Point", _LAYER_NAMES["points"])
        self.look_layer = self._make_layer("LineString", _LAYER_NAMES["look"])
        self.focus_layer = self._make_layer("Point", _LAYER_NAMES["focus"])
        # See _N_SHADE_BUCKETS above -- 2D-invisible (_style_2d_invisible), 3D-only
        # step-gradient companions to points_layer, which stays smooth-gradient
        # 2D-only (no 3D renderer of its own -- these take over 3D duty for
        # camera positions so they aren't drawn twice).
        self.shade_layers = [
            self._make_layer("Point", _shade_layer_name(i, _N_SHADE_BUCKETS))
            for i in range(_N_SHADE_BUCKETS)
        ]
        self.layers = [
            self.path_layer, self.points_layer, self.look_layer, self.focus_layer, *self.shade_layers,
        ]

    @staticmethod
    def _make_layer(geom_kind, name):
        crs = QgsProject.instance().crs()
        authid = crs.authid() or "EPSG:4326"
        layer = QgsVectorLayer(f"{geom_kind}Z?crs={authid}", name, "memory")
        layer.dataProvider().addAttributes([
            QgsField("frame", QVariant.Int),
            QgsField("time_s", QVariant.Double),
        ])
        layer.updateFields()
        return layer

    def _ensure_layers_alive(self):
        """PathVisualizer is long-lived (one instance for the dock widget's
        whole life), but the project these layers were added to isn't -- it can
        be closed, replaced (File > New / opening another project), or the user
        can simply remove one of these named layers from the Layers panel by
        hand without this object ever finding out. Any of those destroys the
        underlying C++ QgsVectorLayer while this Python wrapper keeps existing,
        so the next call on it raises "wrapped C/C++ object ... has been
        deleted" (hit in practice -- see the crash this was added to fix).
        Recreate all four together rather than patching just the missing one,
        since a project change invalidates them as a set, not individually.
        """
        if any(sip.isdeleted(layer) for layer in self.layers):
            _log("path/focus layers were deleted out from under the plugin (project changed?) -- recreating")
            self._added = False
            self._create_layers()

    def ensure_added(self):
        self._ensure_layers_alive()
        if self._added:
            return
        for layer in self.layers:
            QgsProject.instance().addMapLayer(layer, True)
        self._added = True

    def set_visible(self, visible):
        """Toggles layer-tree visibility for all four layers together -- used
        by both the UI's "show path" checkbox and, via dockwidget.py's
        on_finished wiring, to hide these from an Export run's screen grab.
        """
        self._ensure_layers_alive()
        if not self._added:
            if not visible:
                return  # nothing to hide if it was never shown
            self.ensure_added()
        root = QgsProject.instance().layerTreeRoot()
        for layer in self.layers:
            node = root.findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(visible)

    def clear(self):
        self._ensure_layers_alive()
        for layer in self.layers:
            layer.dataProvider().truncate()
            layer.updateExtents(True)
            layer.triggerRepaint(False)
            try:
                layer.trigger3DUpdate()
            except AttributeError:
                pass

    def remove_from_project(self):
        self._ensure_layers_alive()
        if not self._added:
            return
        QgsProject.instance().removeMapLayers([layer.id() for layer in self.layers])
        self._added = False

    def update(self, animator, map_settings, map_canvas_2d=None):
        """Rebuilds every feature from animator.frame_pose()/time_for_frame()
        and restyles every layer. Call after anything that could move the
        path: curve params, focus point, look mode, or a new import.

        map_canvas_2d: the 2D QgsMapCanvas (dockwidget.py passes
        self.iface.mapCanvas()) -- optional, but strongly recommended.
        layer.triggerRepaint(False) alone is what actually invalidates
        QgsMapRendererCache's per-layer cache entry for this layer
        (confirmed against QGIS's own source: QgsMapRendererCache connects
        to every cached layer's repaintRequested signal and unconditionally
        clears that layer's cache entry on it -- no extent/scale/isModified
        gating), so it should be sufficient on its own. canvas.refresh() at
        the end is deliberate belt-and-suspenders redundancy on top of
        that, not a substitute for it -- there's no way to verify from
        source alone that every QGIS 3.44 build's canvas-to-layer signal
        wiring is intact in every circumstance a plugin might catch it in.
        """
        self._ensure_layers_alive()
        self.ensure_added()

        n = animator.frame_count
        duration = max(animator.duration_s, 1e-6)
        # Caps feature count for long/high-fps clips -- a 10s@30fps path (300
        # frames) is already plenty of points; a 60s clip shouldn't mean 1800.
        point_stride = max(n // 300, 1)
        tick_stride = max(n // 24, 1)  # look-direction ticks: sparser, or they'd be unreadable clutter

        path_feats, point_feats, look_feats = [], [], []
        shade_feats = [[] for _ in range(_N_SHADE_BUCKETS)]
        prev_pt = None
        targets = []
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        for i in range(n):
            position, center, distance, _, _ = animator.frame_pose(i)
            pos_map = map_settings.worldToMapCoordinates(position)
            time_s = animator.time_for_frame(i)
            min_x, max_x = min(min_x, pos_map.x()), max(max_x, pos_map.x())
            min_y, max_y = min(min_y, pos_map.y()), max(max_y, pos_map.y())

            if prev_pt is not None:
                seg = QgsFeature()
                seg.setGeometry(QgsGeometry.fromPolyline([_pt(prev_pt), _pt(pos_map)]))
                seg.setAttributes([i, time_s])
                path_feats.append(seg)
            prev_pt = pos_map

            if i % point_stride == 0 or i == n - 1:
                feat = QgsFeature()
                feat.setGeometry(QgsGeometry.fromPoint(_pt(pos_map)))
                feat.setAttributes([i, time_s])
                point_feats.append(feat)

                # Same sample, duplicated into whichever time-bucket layer it
                # falls in -- see _N_SHADE_BUCKETS above for why this exists.
                bucket = min(int((time_s / duration) * _N_SHADE_BUCKETS), _N_SHADE_BUCKETS - 1)
                shade_feat = QgsFeature()
                shade_feat.setGeometry(QgsGeometry.fromPoint(_pt(pos_map)))
                shade_feat.setAttributes([i, time_s])
                shade_feats[bucket].append(shade_feat)

            if i % tick_stride == 0 or i == n - 1:
                look_map = map_settings.worldToMapCoordinates(center)
                targets.append((look_map.x(), look_map.y()))

                # Short direction vector (position -> look-at, normalized then
                # rescaled to _LOOK_VECTOR_FRACTION of this frame's own
                # distance) -- see _LOOK_VECTOR_FRACTION's docstring above for
                # why this replaced a full line to the look-at point.
                dx, dy, dz = center.x() - position.x(), center.y() - position.y(), center.z() - position.z()
                seg_len = math.sqrt(dx * dx + dy * dy + dz * dz)
                if seg_len > 1e-9:
                    vec_len = min(distance * _LOOK_VECTOR_FRACTION, seg_len)
                    ux, uy, uz = dx / seg_len, dy / seg_len, dz / seg_len
                    vec_end_scene = QgsVector3D(
                        position.x() + ux * vec_len, position.y() + uy * vec_len, position.z() + uz * vec_len,
                    )
                    vec_end_map = map_settings.worldToMapCoordinates(vec_end_scene)
                    min_x, max_x = min(min_x, vec_end_map.x()), max(max_x, vec_end_map.x())
                    min_y, max_y = min(min_y, vec_end_map.y()), max(max_y, vec_end_map.y())
                    seg = QgsFeature()
                    seg.setGeometry(QgsGeometry.fromPolyline([_pt(pos_map), _pt(vec_end_map)]))
                    seg.setAttributes([i, time_s])
                    look_feats.append(seg)

        self._grow_scene_extent(map_settings, min_x, min_y, max_x, max_y)
        self._replace_features(self.path_layer, path_feats)
        self._replace_features(self.points_layer, point_feats)
        self._replace_features(self.look_layer, look_feats)
        for bucket_layer, feats in zip(self.shade_layers, shade_feats):
            self._replace_features(bucket_layer, feats)

        # A single focus marker only makes sense if the look-at target is
        # (nearly) constant across the path -- true for every curve except Fly
        # Through's forward/sideways modes, and for an imported file that
        # genuinely orbits one point. When it varies, the look-direction
        # vectors above already show where the camera's aiming each moment.
        constant_target = targets and all(
            abs(x - targets[0][0]) < 1e-6 and abs(y - targets[0][1]) < 1e-6 for x, y in targets
        )
        if constant_target:
            focus_map = map_settings.worldToMapCoordinates(animator.frame_pose(0)[1])
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPoint(_pt(focus_map)))
            feat.setAttributes([0, 0.0])
            self._replace_features(self.focus_layer, [feat])
        else:
            self._replace_features(self.focus_layer, [])

        # 3D point marker radius, scaled to this path's own size -- see
        # _MARKER_RADIUS_FRACTION's docstring above.
        diagonal = math.hypot(max_x - min_x, max_y - min_y) if max_x > min_x else 0.0
        marker_radius = max(diagonal * _MARKER_RADIUS_FRACTION, _MIN_MARKER_RADIUS)

        self._style_2d_gradient(self.path_layer, is_line=True)
        self._style_2d_gradient(self.points_layer, is_line=False, size_mm=_POINTS_2D_SIZE_MM)
        self._style_2d_gradient(self.look_layer, is_line=True)
        self._style_2d_solid(self.focus_layer, _FOCUS_COLOR, size_mm=_FOCUS_2D_SIZE_MM)
        for bucket_layer in self.shade_layers:
            # 3D-only -- invisible in the 2D canvas so they don't sit on top of
            # points_layer's own (smooth-gradient) 2D markers as visible clutter.
            self._style_2d_invisible(bucket_layer)

        # Solid colors, not a time gradient, in 3D -- see _style_3d_solid's
        # docstring: data-defined material properties (what a real per-vertex
        # gradient would need) aren't exposed to Python at all as of QGIS 3.44.
        # The 2D map canvas above still gets the full gradient (QgsGraduated-
        # SymbolRenderer is a completely different, unaffected API). points_layer
        # itself carries no 3D renderer -- shade_layers below take over 3D duty
        # for camera positions (stepped blue->red by time) so positions aren't
        # drawn twice in the 3D view.
        self._style_3d_solid(self.path_layer, _PATH_3D_COLOR, is_line=True)
        self._style_3d_solid(self.look_layer, _LOOK_COLOR, is_line=True)
        self._style_3d_solid(self.focus_layer, _FOCUS_COLOR, is_line=False, point_radius=marker_radius)
        for i, bucket_layer in enumerate(self.shade_layers):
            t = i / max(_N_SHADE_BUCKETS - 1, 1)
            self._style_3d_solid(
                bucket_layer, _lerp_color(_START_COLOR, _END_COLOR, t), is_line=False, point_radius=marker_radius,
            )

        for layer in self.layers:
            # deferredUpdate=False, explicitly -- QGIS source
            # (qgsmapcanvas.cpp's layerRepaintRequested()) only actually calls
            # refresh() when this is False; True just marks the canvas dirty
            # for some later refresh that may not come from a plugin script
            # running synchronously outside the normal edit-session flow.
            # This is also what invalidates QgsMapRendererCache's per-layer
            # cached image (confirmed against QgsMapRendererCache's own
            # source: it connects to every cached layer's repaintRequested
            # signal and unconditionally clears that layer's cache entry).
            layer.triggerRepaint(False)
            # triggerRepaint() only asks the 2D canvas to redraw -- it has no
            # connection to the 3D scene at all. Qgs3DMapScene only rebuilds a
            # layer's chunked 3D entity in response to layer.request3DUpdate()
            # (emitted by trigger3DUpdate()), or QgsVectorLayer's own
            # selectionChanged/layerModified/subsetStringChanged signals --
            # confirmed against QGIS's source (qgs3dmapscene.cpp's
            # addLayerEntity(), qgsmaplayer.cpp/.h). None of those fire from a
            # direct data-provider update: these are "memory" provider layers,
            # and QgsMemoryProvider.truncate()/addFeatures() never emit
            # dataChanged() either (confirmed in qgsmemoryprovider.cpp) -- the
            # signal chain QgsVectorLayer::setDataProvider() wires up to relay
            # a provider's dataChanged() into the layer's own never fires for
            # these calls.
            try:
                layer.trigger3DUpdate()
            except AttributeError:
                pass  # older QGIS without this method

        # Defensive redundancy on top of triggerRepaint(False) above, not a
        # substitute for it -- see update()'s docstring for map_canvas_2d.
        if map_canvas_2d is not None:
            try:
                map_canvas_2d.refresh()
            except Exception as exc:
                _log(f"2D canvas refresh() failed: {exc}", level=Qgis.Warning)

    @staticmethod
    def _grow_scene_extent(map_settings, min_x, min_y, max_x, max_y):
        """QGIS's 3D view has a hard scene extent (Qgs3DMapSettings.extent(),
        normally set once from the terrain/point-cloud's own footprint when the
        3D view was first opened) that vector-layer 3D tiling is built against:
        QgsVectorLayerChunkLoaderFactory queries each tile with a filter rect
        derived from that extent, so features outside it are never loaded into
        the chunk tree at all -- not merely frustum-culled per frame, but
        permanently absent. A generated camera path (radius/height) is often
        bigger than the point cloud that originally set this extent, so most of
        the path/positions/look-direction silently never render in 3D. Fix:
        grow (never shrink -- toggling a param back down shouldn't fight
        whatever extent is already there) the 3D scene extent to cover the
        path whenever it's shown.
        """
        if min_x > max_x:
            return  # no points collected (n == 0) -- nothing to grow for
        try:
            current = map_settings.extent()
        except AttributeError:
            return  # not a real Qgs3DMapSettings (e.g. a test double) -- skip

        path_rect = QgsRectangle(min_x, min_y, max_x, max_y)
        # Small margin so points exactly on the boundary aren't excluded by a
        # strict >= / <= comparison during tiling.
        margin = max(path_rect.width(), path_rect.height()) * 0.02 + 1.0
        path_rect.grow(margin)

        grown = QgsRectangle(current)
        grown.combineExtentWith(path_rect)
        if grown != current:
            try:
                map_settings.setExtent(grown)
            except Exception as exc:
                _log(f"could not grow 3D scene extent to fit camera path: {exc}", level=Qgis.Warning)

    @staticmethod
    def _replace_features(layer, features):
        provider = layer.dataProvider()
        provider.truncate()
        if features:
            provider.addFeatures(features)
        # force=True -- QGIS source (qgsvectorlayer.cpp) shows this recomputes
        # the extent from the provider's actual features unconditionally,
        # rather than trusting cached/project metadata that may be stale
        # after a direct provider.truncate()/addFeatures() call (this bypasses
        # the edit buffer entirely, so nothing else recomputes it for us).
        layer.updateExtents(True)

    def _style_2d_gradient(self, layer, is_line, size_mm=None):
        try:
            ramp = QgsGradientColorRamp(_START_COLOR, _END_COLOR)
            props = {"size": str(size_mm)} if (size_mm is not None and not is_line) else {}
            base_symbol = (QgsLineSymbol if is_line else QgsMarkerSymbol).createSimple(props)
            renderer = QgsGraduatedSymbolRenderer.createRenderer(
                layer, "time_s", 16, QgsGraduatedSymbolRenderer.EqualInterval, base_symbol, ramp,
            )
            if renderer is not None:
                layer.setRenderer(renderer)
        except Exception as exc:  # 2D styling failing shouldn't break the plugin
            _log(f"2D gradient styling failed for '{layer.name()}': {exc}", level=Qgis.Warning)

    @staticmethod
    def _style_2d_solid(layer, color, size_mm=4.0):
        try:
            symbol = QgsMarkerSymbol.createSimple({
                "color": f"{color.red()},{color.green()},{color.blue()},255", "size": str(size_mm),
            })
            # Explicit QgsSingleSymbolRenderer rather than mutating layer.renderer()
            # in place -- a freshly created memory layer's default renderer isn't
            # guaranteed to be a type with setSymbol() (or to exist at all before
            # the layer's been added to a project in every QGIS version).
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        except Exception as exc:
            _log(f"2D solid styling failed for '{layer.name()}': {exc}", level=Qgis.Warning)

    @staticmethod
    def _style_2d_invisible(layer):
        """Fully transparent 2D symbol -- used for the 3D-only shade-bucket
        layers so they don't draw a second, redundant marker on top of
        points_layer's own (smooth-gradient) 2D symbology in the map canvas."""
        try:
            symbol = QgsMarkerSymbol.createSimple({"color": "0,0,0,0", "outline_color": "0,0,0,0", "size": "0"})
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        except Exception as exc:
            _log(f"2D invisible styling failed for '{layer.name()}': {exc}", level=Qgis.Warning)

    @staticmethod
    def _style_3d_solid(layer, color, is_line, point_radius=None):
        """One flat color per layer, not the 2D view's time gradient.

        A real per-vertex/per-time 3D gradient would need a data-defined
        material property (varying diffuse color by the "time_s" field, the
        way _style_2d_gradient does in 2D via QgsGraduatedSymbolRenderer). That
        was attempted here originally via
        QgsPhongMaterialSettings.setDataDefinedProperty(...), but on a real
        QGIS 3.44.10 session that raised:
            AttributeError: 'QgsPhongMaterialSettings' object has no attribute
            'setDataDefinedProperty'
        Confirmed against QGIS's own source (qgsabstractmaterialsettings.h /
        its auto-generated .sip.in for the 3.44 branch): the entire data-
        defined-properties block on QgsAbstractMaterialSettings (the base
        class Phong and every other 3D material inherits from) is wrapped in
        `#ifndef SIP_RUN` -- it's C++-only, not exposed to Python at all in
        this branch, under any method name. There is currently no working
        Python path to a real per-feature 3D color gradient.

        This matters beyond just losing the gradient look: the original code
        called setDataDefinedProperty() *before* setRenderer3D() in the same
        try block, so that AttributeError aborted the whole function early --
        setRenderer3D() was never reached, meaning the path/points/look layers
        never got a real 3D renderer at all. Without one, QGIS falls back to
        some default (terrain-draped, ignoring Z) representation -- which is
        what actually caused the "path renders flat from every angle" and
        "look direction doesn't reach the focus point" reports, not the
        altitude-clamping default this method also fixes. Building a plain
        solid-color material here, with no data-defined step to fail on,
        guarantees setRenderer3D() always actually runs.
        """
        if not _HAVE_3D_SYMBOLS:
            return
        try:
            material = QgsPhongMaterialSettings()
            material.setAmbient(color)
            material.setDiffuse(color)
            symbol = QgsLine3DSymbol() if is_line else QgsPoint3DSymbol()
            if is_line:
                symbol.setWidth(1.0)
            else:
                # QgsPoint3DSymbol defaults to a Cylinder shape with radius=10,
                # length=10 *map units* (confirmed against QGIS's source) --
                # enormous next to a typical camera path, hence the previously
                # oversized markers. A small sphere, scaled by the caller to
                # this path's own size (see _MARKER_RADIUS_FRACTION), reads as
                # a marker instead of a landmark. Wrapped separately so a
                # failure here (e.g. a future QGIS renaming this API too --
                # see this method's docstring for a precedent) still leaves
                # the point visible in 3D at QGIS's own default size/shape,
                # rather than losing the 3D renderer entirely.
                try:
                    symbol.setShape(Qgis.Point3DShape.Sphere)
                    symbol.setShapeProperties({"radius": point_radius if point_radius else 1.0})
                except Exception as exc:
                    _log(f"could not resize 3D point marker for '{layer.name()}': {exc}", level=Qgis.Warning)
            # Both symbol types default to Relative clamping (Z added on top of
            # the terrain's own elevation at that X/Y). Our Z is already a real,
            # absolute elevation (round-tripped through worldToMapCoordinates()),
            # so Relative double-counts it -- the whole path renders shifted up
            # (or down) by however tall the terrain is at each point. Absolute
            # uses Z as-is, matching the point cloud's own real elevation.
            symbol.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
            symbol.setMaterialSettings(material)
            layer.setRenderer3D(QgsVectorLayer3DRenderer(symbol))
        except Exception as exc:
            _log(
                f"3D styling failed for '{layer.name()}' -- qgis._3d's symbol classes "
                f"are a QGIS tech-preview API and can vary between versions; the layer "
                f"will still show in 2D. Error: {exc}",
                level=Qgis.Warning,
            )
