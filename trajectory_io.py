"""Loads a trajectory file back into a per-frame keyframe list the animator can
play back directly, instead of computing positions from curve math.

Two file types, handled differently:

- .json: this plugin's own trajectory.json (the "keyframes" list from
  animator.py's export_trajectory()). Fixed, well-known schema -- loaded
  directly via load_json_trajectory(), no user input needed. Prefers each
  keyframe's position_map/look_at_map (project-CRS) over its position/look_at
  (scene-local) -- scene-local coordinates are only valid in the exact 3D
  scene they were computed against (origin can drift between sessions -- see
  dockwidget.py's _get_canvas3d), so re-deriving scene coordinates fresh from
  map coordinates at load time is what makes re-importing reliable.

- .csv: column names are never guaranteed -- this plugin's own exports use
  map_x/pos_x-style names, but a file from another tool could use anything
  (X/Y/Z, tx/ty/tz, camera_x, ...). Column mapping is therefore a three-step,
  UI-visible process rather than a silent guess:
    1. csv_headers(path) -- just the header row.
    2. suggest_csv_mapping(headers) -- best-guess field -> column name (and a
       guess at whether the coordinates are project-CRS or this project's
       3D-scene-local space), meant to pre-fill an editable form, never to be
       trusted blindly.
    3. load_csv_with_mapping(path, mapping, coord_space, ...) -- the mapping
       the user confirmed (or corrected) in that form, applied to every row.
  Required fields: x, y, z (camera position), look_x, look_y, look_z (look-at
  target). A look-at target is required rather than a raw rotation (pitch/yaw/
  roll) because rotation conventions differ enough between tools (axis order,
  degrees vs radians, handedness) that guessing wrong would point the camera
  off with no obvious symptom -- a look-at point is unambiguous regardless of
  source tool. Optional: frame, time_s (row order + an assumed fps is used if
  neither is mapped).
"""
import csv
import json

from qgis.core import QgsVector3D

from .animator import _offset_to_pose


class TrajectoryLoadError(Exception):
    """Raised for anything wrong with the file or the confirmed mapping (missing
    columns, bad JSON, empty file, unparseable cell, ...) -- always human-
    readable, meant to be shown as-is in the dock widget's status label."""


CSV_REQUIRED_FIELDS = ["x", "y", "z", "look_x", "look_y", "look_z"]
CSV_OPTIONAL_FIELDS = ["frame", "time_s"]
CSV_ALL_FIELDS = CSV_REQUIRED_FIELDS + CSV_OPTIONAL_FIELDS

FIELD_LABELS = {
    "x": "Position X", "y": "Position Y", "z": "Position Z",
    "look_x": "Look-at X", "look_y": "Look-at Y", "look_z": "Look-at Z",
    "frame": "Frame (optional)", "time_s": "Time, seconds (optional)",
}

# Alphabetically-first-match wins is deliberately not the strategy here -- order
# within each list is preference order (this plugin's own column names first),
# checked case-insensitively against the file's actual headers.
_ALIASES = {
    "x": ["map_x", "pos_x", "x", "position_x", "cam_x", "camera_x", "tx", "easting"],
    "y": ["map_y", "pos_y", "y", "position_y", "cam_y", "camera_y", "ty", "northing"],
    "z": ["map_z", "pos_z", "z", "position_z", "cam_z", "camera_z", "tz", "elevation", "altitude", "height"],
    "look_x": ["look_map_x", "look_x", "target_x", "lookat_x", "look_at_x"],
    "look_y": ["look_map_y", "look_y", "target_y", "lookat_y", "look_at_y"],
    "look_z": ["look_map_z", "look_z", "target_z", "lookat_z", "look_at_z"],
    "frame": ["frame", "frame_idx", "frame_index", "index", "idx"],
    "time_s": ["time_s", "time", "timestamp", "t", "time_sec", "time_seconds"],
}

