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
    2. suggest_csv_mapping(headers) -- best-guess field -> column name, meant
       to pre-fill an editable form, never to be trusted blindly.
    3. load_csv_with_mapping(path, mapping, map_settings, ..., angle_convention,
       orientation_source) -- the mapping the user confirmed (or corrected)
       in that form, applied to every row.
  Position columns (x, y, z, and look_x/y/z if used) are always project-CRS
  -- this plugin only ever works in one coordinate system in its UI (map/
  project CRS, the same one the 2D map uses); there is no scene-local input
  option to get wrong. (This plugin's own CSV export can, in one narrow edge
  case -- no 3D view open at export time -- write a file with only
  scene-local pos_x/y/z columns and no map_x/y/z; such a file has no
  project-CRS data to import at all and isn't supported here as a
  consequence, same as it was never supported to type scene-local numbers
  into the Focus Point fields either.)
  Required fields: x, y, z (camera position, always). Orientation then comes
  from EXACTLY ONE of two sources, chosen explicitly via orientation_source
  (never inferred from which fields happen to be mapped -- picking the wrong
  one silently is exactly the failure mode this plugin's mapping UI exists to
  prevent):
    - "lookat": look_x, look_y, look_z, a look-at target. Preferred and
      unambiguous regardless of source tool -- this is why the plugin has
      always supported this route, and remains the default.
    - "angles": pitch, yaw (optionally roll), camera orientation angles
      directly. This exists because some tools only ever export orientation,
      never a look-at point. The risk flagged here previously stands:
      rotation conventions differ between tools (axis order, degrees vs
      radians, handedness, where pitch=0 points), and guessing wrong points
      the camera off with no obvious symptom. Rather than guess, the mapping
      form makes the assumed convention an explicit, visible choice
      (angle_convention: "qgis", matching this plugin's own pitch/yaw
      exactly, or "aviation", a gimbal/drone-style convention where pitch=0
      is level and -90 is straight down -- see load_csv_with_mapping()'s
      docstring for the exact formula each implies). roll can never be
      represented -- QGIS's look-at camera has no roll degree of freedom --
      so a mapped roll column is only used to raise a warning that it was
      read but ignored, never applied.
  Optional: frame, time_s (row order + an assumed fps is used if neither is
  mapped).
"""
import csv
import json

from qgis.core import QgsVector3D

from .animator import _offset_to_pose, _pose_to_offset


class TrajectoryLoadError(Exception):
    """Raised for anything wrong with the file or the confirmed mapping (missing
    columns, bad JSON, empty file, unparseable cell, ...) -- always human-
    readable, meant to be shown as-is in the dock widget's status label."""


CSV_POSITION_FIELDS = ["x", "y", "z"]
CSV_LOOKAT_FIELDS = ["look_x", "look_y", "look_z"]
CSV_ORIENTATION_FIELDS = ["pitch", "yaw"]
# roll is kept separate from CSV_ORIENTATION_FIELDS (never required, never
# part of the either/or check below) but grouped with it everywhere else --
# ordering here, and the show/hide toggle in dockwidget.py's
# _on_csv_orientation_source_changed() -- since it's only ever meaningful
# alongside pitch/yaw, not alongside a look-at target or the generic
# frame/time_s fields below.
CSV_ORIENTATION_OPTIONAL_FIELDS = ["roll"]
# CSV_REQUIRED_FIELDS kept for backwards compatibility (just position -- the
# unconditionally-required subset). See load_csv_with_mapping() for the
# either/or check between CSV_LOOKAT_FIELDS and CSV_ORIENTATION_FIELDS.
CSV_REQUIRED_FIELDS = CSV_POSITION_FIELDS
# Generic, orientation-independent optional fields -- deliberately ordered
# after all orientation-related fields (look-at, pitch/yaw, roll) rather than
# interleaved with them, so the mapping form groups "how the camera is
# oriented" together and "which row is which frame/timestamp" as a clearly
# separate, later group.
CSV_OPTIONAL_FIELDS = ["frame", "time_s"]
CSV_ALL_FIELDS = (
    CSV_POSITION_FIELDS + CSV_LOOKAT_FIELDS + CSV_ORIENTATION_FIELDS
    + CSV_ORIENTATION_OPTIONAL_FIELDS + CSV_OPTIONAL_FIELDS
)

FIELD_LABELS = {
    "x": "Position X", "y": "Position Y", "z": "Position Z",
    "look_x": "Look-at X", "look_y": "Look-at Y", "look_z": "Look-at Z",
    "pitch": "Pitch", "yaw": "Yaw / Heading",
    "roll": "Roll (optional -- cannot be represented, dropped with a warning)",
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
    "pitch": ["pitch", "cam_pitch", "camera_pitch", "gimbal_pitch", "tilt"],
    "yaw": ["yaw", "cam_yaw", "camera_yaw", "heading", "cam_heading", "gimbal_yaw", "bearing"],
    "roll": ["roll", "cam_roll", "camera_roll", "gimbal_roll", "bank"],
    "frame": ["frame", "frame_idx", "frame_index", "index", "idx"],
    "time_s": ["time_s", "time", "timestamp", "t", "time_sec", "time_seconds"],
}

