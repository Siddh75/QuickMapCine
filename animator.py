"""Drives a Qgs3DMapCanvas camera along a curves.py path and, optionally,
exports frames + stitches them into a video with ffmpeg.

Camera moves via QgsCameraController.setLookingAtPoint(center, distance,
pitch, yaw) -- the orbit-style API the whole QGIS 3D navigation system uses.

Frame capture uses QScreen.grabWindow(0, x, y, w, h) -- a screen-region grab at
the canvas's real global position, not a grab of its own window handle (which
doesn't reliably clip to just its own pixels for an embedded/docked window).
Qgs3DMapCanvas.saveAsImage() exists in the C++ class but was never exposed to
Python (checked the generated .sip bindings for 3.44 -- only mapSettings(),
scene() and cameraController() are bound), so there is no off-screen/async
capture path available from a plugin. This means the 3D view must be visible
and unobstructed on screen while exporting.
"""
import csv
import json
import math
import os
import shutil
import subprocess

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsVector3D
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QPoint, QTimer
from qgis.PyQt.QtGui import QGuiApplication

from .curves import CURVES


def _log(msg, level=Qgis.Info):
    QgsMessageLog.logMessage(msg, "QuickMapCine", level)


def _canvas_is_dead(canvas):
    """True only for a real (SIP-wrapped) Qgs3DMapCanvas whose underlying C++
    object has actually been deleted -- e.g. the user closed the 3D view's
    dock mid-run. sip.isdeleted() raises TypeError on anything that isn't a
    SIP-wrapped object at all, which is exactly what dockwidget.py's headless
    _HeadlessCanvas3D stand-in is (see its docstring) -- a plain Python
    object used only when no real 3D view is open, so path/focus previews
    can still be computed without ever creating one. That stand-in can never
    go "dangling" the way a real canvas can (it has normal Python object
    lifetime, no separate C++ side to lose), so treating it as always-alive
    here is correct, not just a safe default."""
    if canvas is None:
        return True
    try:
        return sip.isdeleted(canvas)
    except TypeError:
        return False


# ponytail: QGIS.app launched from Finder/Dock (not a terminal) often gets a PATH
# that excludes Homebrew's install dirs, so PATH-only lookup misses an ffmpeg that
# works fine everywhere else. Check the common install locations as a fallback
# instead of asking users to relaunch QGIS from a terminal.
_FFMPEG_FALLBACK_PATHS = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in _FFMPEG_FALLBACK_PATHS:
        if os.path.isfile(path):
            return path
    return None


def _offset_to_pose(dx, dy, dz):
    """Cartesian offset from the focus point -> (distance, pitch, yaw).

    Per QgsCameraPose: pitch 0 = camera looking straight down, 90 = looking
    from the side; yaw is heading in degrees, matching QGIS's own convention.

    Verified against QGIS 3.44's actual source (src/3d/qgscamerapose.cpp's
    updateCamera(), src/3d/qgs3dutils.cpp's rotationFromPitchHeadingAngles())
    that the camera's real offset from center is:
        dx =  distance * sin(pitch) * sin(yaw)
        dy = -distance * sin(pitch) * cos(yaw)   -- note the minus sign
        dz =  distance * cos(pitch)
    (rotationFromPitchHeadingAngles() composes q = Rz(yaw) * Rx(pitch), which
    flips the sign on the Y component relative to the naive/expected
    +cos(yaw)). Solving for yaw from a desired (dx, dy) offset therefore needs
    atan2(dx, -dy), not atan2(dx, dy) -- this file used the unflipped version
    for a while, which meant every yaw sent to setLookingAtPoint() was
    mirrored across the X axis from what was intended: the "position" this
    class (and the preview markers path_visualization.py draws) computed for
    a given frame didn't match where the camera controller actually pointed
    the camera, even though cameraPose() read back exactly the (center,
    distance, pitch, yaw) that was sent -- QGIS wasn't rejecting or clamping
    anything, the values sent were just already wrong. See
    log_camera_debug()'s docstring for how this was diagnosed.
    """
    horizontal = math.hypot(dx, dy)
    distance = max(math.hypot(horizontal, dz), 1.0)
    elevation = math.degrees(math.atan2(dz, horizontal))
    pitch = 90.0 - elevation
    yaw = math.degrees(math.atan2(dx, -dy))
    return distance, pitch, yaw