# Only this plugin's own scene-local column name (pos_x, written when no 3D
# canvas was available at export time) implies "already scene-local" -- every
# other match, including this plugin's own map_x, is assumed to be project-CRS,
# since that's what every external tool would export.
_SCENE_LOCAL_HINTS = {"pos_x", "pos_y", "pos_z"}


def _load_json(path):
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise TrajectoryLoadError(f"not valid JSON: {exc}") from exc
    if not isinstance(data.get("keyframes"), list):
        raise TrajectoryLoadError("JSON has no 'keyframes' list -- not a trajectory this plugin exported")
    return data


_SCENE_MISMATCH_WARNING = (
    "file has no map-CRS coordinates -- using its scene-local coordinates as-is, "
    "which only line up correctly if this project's 3D scene origin hasn't changed "
    "since the file was written"
)


def load_json_trajectory(path, map_settings, assumed_fps=30.0):
    """Returns (keyframes, effective_fps, warnings). No user-facing mapping step
    -- this plugin's own export schema is fixed and self-describing."""
    data = _load_json(path)

    warnings = []
    keyframes = []
    for i, row in enumerate(data["keyframes"]):
        try:
            pos_map, look_map = row.get("position_map"), row.get("look_at_map")
            position_map = center_map = None
            if pos_map and look_map and map_settings is not None:
                position_map = QgsVector3D(pos_map["x"], pos_map["y"], pos_map["z"])
                center_map = QgsVector3D(look_map["x"], look_map["y"], look_map["z"])
                position = map_settings.mapToWorldCoordinates(position_map)
                center = map_settings.mapToWorldCoordinates(center_map)
            else:
                p, l = row["position"], row["look_at"]
                position = QgsVector3D(p["x"], p["y"], p["z"])
                center = QgsVector3D(l["x"], l["y"], l["z"])
                if _SCENE_MISMATCH_WARNING not in warnings:
                    warnings.append(_SCENE_MISMATCH_WARNING)
            kf = _build_keyframe(
                position, center, row.get("time_s"), row.get("frame", i), assumed_fps,
                position_map=position_map, center_map=center_map,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TrajectoryLoadError(f"keyframe {i}: {exc}") from exc
        keyframes.append(kf)

    keyframes = _finalize(keyframes)
    fps = _estimate_fps(keyframes, data.get("fps"), assumed_fps)
    return keyframes, fps, warnings


def csv_headers(path):
    """Just the header row -- cheap, used to build the mapping form before
    committing to parsing every row."""
    with open(path, newline="") as f:
        return next(csv.reader(f), [])


def suggest_csv_mapping(headers):
    """Best-guess field -> column name for each of CSV_ALL_FIELDS, plus a guess
    at coordinate space ("map" or "scene"). A starting point for an editable
    mapping form -- never applied without the user seeing and confirming it.
    """
    lookup = {h.strip().lower(): h for h in headers}
    mapping = {}
    for field, aliases in _ALIASES.items():
        mapping[field] = next((lookup[alias] for alias in aliases if alias in lookup), None)

    x_col = mapping.get("x")
    coord_space = "scene" if x_col and x_col.strip().lower() in _SCENE_LOCAL_HINTS else "map"
    return mapping, coord_space


def load_csv_with_mapping(path, mapping, coord_space, map_settings, assumed_fps=30.0):
    """mapping: dict field -> column name (or None), covering CSV_ALL_FIELDS.
    All of CSV_REQUIRED_FIELDS must be mapped; CSV_OPTIONAL_FIELDS may be None.
    coord_space: "map" (mapping's x/y/z/look_* columns are project-CRS,
    converted via mapToWorldCoordinates) or "scene" (already this project's
    3D-scene-local space, used as-is).
    """
    missing = [f for f in CSV_REQUIRED_FIELDS if not mapping.get(f)]
    if missing:
        labels = ", ".join(FIELD_LABELS[f] for f in missing)
        raise TrajectoryLoadError(f"no column mapped for: {labels}")
    if coord_space not in ("map", "scene"):
        raise TrajectoryLoadError(f"unknown coordinate space '{coord_space}'")
    if coord_space == "map" and map_settings is None:
        raise TrajectoryLoadError(
            "coordinates are set to project CRS but no 3D scene is open to convert "
            "them -- open the 3D Map View first, or switch to Scene-local if these "
            "coordinates are already in that space"
        )

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    keyframes = []
    for i, row in enumerate(rows):
        try:
            raw_pos = QgsVector3D(
                float(row[mapping["x"]]), float(row[mapping["y"]]), float(row[mapping["z"]])
            )
            raw_look = QgsVector3D(
                float(row[mapping["look_x"]]), float(row[mapping["look_y"]]), float(row[mapping["look_z"]])
            )
            if coord_space == "map":
                position = map_settings.mapToWorldCoordinates(raw_pos)
                center = map_settings.mapToWorldCoordinates(raw_look)
                position_map, center_map = raw_pos, raw_look
            else:
                # Genuinely scene-local input (no map-CRS source at all) -- there's
                # nothing to re-derive from later if the scene origin moves, so
                # this keyframe stays static (see _SCENE_MISMATCH_WARNING's cousin
                # in load_json_trajectory -- same inherent limitation here).
                position, center = raw_pos, raw_look
                position_map = center_map = None

            time_col = mapping.get("time_s")
            frame_col = mapping.get("frame")
            time_value = row.get(time_col) if time_col else None
            frame_value = row.get(frame_col) if frame_col else None
            frame_idx = i if frame_value in (None, "") else float(frame_value)
            kf = _build_keyframe(
                position, center, time_value, frame_idx, assumed_fps,
                position_map=position_map, center_map=center_map,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TrajectoryLoadError(f"row {i}: {exc}") from exc
        keyframes.append(kf)

    keyframes = _finalize(keyframes)
    fps = _estimate_fps(keyframes, None, assumed_fps)
    return keyframes, fps, []


def _build_keyframe(position, center, time_value, frame_idx, assumed_fps, position_map=None, center_map=None):
    distance, pitch, yaw = _offset_to_pose(
        position.x() - center.x(), position.y() - center.y(), position.z() - center.z()
    )
    if time_value in (None, ""):
        idx = 0 if frame_idx is None else frame_idx
        time_s = idx / assumed_fps
    else:
        time_s = float(time_value)
    return {
        "time_s": time_s,
        "position": position,
        "center": center,
        "distance": distance,
        "pitch": pitch,
        "yaw": yaw,
        # Map-CRS equivalents, when the source had them (None otherwise -- see
        # callers). Lets CameraPathAnimator re-derive position/center fresh each
        # frame instead of trusting the scene-local values above forever, which
        # go stale if the 3D scene's origin moves after this file was loaded
        # (Qgs3DMapSettings.setExtent() recenters origin unconditionally; our
        # own path-visualization code calls it to avoid clipping a wide path).
        "position_map": position_map,
        "center_map": center_map,
    }


def _finalize(keyframes):
    if not keyframes:
        raise TrajectoryLoadError("file contained no usable keyframes")
    keyframes.sort(key=lambda k: k["time_s"])
    return keyframes


def _estimate_fps(keyframes, fps_hint, assumed_fps):
    """Playback uses one fixed QTimer interval (see CameraPathAnimator.start()),
    not a per-frame variable delay -- so for irregularly-spaced keyframes this is
    an average, not exact timing.
    """
    if fps_hint:
        return float(fps_hint)
    if len(keyframes) < 2:
        return assumed_fps
    deltas = sorted(b["time_s"] - a["time_s"] for a, b in zip(keyframes, keyframes[1:]))
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return assumed_fps
    return 1.0 / deltas[len(deltas) // 2]