# angle_convention values accepted by load_csv_with_mapping() below. The
# user-facing label -> value mapping (with the exact numeric meaning spelled
# out) lives in dockwidget.py's _ANGLE_CONVENTIONS -- kept there rather than
# here so this module only ever deals in the plain value.
ANGLE_CONVENTION_VALUES = ("qgis", "aviation")
DEFAULT_ANGLE_CONVENTION = "qgis"

# orientation_source values accepted by load_csv_with_mapping() below -- an
# explicit choice between the two mutually-exclusive orientation inputs
# (CSV_LOOKAT_FIELDS vs CSV_ORIENTATION_FIELDS), never inferred from which
# fields happen to be mapped (see load_csv_with_mapping()'s docstring for
# why). Label -> value mapping lives in dockwidget.py's
# _ORIENTATION_SOURCES, same pattern as ANGLE_CONVENTION_VALUES above.
ORIENTATION_SOURCE_VALUES = ("lookat", "angles")
DEFAULT_ORIENTATION_SOURCE = "lookat"


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
    """Best-guess field -> column name for each of CSV_ALL_FIELDS. A starting
    point for an editable mapping form -- never applied without the user
    seeing and confirming it.

    Does NOT guess angle_convention (see load_csv_with_mapping()) -- callers
    should default their UI to DEFAULT_ANGLE_CONVENTION and require the user
    to actually look at and confirm it, same reasoning as the module
    docstring's note on why rotation conventions aren't auto-detected.
    """
    lookup = {h.strip().lower(): h for h in headers}
    mapping = {}
    for field, aliases in _ALIASES.items():
        mapping[field] = next((lookup[alias] for alias in aliases if alias in lookup), None)
    return mapping