def _pose_to_offset(distance, pitch_deg, yaw_deg):
    """(distance, pitch, yaw) -> Cartesian offset from center. The exact
    algebraic inverse of _offset_to_pose() -- implements the same verified
    formula that function's docstring derives the atan2() calls from:
        dx =  distance * sin(pitch) * sin(yaw)
        dy = -distance * sin(pitch) * cos(yaw)
        dz =  distance * cos(pitch)

    Used by trajectory_io.py to synthesize a "virtual" look-at center when a
    keyframe's orientation is given directly as pitch/yaw (e.g. an imported
    CSV that has camera orientation angles instead of a look-at target)
    rather than derived from an actual look-at point -- the same trick
    _pose_for()'s "forward"/"sideways" look modes use for a curve-derived
    facing direction below. round-trips exactly through _offset_to_pose():
    _offset_to_pose(*_pose_to_offset(d, p, y)) == (d, p, y) for any valid
    pitch in [0, 180] and yaw in (-180, 180].
    """
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)
    dx = distance * math.sin(pitch) * math.sin(yaw)
    dy = -distance * math.sin(pitch) * math.cos(yaw)
    dz = distance * math.cos(pitch)
    return dx, dy, dz


class CameraPathAnimator:
    def __init__(
        self, canvas3d, curve_name, curve_kwargs, focus, duration_s, fps, output_dir=None,
        make_video=True, look_mode="focus", imported_keyframes=None, on_finished=None,
        rotation_x_deg=0.0, rotation_y_deg=0.0, rotation_z_deg=0.0, on_frame=None,
    ):
        """Either generates the path from a curve (curve_name/curve_kwargs/focus,
        the original mode), or plays back imported_keyframes verbatim -- a list of
        dicts (time_s, position, center, distance, pitch, yaw; see trajectory_io.
        load_trajectory()) loaded from a previously-exported or externally-authored
        trajectory file. duration_s/fps are ignored for the imported case: frame
        count and per-keyframe timing come from the file itself (see _time_for_frame),
        though fps is still used as the single QTimer tick rate during playback --
        pass the caller's estimated average fps for that file, not a UI default.

        rotation_x_deg/rotation_y_deg/rotation_z_deg: spins the whole trajectory
        (both position and look-at, for every frame -- generated or imported)
        around focus, applied as a final step in _frame_data(). focus therefore
        doubles as the rotation pivot even in imported mode, where it's
        otherwise unused -- see dockwidget.py's _build_animator(), which passes
        the imported keyframes' own centroid as focus when there's no curve-
        generation focus point to reuse.

        Scene Z is vertical here (matching _offset_to_pose()'s own dx/dy-are-
        horizontal, dz-is-vertical convention -- see that function's docstring),
        so rotation_z_deg is the intuitive one: spins the path like a dial
        viewed from above, without changing its height profile at all.
        rotation_x_deg and rotation_y_deg instead tilt the path's horizontal
        plane itself (each pivots a different horizontal axis into vertical),
        which is what makes all 3 genuinely independent -- e.g. tipping a
        Helix over so it spirals sideways instead of climbing straight up.
        Applied in X, then Y, then Z order (each subsequent rotation happens in
        the frame the previous one left behind -- standard Euler composition;
        fixed and documented since the three don't commute when more than one
        is nonzero at once, see _rotate_point()).
        distance is always exactly preserved (rotation can't change a point's
        distance from the pivot it rotates around); pitch/yaw are recomputed
        from the rotated offset afterward since, unlike a Z-only rotation,
        X/Y rotations do change which direction is "up" relative to the offset.

        on_finished: called once, whether playback runs to completion or is cut
        short by stop() -- e.g. by dockwidget.py's path/focus visualization,
        which needs to know exactly when an export run ends so it can restore
        visibility of the layers it hid for the run's duration (a real map
        layer, unlike the old rubber-band-only preview, would otherwise show up
        in the screen-grabbed frames -- see path_visualization.py).

        on_frame: called after each frame is posed (frame_idx, frame_count),
        frame_idx 0-based -- dockwidget.py uses this to drive the Preview/
        Export run-status line (frame count, elapsed time) without this class
        needing to know anything about Qt widgets itself.
        """
        self.canvas3d = canvas3d
        self.imported_keyframes = imported_keyframes
        self._rotation_rad = (
            math.radians(rotation_x_deg), math.radians(rotation_y_deg), math.radians(rotation_z_deg),
        )
        if imported_keyframes is not None:
            self.curve_name = None
            self.curve_fn = None
            self.curve_kwargs = None
            # Only used as the rotation pivot in imported mode (there's no curve
            # to generate, so no other reason to track a focus point) -- see
            # rotation_x_deg above and dockwidget.py's _build_animator().
            self.focus = focus
            self._focus_map = self._to_map(focus)
            self.frame_count = len(imported_keyframes)
            self.duration_s = imported_keyframes[-1]["time_s"] if imported_keyframes else 0.0
        else:
            self.curve_name = curve_name  # kept for trajectory-file metadata, not just the lookup below
            self.curve_fn, _ = CURVES[curve_name]
            self.curve_kwargs = curve_kwargs
            self.focus = focus  # QgsVector3D, scene-space point the camera threads around
            # focus is captured once, here, as a scene-local point -- but the 3D
            # scene's origin can move *after* this (Qgs3DMapSettings.setExtent()
            # unconditionally recenters origin whenever the scene extent changes,
            # confirmed in QGIS's own qgs3dmapsettings.cpp -- and path_visualization.py
            # calls setExtent() to keep a wide camera path from being clipped out of
            # the 3D view). If origin moves while this animator is still running or
            # still feeding the path preview, a static scene-local focus silently
            # points at the wrong real-world location -- the terrain/point cloud
            # re-centers correctly (QGIS rebuilds those itself), but our camera
            # doesn't, so it visibly drifts away from the data. Storing the
            # origin-independent map-CRS equivalent lets _live_focus() re-derive a
            # correct scene-local point on every frame, however the origin's moved.
            self._focus_map = self._to_map(focus)
            self.frame_count = max(int(duration_s * fps), 2)
            self.duration_s = duration_s
        self.look_mode = look_mode  # "focus", "forward", or "sideways" -- only meaningful when generating
        self.fps = fps
        self.on_finished = on_finished
        self.on_frame = on_frame
        self._finished_called = False
        self.output_dir = output_dir
        self.export = output_dir is not None
        self.make_video = make_video
        # frames get their own subfolder so the export folder isn't just a pile of PNGs
        # with the mp4 mixed in among them.
        self.frames_dir = os.path.join(output_dir, "frames") if self.export else None
        if self.export:
            os.makedirs(self.frames_dir, exist_ok=True)
        self.frame = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self._advance)

    def start(self):
        self.frame = 0
        # A freshly-created canvas isn't guaranteed to be shown/raised yet -- do
        # this for preview too, not just export, or the window can sit there
        # not actually rendering anything while the camera moves underneath it.
        self.canvas3d.show()
        self.canvas3d.requestActivate()
        # showMaximized() is a real no-op while docked -- it's embedded in the
        # dock's layout (isTopLevel() False), confirmed earlier: windowState()
        # flips to Maximized but width()/height() don't move at all. It only does
        # anything once the "QuickMapCine" panel has been floated (its own
        # toolbar has a dock/undock toggle) into a genuine top-level window.
        # Applies to preview too, not just export -- harmless either way.
        if self.canvas3d.isTopLevel():
            self.canvas3d.showMaximized()
            _log("3D view is floating -- maximized it for a bigger view.")
        elif self.export:
            _log(
                "3D view is docked -- can't be maximized. Float it (toolbar's "
                "dock/undock button) before exporting for a bigger capture."
            )
        self.timer.start(int(1000 / self.fps))

    def stop(self):
        self.timer.stop()
        # Fires exactly once whether playback ran to completion (_advance calling
        # this itself) or was cut short (Stop button, or superseded by a new
        # Preview/Export click) -- restoring visibility as soon as capture stops
        # is correct even mid-stitch: _stitch_video() only reads already-saved
        # PNGs, it doesn't touch the screen again.
        if not self._finished_called:
            self._finished_called = True
            if self.on_finished:
                self.on_finished()

    def _to_map(self, scene_point):
        """scene-local -> map-CRS, using whatever origin is current right now.
        Returns None if there's no live canvas to ask (e.g. canvas3d is None, or
        was closed/deleted) -- callers fall back to the static scene-local point."""
        if scene_point is None or _canvas_is_dead(self.canvas3d):
            return None
        try:
            return self.canvas3d.mapSettings().worldToMapCoordinates(scene_point)
        except Exception:
            return None

    def _live_focus(self):
        """self.focus, re-derived from the origin-independent _focus_map every
        call so a moved scene origin (see __init__'s comment) doesn't leave the
        camera pointed at a stale location. Falls back to the static self.focus
        if there's no canvas to convert with (still correct as long as origin
        hasn't moved, which is the only case that's possible without a canvas)."""
        if self._focus_map is not None and not _canvas_is_dead(self.canvas3d):
            try:
                return self.canvas3d.mapSettings().mapToWorldCoordinates(self._focus_map)
            except Exception as exc:
                # Canvas can be torn down between the _canvas_is_dead() check
                # above and this call (e.g. the 3D view closing mid-frame) --
                # fall back to the static focus below rather than crash the
                # running animation, but log it so a genuine bug doesn't go
                # unnoticed.
                _log(f"_live_focus: mapToWorldCoordinates failed, using static focus: {exc}", Qgis.Warning)
        return self.focus

    @staticmethod
    def _rotate_point(point, pivot, angles_rad):
        """point, rotated around pivot by angles_rad = (rx, ry, rz) -- one axis
        rotation at a time, each applied to whatever the previous one left
        behind (X, then Y, then Z; see rotation_x_deg's docstring in __init__
        for why this order is fixed rather than left path-dependent).

        Distance from pivot is always exactly preserved -- every step here is
        a rotation, and a rotation can't change a point's distance from the
        axis/pivot it rotates around, however many of the 3 are combined.
        Rotating both a frame's position AND its look-at target through the
        same 3 angles around the same pivot leaves their relative offset
        rotated by exactly that composed rotation too (the pivot cancels out
        in the subtraction), which is why _frame_data() can safely re-derive
        distance/pitch/yaw from _offset_to_pose() afterward and get a
        consistent result no matter which axes were used.
        """
        x, y, z = point.x() - pivot.x(), point.y() - pivot.y(), point.z() - pivot.z()
        rx, ry, rz = angles_rad

        if rx:
            c, s = math.cos(rx), math.sin(rx)
            y, z = y * c - z * s, y * s + z * c

        if ry:
            c, s = math.cos(ry), math.sin(ry)
            z, x = z * c - x * s, z * s + x * c

        if rz:
            c, s = math.cos(rz), math.sin(rz)
            x, y = x * c - y * s, x * s + y * c

        return QgsVector3D(pivot.x() + x, pivot.y() + y, pivot.z() + z)

    def _pose_for(self, t):
        """(center, distance, pitch, yaw) for setLookingAtPoint() at time t."""
        dx, dy, dz = self.curve_fn(t, **self.curve_kwargs)
        focus = self._live_focus()
        if self.look_mode == "focus":
            return (focus, *_offset_to_pose(dx, dy, dz))

        # Fixed-direction look ("forward"/"sideways"): setLookingAtPoint() can only
        # aim *at* a center point, so a free-look camera is faked by placing a
        # virtual center one unit behind the camera along the desired facing
        # direction -- the offset from that virtual center to the camera's real
        # position is exactly the facing direction, letting the same verified API
        # do the work.
        eps = 0.5 / max(self.frame_count - 1, 1)
        px, py, pz = self.curve_fn(max(t - eps, 0.0), **self.curve_kwargs)
        nx, ny, nz = self.curve_fn(min(t + eps, 1.0), **self.curve_kwargs)
        travel = (nx - px, ny - py, nz - pz)

        if self.look_mode == "sideways":
            # Rotate the travel direction 90 degrees in the horizontal plane,
            # levelling the vertical component for a level side-on look.
            # NOTE: this levels index 1 (y), but confirmed against QGIS 3.44's
            # actual source (see _canvas_is_dead's docstring / __init__'s "Scene
            # Z is vertical" convention used everywhere else in this file) scene
            # z is the vertical axis, not y -- mapToWorldCoordinates() does a
            # plain per-axis origin subtraction, no swap. This line predates
            # that finding and may be levelling the wrong axis for "sideways"
            # look mode; flagged here rather than changed blind since it's
            # unrelated to the bug this comment update was made for.
            direction = (-travel[2], 0.0, travel[0])
        else:  # "forward"
            direction = travel

        length = math.hypot(*direction) or 1.0
        unit = (direction[0] / length, direction[1] / length, direction[2] / length)

        camera_pos = (focus.x() + dx, focus.y() + dy, focus.z() + dz)
        virtual_center = QgsVector3D(
            camera_pos[0] - unit[0], camera_pos[1] - unit[1], camera_pos[2] - unit[2]
        )
        return (virtual_center, *_offset_to_pose(*unit))

    def frame_pose(self, frame_idx):
        """Public wrapper around _frame_data() -- for callers outside this class
        (dockwidget.py's path/focus/look-direction preview) that want the exact
        same per-frame math Preview/Export/Export Trajectory use, without
        reaching into a leading-underscore method."""
        return self._frame_data(frame_idx)

    def time_for_frame(self, frame_idx):
        """Public wrapper around _time_for_frame() -- see frame_pose() above."""
        return self._time_for_frame(frame_idx)

    def _frame_data(self, frame_idx):
        """(position, center, distance, pitch, yaw) for a frame -- shared by
        playback (_advance) and export_trajectory so both a generated curve and
        an imported file go through the exact same code from here on. position
        and center are always QgsVector3D in scene-local coordinates."""
        if self.imported_keyframes is not None:
            kf = self.imported_keyframes[frame_idx]
            position, center = kf["position"], kf["center"]
            # Re-derive from the map-CRS equivalents when the loaded file had
            # them (trajectory_io.py's load_json_trajectory/load_csv_with_mapping
            # -- absent for a genuinely scene-local-only CSV import, which has no
            # authoritative source to re-derive from and stays static). Same
            # staleness concern as generated curves' _live_focus() -- see
            # __init__'s comment.
            pos_map = kf.get("position_map")
            center_map = kf.get("center_map")
            if pos_map is not None and not _canvas_is_dead(self.canvas3d):
                try:
                    position = self.canvas3d.mapSettings().mapToWorldCoordinates(pos_map)
                    center = self.canvas3d.mapSettings().mapToWorldCoordinates(center_map)
                except Exception as exc:
                    # Same defensive fallback as _live_focus() above -- keep the
                    # scene-local position/center already unpacked from kf
                    # rather than crash, but log rather than swallow silently.
                    _log(f"_frame_data: mapToWorldCoordinates failed, using scene-local keyframe: {exc}", Qgis.Warning)
            distance, pitch, yaw = kf["distance"], kf["pitch"], kf["yaw"]
        else:
            t = frame_idx / (self.frame_count - 1)
            center, distance, pitch, yaw = self._pose_for(t)
            dx, dy, dz = self.curve_fn(t, **self.curve_kwargs)
            focus = self._live_focus()
            position = QgsVector3D(focus.x() + dx, focus.y() + dy, focus.z() + dz)

        if any(self._rotation_rad):
            # Applied uniformly here, after either branch above, rather than
            # baked into curve math or imported keyframes directly -- keeps
            # rotation correct for BOTH generated and imported trajectories
            # with one implementation, and correct even when the pivot
            # (_live_focus()) itself moves mid-session (see __init__'s comment
            # on origin drift) since it's re-fetched fresh every call.
            pivot = self._live_focus()
            position = self._rotate_point(position, pivot, self._rotation_rad)
            center = self._rotate_point(center, pivot, self._rotation_rad)
            distance, pitch, yaw = _offset_to_pose(
                position.x() - center.x(), position.y() - center.y(), position.z() - center.z()
            )

        return position, center, distance, pitch, yaw

    def _time_for_frame(self, frame_idx):
        if self.imported_keyframes is not None:
            return self.imported_keyframes[frame_idx]["time_s"]
        return frame_idx / self.fps

    def export_trajectory(self, output_dir):
        """Write the camera path as plain keyframe data (JSON + CSV) -- no canvas
        driving, no screen capture, doesn't touch the timer at all. This is what
        lets a move get validated with a cheap low-res render now and re-rendered
        later at final quality (or in another engine, e.g. Blender/Unreal) from
        the same file, instead of reshooting the camera path by hand.

        Positions/targets are in scene-local coordinates -- the same space
        setLookingAtPoint() consumes -- with scene_origin/project_crs recorded
        in the header so the file is self-describing enough to convert later.
        """
        os.makedirs(output_dir, exist_ok=True)

        # worldToMapCoordinates() is the exact inverse of the mapToWorldCoordinates()
        # call that put the focus point into scene space in the first place (see
        # dockwidget.py's "Center on point clouds" / "Pick on map") -- confirmed in
        # QGIS's src/3d/qgs3dmapsettings.h, public and bound to Python, not SIP_SKIP.
        # Without this, the file only carries scene-local numbers (origin-shifted --
        # a plain per-axis subtraction, no axis swap, confirmed against QGIS 3.44's
        # actual source) which don't line up with the point cloud's own CRS if you
        # load the CSV as a layer -- e.g. scene x=229.5 instead of the point cloud's
        # real easting like 337615.62.
        map_settings = None
        if not _canvas_is_dead(self.canvas3d):
            map_settings = self.canvas3d.mapSettings()

        keyframes = []
        for frame in range(self.frame_count):
            position, center, distance, pitch, yaw = self._frame_data(frame)
            kf = {
                "frame": frame,
                "time_s": self._time_for_frame(frame),
                "position": {"x": position.x(), "y": position.y(), "z": position.z()},
                "look_at": {"x": center.x(), "y": center.y(), "z": center.z()},
                "distance": distance,
                "pitch_deg": pitch,
                "yaw_deg": yaw,
            }
            if map_settings is not None:
                pos_map = map_settings.worldToMapCoordinates(position)
                look_map = map_settings.worldToMapCoordinates(center)
                kf["position_map"] = {"x": pos_map.x(), "y": pos_map.y(), "z": pos_map.z()}
                kf["look_at_map"] = {"x": look_map.x(), "y": look_map.y(), "z": look_map.z()}
            keyframes.append(kf)

        origin = None
        if map_settings is not None:
            o = map_settings.origin()
            origin = {"x": o.x(), "y": o.y(), "z": o.z()}

        # Live-derived (see _live_focus()), not the raw self.focus captured at
        # __init__ -- must be paired against scene_origin above, which is read
        # right now too; using the stale build-time focus here would mismatch a
        # scene_origin that's since moved (e.g. from the path-visualization's
        # own extent growth), making the pair internally inconsistent.
        live_focus = self._live_focus() if self.focus is not None else None

        payload = {
            "source": "imported" if self.imported_keyframes is not None else "generated",
            "curve": self.curve_name,
            "curve_params": self.curve_kwargs,
            "look_mode": self.look_mode,
            # Already baked into every keyframe below (rotation is applied in
            # _frame_data() before this loop runs) -- recorded here only so a
            # human/tool reading the file knows why a re-generated version of
            # the same curve_params wouldn't match without also re-applying it.
            # Applied in x, then y, then z order -- see rotation_x_deg's
            # docstring in __init__.
            "rotation_deg": {
                "x": math.degrees(self._rotation_rad[0]),
                "y": math.degrees(self._rotation_rad[1]),
                "z": math.degrees(self._rotation_rad[2]),
            },
            "focus_scene": (
                {"x": live_focus.x(), "y": live_focus.y(), "z": live_focus.z()}
                if live_focus is not None else None
            ),
            "scene_origin": origin,
            "project_crs": QgsProject.instance().crs().authid() or None,
            "coordinate_space": (
                "Each keyframe carries both: position/look_at are scene-local (same "
                "coordinates QgsCameraController.setLookingAtPoint() consumes -- origin-"
                "shifted, Y-up axis swap vs. the map CRS; portable to re-render at a "
                "different origin or in another engine). position_map/look_at_map are "
                "the same points converted back to the project CRS (project_crs above) "
                "via Qgs3DMapSettings.worldToMapCoordinates() -- these are what line up "
                "with the point cloud's own coordinates, e.g. for loading this file as a "
                "layer alongside it. position_map/look_at_map are omitted if no 3D canvas "
                "was available at export time."
            ),
            "fps": self.fps,
            "duration_s": self.duration_s,
            "frame_count": self.frame_count,
            "keyframes": keyframes,
        }

        json_path = os.path.join(output_dir, "trajectory.json")
        with open(json_path, "w") as f:
            json.dump(payload, f, indent=2)

        has_map = map_settings is not None
        csv_path = os.path.join(output_dir, "trajectory.csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            header = [
                "frame", "time_s", "pos_x", "pos_y", "pos_z",
                "look_x", "look_y", "look_z", "distance", "pitch_deg", "yaw_deg",
            ]
            if has_map:
                # map_x/y/z, look_map_x/y/z: same points in the project CRS -- these
                # are the columns to use if loading this CSV as a layer next to the
                # point cloud, not pos_x/pos_y/pos_z (scene-local, won't line up).
                header += ["map_x", "map_y", "map_z", "look_map_x", "look_map_y", "look_map_z"]
            writer.writerow(header)
            for kf in keyframes:
                row = [
                    kf["frame"], kf["time_s"],
                    kf["position"]["x"], kf["position"]["y"], kf["position"]["z"],
                    kf["look_at"]["x"], kf["look_at"]["y"], kf["look_at"]["z"],
                    kf["distance"], kf["pitch_deg"], kf["yaw_deg"],
                ]
                if has_map:
                    row += [
                        kf["position_map"]["x"], kf["position_map"]["y"], kf["position_map"]["z"],
                        kf["look_at_map"]["x"], kf["look_at_map"]["y"], kf["look_at_map"]["z"],
                    ]
                writer.writerow(row)

        _log(f"trajectory exported: {json_path}, {csv_path}")
        return json_path, csv_path

    def log_camera_debug(self, frame_idx, position, center, distance, pitch, yaw, controller):
        """Debug logging for "the marker shown doesn't match where the camera
        actually ends up": logs the "intended" camera position this class
        computed for frame_idx (position -- exactly what path_visualization.py
        samples for the points/shade-bucket markers) next to the
        (center, distance, pitch, yaw) pose sent to setLookingAtPoint(), and
        QGIS's own cameraPose() read back immediately after -- the closest
        available ground truth. There's no way to get further than this from
        Python: QgsCameraController.camera() (the actual Qt3D QCamera, which
        would have the real rendered eye position) is explicitly marked "Not
        available in Python bindings" in QGIS's own header, and confirmed
        absent from the auto-generated .sip.in too -- Qt3D types aren't bound
        anywhere in PyQGIS.

        If the readback doesn't exactly match what was just sent, QGIS is
        silently clamping/adjusting one of the 4 values (a pitch or distance
        limit is the likely suspect) -- that alone would explain a
        marker/camera mismatch, since the SAME position->pose conversion
        feeds both the marker draw and this call. If the readback DOES match
        exactly, the mismatch isn't in this class's math or in QGIS rejecting
        our values -- it's in how QGIS's camera itself turns a matching
        center/distance/pitch/yaw into a rendered eye position (a convention
        this plugin doesn't control and can't independently verify from
        Python), which would need a different kind of investigation.
        """
        pos_map = self._to_map(position)
        msg = (
            f"[camera-debug] frame {frame_idx}: intended position(scene)={position}"
            + (f" position(map)={pos_map}" if pos_map is not None else "")
            + f" | sent to setLookingAtPoint: center={center} distance={distance:.3f} "
            f"pitch={pitch:.3f} yaw={yaw:.3f}"
        )
        try:
            pose = controller.cameraPose()
            msg += (
                f" | cameraPose() readback: center={pose.centerPoint()} "
                f"distance={pose.distanceFromCenterPoint():.3f} "
                f"pitch={pose.pitchAngle():.3f} yaw={pose.headingAngle():.3f}"
            )
        except Exception as exc:
            msg += f" | cameraPose() readback failed: {exc}"
        _log(msg)

    def _advance(self):
        # The 3D view (its dock, or the whole canvas) can be closed by the user
        # mid-run since this chains across many frames/seconds -- stop instead
        # of crashing on the dangling C++ object.
        if _canvas_is_dead(self.canvas3d):
            self.stop()
            return

        if self.frame >= self.frame_count:
            # Stitch BEFORE stop() -- stop() fires on_finished synchronously, and
            # dockwidget.py's export on_finished callback refreshes the Export
            # tab's "previously exported videos" list, which only shows runs
            # that already have a camera_path.mp4 on disk. Calling stop() first
            # would fire that refresh before ffmpeg has actually written the
            # file, silently dropping this run from the list until some later,
            # unrelated refresh happened to run.
            if self.export:
                self._stitch_video()
            self.stop()
            return

        position, center, distance, pitch, yaw = self._frame_data(self.frame)

        controller = self.canvas3d.cameraController()
        if self.frame == 0:
            _log(
                f"frame 0: look_mode={self.look_mode} center={center} "
                f"distance={distance:.2f} pitch={pitch:.2f} yaw={yaw:.2f} "
                f"cameraController={'None -- scene not initialized yet' if controller is None else 'ok'}"
            )
        if controller is None:
            self.stop()
            return
        controller.setLookingAtPoint(center, distance, pitch, yaw)
        # See log_camera_debug()'s docstring -- compares the position this class
        # intended for this frame (what the preview markers draw) against what
        # was actually sent/read back from QGIS's own camera controller.
        self.log_camera_debug(self.frame, position, center, distance, pitch, yaw, controller)

        if self.export:
            # ponytail: no render-complete signal is exposed for this canvas, so this
            # is a best-effort nudge, not a guarantee -- grabWindow() can still catch
            # the previous frame on a slow/heavy scene. Add a QTimer delay before the
            # grab if exported frames show stale camera poses.
            QCoreApplication.processEvents()
            path = os.path.join(self.frames_dir, f"frame_{self.frame:05d}.png")
            screen = QGuiApplication.primaryScreen()
            # grabWindow(canvas3d.winId()) doesn't reliably clip to just this
            # embedded/docked window's own pixels. Grabbing window=0 (the whole
            # desktop) with an explicit rectangle at the canvas's real global
            # position is a plain screen-region grab -- it doesn't depend on
            # window-embedding semantics at all, so there's nothing to crop after.
            top_left = self.canvas3d.mapToGlobal(QPoint(0, 0))
            w, h = self.canvas3d.width(), self.canvas3d.height()
            if self.frame == 0:
                _log(
                    f"capture region: top_left=({top_left.x()},{top_left.y()}) size={w}x{h} "
                    f"screen={screen.name()} devicePixelRatio={screen.devicePixelRatio()}"
                )
            screen.grabWindow(0, top_left.x(), top_left.y(), w, h).save(path, "PNG")

        if self.on_frame:
            self.on_frame(self.frame, self.frame_count)

        self.frame += 1

    def _stitch_video(self):
        if not self.make_video:
            _log(f"export done: {self.frame_count} frames saved to {self.frames_dir}")
            return

        ffmpeg_path = _find_ffmpeg()
        if ffmpeg_path is None:
            _log(
                "ffmpeg not found -- frames were saved but no video was created. "
                "Install ffmpeg (e.g. `brew install ffmpeg`) and click Export again.",
                level=Qgis.Warning,
            )
            return

        out_path = os.path.join(self.output_dir, "camera_path.mp4")
        # ffmpeg_path is never raw user text: _find_ffmpeg() only returns an
        # absolute path resolved via shutil.which() (searches PATH for an
        # executable actually named "ffmpeg") or one of the hardcoded
        # _FFMPEG_FALLBACK_PATHS, each checked with os.path.isfile() first.
        # Every other argument is either a constant flag or built internally
        # (str(self.fps), os.path.join(...)) -- nothing here is shell-
        # interpreted (no shell=True) or constructed from untrusted input.
        try:
            subprocess.run(  # nosec B603 -- see comment above
                [
                    ffmpeg_path, "-y",
                    "-framerate", str(self.fps),
                    "-i", os.path.join(self.frames_dir, "frame_%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    out_path,
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            _log(f"ffmpeg failed to stitch the video:\n{stderr[-1000:]}", level=Qgis.Critical)
            return

        _log(f"video saved to {out_path}")