def load_csv_with_mapping(
    path, mapping, map_settings, assumed_fps=30.0,
    angle_convention=DEFAULT_ANGLE_CONVENTION, orientation_source=DEFAULT_ORIENTATION_SOURCE,
):
    """mapping: dict field -> column name (or None), covering CSV_ALL_FIELDS.
    CSV_POSITION_FIELDS (x/y/z) must always be mapped. Position columns
    (x/y/z, look_x/y/z if used) are always project-CRS, converted via
    map_settings.mapToWorldCoordinates() -- this plugin has only one
    coordinate system anywhere in its UI (map/project CRS), so there's
    nothing to choose here; map_settings must not be None.

    orientation_source: an explicit, required choice of ORIENTATION_SOURCE_VALUES
    -- NOT inferred from which fields happen to be mapped. This mirrors
    angle_convention below: rather than silently preferring one source if a
    user maps both (or worse, mapping the wrong one without noticing), the
    caller states which one this file actually uses, and only that source's
    fields are read/required:
      - "lookat": requires all of CSV_LOOKAT_FIELDS (look_x/y/z) mapped.
        CSV_ORIENTATION_FIELDS are ignored even if mapped.
      - "angles": requires all of CSV_ORIENTATION_FIELDS (pitch/yaw) mapped.
        CSV_LOOKAT_FIELDS are ignored even if mapped. CSV_ORIENTATION_OPTIONAL_FIELDS
        (roll) may optionally also be mapped; it's read only to raise a
        warning that it was ignored, since QGIS's look-at camera has no roll
        degree of freedom to apply it to.
    CSV_ORIENTATION_OPTIONAL_FIELDS and CSV_OPTIONAL_FIELDS may always be
    left unmapped (None).

    angle_convention: only consulted when orientation_source is "angles".
    One of ANGLE_CONVENTION_VALUES:
      - "qgis": pitch/yaw are used exactly as QGIS's own convention defines
        them (see animator.py's _offset_to_pose() docstring) -- 0deg pitch is
        straight down, 90deg is level; no conversion applied.
      - "aviation": a gimbal/drone-style convention where pitch is measured
        from level (0deg=level, -90deg=straight down, +90deg=straight up) and
        yaw is treated as equivalent to heading. Converted via
        qgis_pitch = 90 + aviation_pitch (see _build_keyframe_from_orientation()).
        This does not attempt true-north correction -- heading is passed
        through as yaw unchanged, which only matches compass heading for
        CRSs where +Y points north, same as every other X/Y-is-just-planar-
        axes assumption already made elsewhere in this plugin.
    """
    missing = [f for f in CSV_POSITION_FIELDS if not mapping.get(f)]
    if missing:
        labels = ", ".join(FIELD_LABELS[f] for f in missing)
        raise TrajectoryLoadError(f"no column mapped for: {labels}")

    if orientation_source not in ORIENTATION_SOURCE_VALUES:
        raise TrajectoryLoadError(f"unknown orientation source '{orientation_source}'")
    has_lookat = orientation_source == "lookat"
    has_orientation = orientation_source == "angles"
    required_fields = CSV_LOOKAT_FIELDS if has_lookat else CSV_ORIENTATION_FIELDS
    missing_orientation = [f for f in required_fields if not mapping.get(f)]
    if missing_orientation:
        labels = ", ".join(FIELD_LABELS[f] for f in missing_orientation)
        source_label = "look-at target" if has_lookat else "orientation angles"
        raise TrajectoryLoadError(f"orientation source is set to {source_label}, but no column mapped for: {labels}")
    if angle_convention not in ANGLE_CONVENTION_VALUES:
        raise TrajectoryLoadError(f"unknown angle convention '{angle_convention}'")
    if map_settings is None:
        raise TrajectoryLoadError(
            "no 3D scene available to convert the CSV's map/project CRS "
            "coordinates -- open the 3D Map View first"
        )

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    warnings = []
    roll_col = mapping.get("roll") if has_orientation else None
    roll_warned = False
    keyframes = []
    for i, row in enumerate(rows):
        try:
            raw_pos = QgsVector3D(
                float(row[mapping["x"]]), float(row[mapping["y"]]), float(row[mapping["z"]])
            )
            position = map_settings.mapToWorldCoordinates(raw_pos)
            position_map = raw_pos

            time_col = mapping.get("time_s")
            frame_col = mapping.get("frame")
            time_value = row.get(time_col) if time_col else None
            frame_value = row.get(frame_col) if frame_col else None
            frame_idx = i if frame_value in (None, "") else float(frame_value)

            if has_lookat:
                raw_look = QgsVector3D(
                    float(row[mapping["look_x"]]), float(row[mapping["look_y"]]), float(row[mapping["look_z"]])
                )
                center = map_settings.mapToWorldCoordinates(raw_look)
                center_map = raw_look
                kf = _build_keyframe(
                    position, center, time_value, frame_idx, assumed_fps,
                    position_map=position_map, center_map=center_map,
                )
            else:
                pitch_raw = float(row[mapping["pitch"]])
                yaw_deg = float(row[mapping["yaw"]])
                pitch_deg = 90.0 + pitch_raw if angle_convention == "aviation" else pitch_raw
                if roll_col and row.get(roll_col) not in (None, "") and not roll_warned:
                    warnings.append(
                        "roll column is mapped but ignored -- QGIS's look-at camera "
                        "has no roll degree of freedom to apply it to"
                    )
                    roll_warned = True
                kf = _build_keyframe_from_orientation(
                    position, pitch_deg, yaw_deg, time_value, frame_idx, assumed_fps,
                    position_map=position_map,
                )
        except (KeyError, ValueError, TypeError) as exc:
            raise TrajectoryLoadError(f"row {i}: {exc}") from exc
        keyframes.append(kf)

    keyframes = _finalize(keyframes)
    fps = _estimate_fps(keyframes, None, assumed_fps)
    return keyframes, fps, warnings


def _build_keyframe_from_orientation(
    position, pitch_deg, yaw_deg, time_value, frame_idx, assumed_fps, position_map=None,
):
    """Builds a keyframe from position + orientation angles directly, instead
    of an explicit look-at point -- used by load_csv_with_mapping() when the
    mapping has pitch/yaw filled in instead of look_x/y/z.
    setLookingAtPoint() only knows how to aim *at* a point, so a synthetic
    "virtual" look-at target is placed 1 scene unit in front of the camera
    along the given orientation, via animator.py's _pose_to_offset() -- the
    same trick animator.py's _pose_for() uses for its "forward"/"sideways"
    look modes. The distance used here is arbitrary (there's no real target to
    measure one from) and affects only the synthetic center's position, never
    the resulting camera pose: _build_keyframe() immediately re-derives
    (distance, pitch, yaw) from (position - center), which by construction
    reproduces pitch_deg/yaw_deg exactly regardless of what distance was used
    to place the virtual center.
    """
    dx, dy, dz = _pose_to_offset(1.0, pitch_deg, yaw_deg)
    center = QgsVector3D(position.x() - dx, position.y() - dy, position.z() - dz)
    center_map = None
    if position_map is not None:
        center_map = QgsVector3D(position_map.x() - dx, position_map.y() - dy, position_map.z() - dz)
    return _build_keyframe(
        position, center, time_value, frame_idx, assumed_fps,
        position_map=position_map, center_map=center_map,
    )


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
