import math
import os
import re
import time
from datetime import datetime

# Registers Qgs3DMapCanvas's SIP type table. Without this, iface.createNewMapCanvas3D()
# / mapCanvases3D() crash QGIS instead of raising -- the 3D bindings live in a separate
# extension module that isn't loaded just by importing qgis.core/qgis.gui.
import qgis._3d  # noqa: F401

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsDoubleRange,
    QgsGeometry,
    QgsMessageLog,
    QgsPointCloudLayer,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSettings,
    QgsVector3D,
)
from qgis.gui import QgsMapToolEmitPoint, QgsMapToolPan
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPoint, QSize, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap, QPolygon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import trajectory_io
from .animator import CameraPathAnimator, _canvas_is_dead
from .curves import CURVES
from .path_visualization import PathVisualizer


def _log(msg):
    QgsMessageLog.logMessage(msg, "QuickMapCine", Qgis.Info)


def _is_elevation_layer(layer):
    """True for any layer this plugin can treat as an elevation source when
    auto-fitting the focus point/curve parameters or sampling a Pick-on-map
    click -- point clouds (always, same as before v0.2) or a raster DEM
    layer with Elevation explicitly enabled (Layer Properties > Elevation >
    Enable, "Represents elevation surface" -- the same flag QGIS's own 3D
    Terrain configuration uses to pick a DEM). Deliberately NOT any single-
    or few-band raster, since that would just as happily match an ordinary
    basemap or imagery layer -- isEnabled() is the one unambiguous signal
    that the user has actually designated this raster as elevation data.
    """
    if isinstance(layer, QgsPointCloudLayer):
        return True
    if isinstance(layer, QgsRasterLayer):
        props = layer.elevationProperties()
        return props is not None and props.isEnabled()
    return False


def _dem_band_number(raster_elevation_properties):
    """Which band represents elevation for a DEM-enabled raster layer.
    bandNumber() was only added in QGIS 3.38 (this plugin's floor is 3.36) --
    band 1 is the correct fallback for the overwhelmingly common case (a
    single-band DEM), so a missing bandNumber() just means an older QGIS,
    not an error."""
    try:
        return raster_elevation_properties.bandNumber()
    except AttributeError:
        return 1


class _HeadlessMapSettings3D:
    """Stand-in for Qgs3DMapSettings, used only so _update_path_visualization()
    can keep the 2D preview live-updating when no real 3D view is currently
    open, without ever creating one as a side effect (iface.createNewMapCanvas3D()
    always shows a real dock -- there's no "create but stay hidden" option, so a
    real canvas was never an option here).

    Confirmed against QGIS 3.44's actual source (src/3d/qgs3dutils.cpp,
    Qgs3DUtils::mapToWorldCoordinates()/worldToMapCoordinates(), which
    Qgs3DMapSettings' own methods of the same name just delegate to) that this
    conversion is a plain per-axis origin subtraction/addition -- no axis
    permutation, no CRS reprojection. An older comment elsewhere in this plugin
    claimed a "Y-up axis swap"; that turned out to be stale, describing a
    pre-refactor QGIS internal convention still readable via a legacy project-
    file compat path (QgsCameraController::readXml's old x/y/elev fallback),
    not current behavior.

    Since it's pure origin math, and this plugin only ever uses the conversion
    in round-trips that apply the SAME origin going in and coming back out
    (focus -> scene, add a curve offset, straight back to map -- see
    CameraPathAnimator._pose_for/_frame_data), the origin term always cancels
    out algebraically regardless of its value. That means any fixed origin,
    including (0, 0, 0) here, reproduces exactly the same final map-CRS
    positions a real, live-canvas-derived Qgs3DMapSettings would -- there's
    nothing QGIS-specific left to replicate for this to be correct.

    Deliberately has no extent()/setExtent() -- path_visualization.py's
    _grow_scene_extent() already treats AttributeError there as "not a real
    3D scene, nothing to grow," which is exactly true here.
    """

    def __init__(self):
        self._origin = QgsVector3D(0.0, 0.0, 0.0)

    def origin(self):
        return self._origin

    def mapToWorldCoordinates(self, map_coords):
        return QgsVector3D(
            map_coords.x() - self._origin.x(),
            map_coords.y() - self._origin.y(),
            map_coords.z() - self._origin.z(),
        )

    def worldToMapCoordinates(self, world_coords):
        return QgsVector3D(
            world_coords.x() + self._origin.x(),
            world_coords.y() + self._origin.y(),
            world_coords.z() + self._origin.z(),
        )


class _HeadlessCanvas3D:
    """Plain-Python stand-in for a Qgs3DMapCanvas -- never a real window,
    never shown, nothing for the user to close. Exists only so
    _build_animator()/CameraPathAnimator can be reused unchanged for the
    "no live 3D view" 2D-preview fallback (see _update_path_visualization()
    and _HeadlessMapSettings3D above). Deliberately not a QObject/SIP-wrapped
    type, so it can never itself go "dangling" the way a real Qgs3DMapCanvas
    does when its dock is closed -- animator.py's _canvas_is_dead() treats
    exactly this case (a non-SIP object) as always-alive, which is correct
    here since it's just a normal Python object with normal Python lifetime.
    """

    def __init__(self):
        self._settings = _HeadlessMapSettings3D()

    def mapSettings(self):
        return self._settings


_LOOK_MODES = {
    "Focus on point": "focus",
    "Forward (direction of travel)": "forward",
    "Sideways": "sideways",
}

# Label shown in the mapping form's coordinate-space combo -> trajectory_io's
# coord_space value.
_COORD_SPACES = {
    "Map / project CRS": "map",
    "Scene-local (this project's 3D view)": "scene",
}
# Label shown in the mapping form's angle-convention combo -> trajectory_io's
# angle_convention value. Only consulted when Pitch/Yaw are mapped instead of
# a look-at target -- see trajectory_io.load_csv_with_mapping()'s docstring.
# Deliberately not auto-guessed (see suggest_csv_mapping()'s docstring); the
# exact numeric meaning is spelled out in the label itself so picking the
# wrong one is a visible choice, not a silent one.
_ANGLE_CONVENTIONS = {
    "QGIS (pitch: 0°=straight down, 90°=level)": "qgis",
    "Aviation / gimbal (pitch: 0°=level, -90°=straight down)": "aviation",
}
# Label shown in the mapping form's orientation-source combo -> trajectory_io's
# orientation_source value. An explicit either/or choice, not a guess -- only
# the fields for the selected source are shown/required (see
# _on_csv_orientation_source_changed()); the other source's fields are hidden
# entirely rather than left visible-but-ignorable, so there's no way to map
# both by accident and wonder which one actually took effect.
_ORIENTATION_SOURCES = {
    "Look-at target (X/Y/Z)": "lookat",
    "Orientation angles (Pitch/Yaw)": "angles",
}
_NO_COLUMN = "(none)"

# Placeholder/first item in the Trajectory tab's "previously exported" combo --
# selecting it is a no-op (see _on_trajectory_list_selected), so it doesn't
# fire an unwanted load the instant the combo is repopulated.
_NO_TRAJECTORY_SELECTED = "(choose a previously exported trajectory)"

# QgsSettings key the Project folder is persisted under -- application-level
# (QGIS profile), not per-.qgz-project, so it's remembered across QGIS
# restarts and plugin reloads regardless of which project file is open. A
# fresh CameraPathDockWidget (created new every time the plugin loads --
# see plugin.py's toggle_dock()) otherwise starts with an empty project_dir,
# which meant previously exported trajectories/videos were still on disk but
# silently stopped showing up in their lists the moment QGIS restarted.
_SETTINGS_PROJECT_DIR_KEY = "QuickMapCine/project_dir"

# curve name (CURVES key) -> diagrams/*.png, generated by generate_diagrams.py from
# the real curves.py math. Labels each curve's spinbox parameters (radius, height,
# turns, ...) against a picture of the actual shape instead of leaving users to
# guess what "tube_radius" or "freq_y" does from the name alone.
_DIAGRAM_FILES = {
    "Helix": "helix.png",
    "Lissajous": "lissajous.png",
    "Torus Knot": "torus_knot.png",
    "Trefoil Knot": "trefoil_knot.png",
    "Fly Through": "fly_through.png",
}
_DIAGRAM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams")
_DIAGRAM_WIDTH = 170  # half the row now (param form takes the other half), not the full panel


def _spinbox(default):
    box = QDoubleSpinBox()
    box.setRange(-1e9, 1e9)
    box.setDecimals(2)
    box.setValue(default)
    return box


# Drawn programmatically with QPainter rather than loaded from a resource
# path -- guarantees these actually render on every platform/theme without
# depending on a specific QGIS icon filename existing (unverifiable from
# here without a live QGIS install to check against). Small, self-contained,
# no external file to ship or go stale.
def _crosshair_icon(size=18):
    """Target/crosshair -- used for "Center on point clouds"."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#2c3e50"))
    pen.setWidth(2)
    painter.setPen(pen)
    margin = 2
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    mid = size // 2
    painter.drawLine(mid, 0, mid, size)
    painter.drawLine(0, mid, size, mid)
    painter.end()
    return QIcon(pixmap)


def _pin_icon(size=18):
    """Map pin/teardrop -- used for "Pick on map"."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#922b21"), 1))
    painter.setBrush(QColor("#e74c3c"))
    painter.drawEllipse(2, 1, size - 4, size - 7)
    tip = QPolygon([
        QPoint(size // 2 - 4, size - 7),
        QPoint(size // 2 + 4, size - 7),
        QPoint(size // 2, size - 1),
    ])
    painter.drawPolygon(tip)
    painter.setPen(QPen(QColor("#ffffff"), 0))
    painter.setBrush(QColor("#ffffff"))
    r = max(size // 7, 2)
    painter.drawEllipse(size // 2 - r, size // 2 - r - 2, 2 * r, 2 * r)
    painter.end()
    return QIcon(pixmap)


def _icon_button(icon, tooltip):
    """Compact square icon-only button -- used for Center on point clouds /
    Pick on map so they can sit inline with the focus x/y/z spinboxes.
    Long text labels here (the previous "Center on point clouds" button)
    don't wrap and were the original reason this whole panel couldn't be
    resized narrower -- see show_path_checkbox's comment for the same issue
    solved the same way (short visible content, detail in the tooltip)."""
    btn = QPushButton()
    btn.setIcon(icon)
    btn.setIconSize(QSize(16, 16))
    btn.setFixedSize(28, 28)
    btn.setToolTip(tooltip)
    return btn


class CameraPathDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("QuickMapCine", parent)
        self.iface = iface
        self.animator = None
        self._run_start_time = None  # set when Preview/Export starts; read by _on_run_frame
        self.canvas3d = None
        self.param_boxes = {}  # kwarg name -> QDoubleSpinBox, rebuilt per curve
        self.param_roles = {}  # kwarg name -> "radius" / "height" / None, rebuilt per curve
        # The authoritative focus point, in map/project CRS -- kept in sync with
        # focus_x/y/z (scene-local, what the UI shows/edits) any time the user
        # sets a new focus, by _refresh_focus_map()/_center_on_point_clouds()/
        # _on_map_point_picked(). Needed because the 3D scene's origin can move
        # after the user picks a focus (path_visualization.py's own extent-
        # growing fix calls Qgs3DMapSettings.setExtent(), which unconditionally
        # recenters origin -- confirmed in QGIS's source) -- if _build_animator()
        # re-read focus_x/y/z directly at build time and reinterpreted those
        # same numbers against whatever origin is *then* current, the resulting
        # scene-local point would silently mean a different real-world location
        # than the one the user actually picked. Re-deriving scene-local from
        # this map-CRS point at build time, using the origin current *then*,
        # keeps every animator pointed at the real spot regardless of how many
        # times origin has shifted in between. See _build_animator().
        self._focus_map = None
        # Set by _load_json_file()/_load_csv_with_current_mapping(); read by
        # _build_animator() when the source combo is on "Import trajectory file".
        self._imported_keyframes = None
        self._imported_fps = None
        self._imported_path = None
        # Path/camera-position/look-direction/focus preview -- real (temporary,
        # in-memory) vector layers rather than QgsRubberBand, so they can show
        # in the 3D view too, not just the 2D map canvas. That means they *can*
        # end up in exported frames if left visible during an export run --
        # unlike a rubber band, which is 2D-canvas-only and can never appear in
        # the 3D view at all. _export() below hides them (path_viz.set_visible)
        # for the run's duration and restores afterward via CameraPathAnimator's
        # on_finished callback. See path_visualization.py for the rest.
        self.path_viz = PathVisualizer()

        # Created once and held as an attribute -- a locally-scoped QgsMapTool gets
        # garbage collected as soon as the function that made it returns, silently
        # breaking the pick.
        self.pick_tool = QgsMapToolEmitPoint(self.iface.mapCanvas())
        self.pick_tool.canvasClicked.connect(self._on_map_point_picked)
        # "Pick on map" is one-shot: after a single click it always hands control
        # back to Pan, not whatever tool happened to be active before. Restoring
        # "whatever was active before" was the previous behavior, but if the user
        # pressed "Pick on map" twice in a row without ever clicking the canvas in
        # between, the "previous" tool was already pick_tool itself, so it never
        # actually left pick mode. A dedicated pan tool, held as an attribute for
        # the same GC reason as pick_tool, sidesteps that entirely.
        self.pan_tool = QgsMapToolPan(self.iface.mapCanvas())

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # Three tabs: "Project" holds the project folder (the shared home for
        # every exported trajectory/video from here on) plus Duration/FPS;
        # "Trajectory" is everything about defining the path itself (source,
        # curve/import, focus, rotation); "Export" is everything about turning
        # that trajectory into output (output folder override, the actual run
        # buttons, and run-status feedback). All three tabs' widgets stay
        # alive and readable regardless of which tab is showing -- only the
        # layout changed here, not which fields feed _build_animator().
        tabs = QTabWidget()
        root_layout.addWidget(tabs)

        project_tab = QWidget()
        project_form = QFormLayout(project_tab)
        tabs.addTab(project_tab, "Project")

        trajectory_tab = QWidget()
        form = QFormLayout(trajectory_tab)
        tabs.addTab(trajectory_tab, "Trajectory")

        export_tab = QWidget()
        export_form = QFormLayout(export_tab)
        tabs.addTab(export_tab, "Export")

        # The project folder is the shared home for every export from here on --
        # Export Trajectory writes into <project>/trajectories/<run>/, Export
        # writes into <project>/exports/<run>/ (see _resolve_export_dir), each
        # <run> auto-named from the timestamp (and project name, if set) by
        # _run_folder_name(). The Export tab's own "Export folder" field still
        # overrides this per-run if you fill it in (see _resolve_export_dir) --
        # this is just the default so you don't have to browse every time.
        self.project_dir = QLineEdit()
        self.project_dir.setPlaceholderText("No project folder set -- exports fall back to Export folder/Browse")
        self.project_dir.textChanged.connect(lambda _: self._on_project_dir_changed())
        project_browse = QPushButton("Browse")
        project_browse.clicked.connect(self._pick_project_dir)
        project_dir_row = QHBoxLayout()
        project_dir_row.addWidget(self.project_dir)
        project_dir_row.addWidget(project_browse)
        project_form.addRow("Project folder", project_dir_row)

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("Optional -- prefixes auto-named export folders")
        project_form.addRow("Project name", self.project_name)

        # Duration/FPS live here, not on the Trajectory or Export tab -- they
        # set frame_count (duration_s * fps), which determines how densely a
        # generated curve is sampled (driving the path preview on the
        # Trajectory tab) and how the final video is timed, so they're
        # project-level settings rather than belonging to either single step.
        # Disabled for "Import trajectory file" (an imported file supplies its
        # own timing) by _on_source_changed.
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.5, 3600)
        self.duration.setValue(10.0)
        project_form.addRow("Duration (s)", self.duration)

        self.fps = QSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setValue(30)
        project_form.addRow("FPS", self.fps)

        # ---- Two collapsible groups, mutually exclusive -- expanding one
        # collapses the other (_on_import_group_toggled/_on_generate_group_
        # toggled/_set_active_source below). This replaces the old "Source"
        # combo box entirely: whichever group is expanded *is* the selected
        # source now, so there's nothing else to keep in sync.

        # ---- "Existing trajectories" -- reuse this plugin's own exports, or
        # an external CSV (see trajectory_io.py's module docstring for the
        # schema). Listed first: once a project has anything exported into
        # it, reusing one is the faster path. Checkable QGroupBox doesn't
        # collapse its contents on its own -- the actual content lives in an
        # inner plain QWidget (self.import_section) whose visibility the
        # toggle handler drives, giving the collapse effect. ----
        self.import_group = QGroupBox("Existing trajectories")
        self.import_group.setCheckable(True)
        import_group_layout = QVBoxLayout(self.import_group)
        self.import_section = QWidget()
        import_form = QFormLayout(self.import_section)
        import_form.setContentsMargins(0, 0, 0, 0)
        import_group_layout.addWidget(self.import_section)

        # Populated from <project folder>/trajectories/ (see _refresh_trajectory_
        # list, called whenever the Project folder changes or a new trajectory is
        # exported) -- picking one loads it immediately, same result as Browse but
        # without hunting for the file. Empty/hidden state (no project folder set,
        # or nothing exported into it yet) is handled inside _refresh_trajectory_list.
        self.trajectory_list_combo = QComboBox()
        self.trajectory_list_combo.currentIndexChanged.connect(self._on_trajectory_list_selected)
        import_form.addRow("Previously exported", self.trajectory_list_combo)

        self.import_path = QLineEdit()
        self.import_path.setReadOnly(True)
        self.import_path.setPlaceholderText("No file loaded")
        import_browse = QPushButton("Browse")
        import_browse.clicked.connect(self._browse_trajectory_file)
        import_row = QHBoxLayout()
        import_row.addWidget(self.import_path)
        import_row.addWidget(import_browse)
        import_form.addRow("Trajectory file", import_row)

        # CSV only -- shown after a .csv is picked, pre-filled by
        # trajectory_io.suggest_csv_mapping() and editable before committing.
        # JSON has a fixed schema (this plugin's own export) so it skips this
        # entirely and loads directly in _load_json_file(). Orientation comes
        # from EXACTLY ONE of a look-at target (Look-at X/Y/Z) or camera
        # angles (Pitch/Yaw), chosen explicitly via the Orientation type
        # combo below rather than inferred from whichever fields happen to be
        # mapped -- see trajectory_io.load_csv_with_mapping()'s docstring.
        self.csv_mapping_section = QWidget()
        csv_form = QFormLayout(self.csv_mapping_section)
        csv_form.setContentsMargins(0, 0, 0, 0)
        self.csv_mapping_form = csv_form  # kept for labelForField() -- see _set_csv_row_visible()

        self.csv_coord_space_combo = QComboBox()
        self.csv_coord_space_combo.addItems(list(_COORD_SPACES.keys()))
        csv_form.addRow("Coordinates are in", self.csv_coord_space_combo)

        self.csv_column_combos = {}
        for field in trajectory_io.CSV_POSITION_FIELDS:
            combo = QComboBox()
            self.csv_column_combos[field] = combo
            csv_form.addRow(trajectory_io.FIELD_LABELS[field], combo)

        # Orientation type + angle convention sit together, right above the
        # orientation fields they control -- Orientation type picks which
        # field group below is used (and shown), Angle convention only
        # matters for the Pitch/Yaw group, so it's kept immediately under
        # Orientation type rather than off at the end of the form.
        self.csv_orientation_source_combo = QComboBox()
        self.csv_orientation_source_combo.addItems(list(_ORIENTATION_SOURCES.keys()))
        self.csv_orientation_source_combo.currentIndexChanged.connect(
            self._on_csv_orientation_source_changed
        )
        csv_form.addRow("Orientation type", self.csv_orientation_source_combo)

        # Only relevant when orientation type is "angles" -- shown/hidden in
        # lockstep with the Pitch/Yaw/Roll rows by
        # _on_csv_orientation_source_changed().
        self.csv_angle_convention_combo = QComboBox()
        self.csv_angle_convention_combo.addItems(list(_ANGLE_CONVENTIONS.keys()))
        csv_form.addRow("Angle convention", self.csv_angle_convention_combo)

        for field in (
            trajectory_io.CSV_LOOKAT_FIELDS + trajectory_io.CSV_ORIENTATION_FIELDS
            + trajectory_io.CSV_ORIENTATION_OPTIONAL_FIELDS + trajectory_io.CSV_OPTIONAL_FIELDS
        ):
            combo = QComboBox()
            self.csv_column_combos[field] = combo
            csv_form.addRow(trajectory_io.FIELD_LABELS[field], combo)

        self.csv_load_btn = QPushButton("Load CSV with this mapping")
        self.csv_load_btn.clicked.connect(self._load_csv_with_current_mapping)
        csv_form.addRow(self.csv_load_btn)

        import_form.addRow(self.csv_mapping_section)
        self.csv_mapping_section.setVisible(False)
        self._on_csv_orientation_source_changed()  # set initial row visibility

        self.import_status = QLabel(
            "Load a .json this plugin exported, or a .csv (its own export, or an "
            "external file -- columns get matched below for you to check)."
        )
        self.import_status.setWordWrap(True)
        import_form.addRow(self.import_status)

        form.addRow(self.import_group)

        # ---- "Generate new" -- the original curve-picker UI. Same inner-
        # widget-for-collapse pattern as the group above. ----
        self.generate_group = QGroupBox("Generate new")
        self.generate_group.setCheckable(True)
        generate_group_layout = QVBoxLayout(self.generate_group)
        self.generate_section = QWidget()
        generate_form = QFormLayout(self.generate_section)
        generate_form.setContentsMargins(0, 0, 0, 0)
        generate_group_layout.addWidget(self.generate_section)
        self.generate_form = generate_form

        self.curve_combo = QComboBox()
        self.curve_combo.addItems(list(CURVES.keys()))
        self.curve_combo.currentTextChanged.connect(self._rebuild_param_form)
        generate_form.addRow("Curve", self.curve_combo)

        # Only meaningful for "Fly Through" -- other curves always orbit the focus
        # point, so this row is hidden for them (toggled in _rebuild_param_form).
        self.look_mode_combo = QComboBox()
        self.look_mode_combo.addItems(list(_LOOK_MODES.keys()))
        self.look_mode_combo.currentTextChanged.connect(lambda _: self._update_path_visualization())
        generate_form.addRow("Camera look", self.look_mode_combo)

        # Swapped per-curve in _rebuild_param_form -- shows what each spinbox
        # on the right actually controls before the user has to guess from the
        # name. Side by side (1:1 stretch = half the row each), image left,
        # that curve's own fields right, so the picture stays in view while
        # you adjust the numbers instead of scrolling past it.
        self.diagram_label = QLabel()
        self.diagram_label.setAlignment(Qt.AlignCenter)

        self.param_form = QFormLayout()

        curve_split = QHBoxLayout()
        curve_split.addWidget(self.diagram_label, 1)
        curve_split.addLayout(self.param_form, 1)
        generate_form.addRow(curve_split)

        # Fills every curve param below (radius/height, whichever this curve
        # has) from the visible elevation layers' own extent (point clouds
        # and/or DEM-enabled rasters -- see _is_elevation_layer()) -- the
        # auto-fill half of what "Center on elevation data" used to do in one
        # combined action; see _calculate_automatically()/_point_cloud_extent().
        # Split out so Center on elevation data can be a pure "move the focus
        # point" action.
        # Added to param_form itself (field column only, empty label) rather
        # than generate_form below -- lines it up in the same column as the
        # curve parameter spinboxes above it instead of spanning the full
        # tab width. It's re-added there at the end of every
        # _rebuild_param_form() call (see that method) since it needs to
        # survive curve changes, unlike the per-curve param rows.
        self.calc_btn = QPushButton("Calculate Automatically")
        self.calc_btn.setToolTip(
            "Sets this curve's radius/height parameters from the combined "
            "extent of the visible point cloud and DEM elevation layers -- "
            "same source data \"Center on elevation data\" uses for the focus "
            "point, but this only touches the curve parameters above, not "
            "the focus point."
        )
        self.calc_btn.clicked.connect(self._calculate_automatically)

        self.focus_x = _spinbox(0.0)
        self.focus_y = _spinbox(0.0)
        self.focus_z = _spinbox(0.0)
        focus_row = QHBoxLayout()
        for box in (self.focus_x, self.focus_y, self.focus_z):
            box.valueChanged.connect(lambda _: self._on_focus_spinbox_changed())
            focus_row.addWidget(box)
        # Icon buttons, inline with the x/y/z spinboxes rather than their own
        # row below -- see _icon_button()'s docstring for why (long text
        # labels here used to force the whole dock wider).
        center_btn = _icon_button(
            _crosshair_icon(),
            "Center on elevation data -- moves the focus point to the combined "
            "center of the visible point cloud and DEM elevation layers "
            "(a raster layer with Elevation enabled in Layer Properties). "
            "Doesn't touch the curve parameters above (see Calculate "
            "Automatically for those).",
        )
        center_btn.clicked.connect(self._center_on_point_clouds)
        focus_row.addWidget(center_btn)
        pick_btn = _icon_button(
            _pin_icon(),
            "Pick on map -- click a point on the 2D map to set the focus point. "
            "Uses the real point cloud/DEM elevation at the click if one is "
            "found there, otherwise ground level (z=0).",
        )
        pick_btn.clicked.connect(self._start_pick_on_map)
        focus_row.addWidget(pick_btn)
        generate_form.addRow("Focus point (x, y, z)", focus_row)

        # Rotation lives inside "Generate new" -- note this means it's hidden
        # whenever "Existing trajectories" is active instead, even though it
        # still functionally applies to an imported trajectory too (spins it
        # around its own centroid -- see CameraPathAnimator's rotation_*_deg
        # docstring and _build_animator()). Deliberate trade-off: visual
        # grouping with the other curve-shaping controls here, at the cost of
        # not being reachable from the Existing-trajectories side of the tab.
        self.rotation_x = QDoubleSpinBox()
        self.rotation_y = QDoubleSpinBox()
        self.rotation_z = QDoubleSpinBox()
        rotation_row = QHBoxLayout()
        for axis_text, box in (("X:", self.rotation_x), ("Y:", self.rotation_y), ("Z:", self.rotation_z)):
            box.setRange(-360.0, 360.0)
            box.setValue(0.0)
            box.setSuffix("°")
            box.valueChanged.connect(lambda _: self._update_path_visualization())
            # Inline per-axis label -- previously the only way to tell the three
            # apart was hovering for a tooltip or reading the shared row label.
            rotation_row.addWidget(QLabel(axis_text))
            rotation_row.addWidget(box)
        self.rotation_z.setToolTip(
            "Spins the whole trajectory -- camera positions and look direction "
            "together -- around the focus point, like a dial viewed from above. "
            "Doesn't change the path's height profile."
        )
        self.rotation_x.setToolTip(
            "Tilts the whole trajectory's horizontal plane -- e.g. tips a Helix "
            "over so it spirals sideways instead of climbing straight up. "
            "Applied before Rotation Y/Z."
        )
        self.rotation_y.setToolTip(
            "Tilts the whole trajectory's horizontal plane the other way. "
            "Applied after Rotation X, before Rotation Z."
        )
        rotation_label = QLabel("Rotation X, Y, Z (°)")
        rotation_label.setToolTip(
            "Rotates the whole trajectory around the focus point (or, when "
            "importing a file, its own centroid) in all 3 axes -- Z is the "
            "intuitive one (spins the path like a dial, viewed from above, "
            "leaving height untouched); X and Y tilt the path's horizontal "
            "plane itself, applied in X, then Y, then Z order."
        )
        generate_form.addRow(rotation_label, rotation_row)

        # Explicit trigger for the path/look-vector preview -- unlike the
        # passive per-field updates above (every curve-param/focus spinbox
        # already calls _update_path_visualization() on change), a deliberate
        # click here is allowed to create the 3D view as a side effect if one
        # doesn't exist yet. See _generate_trajectory()'s docstring.
        generate_btn = QPushButton("Generate Trajectory")
        generate_btn.setToolTip(
            "Builds the trajectory from the settings above and renders the "
            "camera path, positions, and look-direction vectors in the 2D "
            "and 3D views. Opens the 3D view first if one isn't open yet."
        )
        generate_btn.clicked.connect(self._generate_trajectory)
        generate_form.addRow(generate_btn)

        form.addRow(self.generate_group)

        # Wired last, after both groups fully exist -- each toggled handler
        # touches the other group (see _on_import_group_toggled/_on_generate_
        # group_toggled), so both must already be constructed.
        self.import_group.toggled.connect(self._on_import_group_toggled)
        self.generate_group.toggled.connect(self._on_generate_group_toggled)

        # ---- Preview section: trajectory-viewing/saving actions grouped
        # together -- the checkbox that toggles the static path/points/look-
        # vector preview (2D + 3D), the button that does a one-frame static
        # camera positioning against it (Preview Trajectory), and Save
        # (writes the trajectory to disk -- renamed from "Export Trajectory"
        # and moved here from the Export tab, since it's a natural next step
        # after previewing a trajectory's shape, not part of the actual
        # video-capture run buttons there). The animated camera-feed preview
        # lives on the Export tab instead (see Preview Camera Feed there) --
        # it's a rehearsal of an actual Export run, not a look at the static
        # trajectory shape, so it's grouped with Export instead of here.
        preview_section = QGroupBox("Preview")
        preview_section_layout = QVBoxLayout(preview_section)

        # QCheckBox doesn't word-wrap its label the way QLabel does -- a long
        # one-line label forces the whole dock to stay at least that wide, which
        # was the main reason this panel couldn't be resized narrower. Keep the
        # visible text short and put the detail in a tooltip instead.
        self.show_path_checkbox = QCheckBox("Show camera path preview")
        self.show_path_checkbox.setToolTip(
            "Shows the camera path, positions, focus point, and look direction, "
            "colour-graded by time, in both the 2D and 3D views. Hidden "
            "automatically while an Export (or Preview Camera Feed run) is "
            "actually in progress."
        )
        self.show_path_checkbox.setChecked(True)
        self.show_path_checkbox.stateChanged.connect(lambda _: self._on_show_path_checkbox_changed())
        preview_section_layout.addWidget(self.show_path_checkbox)

        # Applies regardless of source (Generate new or Existing trajectories)
        # -- the one deliberate place left that's allowed to open the 3D view
        # as a side effect of a click. See _preview_trajectory()'s docstring.
        preview_traj_btn = QPushButton("Preview Trajectory")
        preview_traj_btn.setToolTip(
            "Opens the 3D view (if not already open) and shows the camera "
            "path, positions, and look-direction vectors there for a quick "
            "visual check. Unlike Preview Camera Feed on the Export tab, "
            "this doesn't animate the camera through the path -- it's just "
            "a look."
        )
        preview_traj_btn.clicked.connect(self._preview_trajectory)
        preview_section_layout.addWidget(preview_traj_btn)

        self.trajectory_btn = QPushButton("Save")
        self.trajectory_btn.setToolTip(
            "Writes the current trajectory to disk as JSON + CSV keyframe "
            "data (under the Project folder's trajectories/ subtree, or the "
            "Export folder override) -- no rendering or video capture, just "
            "the camera path itself. Re-importable later (see Existing "
            "trajectories above) or usable in another tool/engine."
        )
        self.trajectory_btn.clicked.connect(self._export_trajectory)
        preview_section_layout.addWidget(self.trajectory_btn)

        form.addRow(preview_section)

        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Optional override -- leave blank to use the Project folder")
        self.output_dir.setToolTip(
            "Overrides the Project tab's project folder for this run only -- if "
            "set, both Export and Save write directly here instead of "
            "auto-creating a timestamped subfolder under the project folder."
        )
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_output_dir)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir)
        output_row.addWidget(browse)
        export_form.addRow("Export folder (override)", output_row)

        self.make_video_checkbox = QCheckBox("Stitch frames into an MP4 (requires ffmpeg)")
        self.make_video_checkbox.setChecked(True)
        export_form.addRow("", self.make_video_checkbox)

        # Export Trajectory (renamed Save) moved to the Trajectory tab's
        # Preview section. There's no Stop button any more -- a run can only
        # be interrupted by starting another Preview Camera Feed/Export
        # (both call _stop() on whatever's already running before starting
        # theirs, see _preview()/_export()), or by closing the plugin.
        buttons = QHBoxLayout()
        self.preview_btn = QPushButton("Preview Camera Feed")
        self.preview_btn.setToolTip(
            "Animates the camera through the full trajectory in the 3D view "
            "-- a rehearsal of what an actual Export run would look like, "
            "without capturing any frames. Opens the 3D view if one isn't "
            "open yet."
        )
        self.preview_btn.clicked.connect(self._preview)
        self.export_btn = QPushButton("Export")
        self.export_btn.clicked.connect(self._export)
        for btn in (self.preview_btn, self.export_btn):
            buttons.addWidget(btn)
        export_form.addRow(buttons)

        # Run-status line -- only visible while Preview Camera Feed/Export is
        # actually running (_set_run_ui_active), driven by CameraPathAnimator's
        # on_frame callback (see _on_run_frame/_build_animator).
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        export_form.addRow(self.status_label)

        self.status_progress = QProgressBar()
        self.status_progress.setVisible(False)
        export_form.addRow(self.status_progress)

        # Populated from <project folder>/exports/ (see _refresh_video_list,
        # called whenever the Project folder changes or an Export finishes)
        # -- double-click reveals the run's folder (frames/ + camera_path.mp4)
        # in the system file browser.
        self.video_list = QListWidget()
        self.video_list.itemDoubleClicked.connect(self._on_video_list_double_clicked)
        export_form.addRow("Previously exported videos", self.video_list)
        self._video_list_paths = {}

        self.setWidget(root)
        self._rebuild_param_form(self.curve_combo.currentText())
        self._set_active_source(is_import=False)  # "Generate new" active by default

        # Restore the last-used Project folder (see _SETTINGS_PROJECT_DIR_KEY's
        # docstring) -- must happen after every widget above is built, since
        # setText() below triggers _on_project_dir_changed(), which touches
        # trajectory_list_combo/video_list.
        saved_project_dir = QgsSettings().value(_SETTINGS_PROJECT_DIR_KEY, "", type=str)
        if saved_project_dir:
            self.project_dir.setText(saved_project_dir)  # triggers _on_project_dir_changed -> refreshes both lists
        else:
            self._refresh_trajectory_list()
            self._refresh_video_list()

    def _update_diagram(self, curve_name):
        filename = _DIAGRAM_FILES.get(curve_name)
        path = os.path.join(_DIAGRAM_DIR, filename) if filename else None
        pixmap = QPixmap(path) if path and os.path.isfile(path) else None
        if pixmap is None or pixmap.isNull():
            # Missing/regenerate-needed diagram shouldn't block using the plugin --
            # just fall back to no image instead of erroring.
            self.diagram_label.clear()
            _log(f"no diagram image for '{curve_name}' (expected at {path})")
            return
        self.diagram_label.setPixmap(
            pixmap.scaledToWidth(_DIAGRAM_WIDTH, Qt.SmoothTransformation)
        )

    def _on_import_group_toggled(self, checked):
        if checked:
            self._set_active_source(is_import=True)
        elif not self.generate_group.isChecked():
            # Both groups are checkable independently at the Qt level, but
            # exactly one must always be the active source -- clicking the
            # checkbox on the currently-active group would otherwise leave
            # both collapsed with nothing selected. Snap it back rather than
            # allowing that state.
            self.import_group.blockSignals(True)
            self.import_group.setChecked(True)
            self.import_group.blockSignals(False)

    def _on_generate_group_toggled(self, checked):
        if checked:
            self._set_active_source(is_import=False)
        elif not self.import_group.isChecked():
            self.generate_group.blockSignals(True)
            self.generate_group.setChecked(True)
            self.generate_group.blockSignals(False)

    def _set_active_source(self, is_import):
        """Single source of truth for which of the two collapsible groups is
        active -- called whenever one is expanded (by the user, or
        programmatically by _on_trajectory_list_selected picking a
        previously-exported trajectory). Syncs both groups' checked state
        (without re-entering the toggled handlers above), shows/hides their
        content, and disables Duration/FPS for import mode (an imported file
        supplies its own frame count/timing -- see CameraPathAnimator's
        imported_keyframes mode)."""
        self.import_group.blockSignals(True)
        self.generate_group.blockSignals(True)
        self.import_group.setChecked(is_import)
        self.generate_group.setChecked(not is_import)
        self.import_group.blockSignals(False)
        self.generate_group.blockSignals(False)

        self.import_section.setVisible(is_import)
        self.generate_section.setVisible(not is_import)
        self.duration.setEnabled(not is_import)
        self.fps.setEnabled(not is_import)
        self._update_path_visualization()

    def _is_import_mode(self):
        """True if "Existing trajectories" is the active source -- replaces
        the old self.source_combo.currentText() == "Import trajectory file"
        check now that there's no combo box driving this."""
        return self.import_group.isChecked()

    def _on_show_path_checkbox_changed(self):
        """Checking this box just delegates straight to _update_path_visualization(),
        which draws in the 2D map (and the 3D view too, if one's already open)
        via the real-canvas-or-headless-stand-in fallback in
        _live_or_headless_canvas3d() -- so checking the box no longer needs to
        force a 3D view open just to have something to show. It used to
        (_get_canvas3d() here, unconditionally, when checked): back when
        _update_path_visualization() genuinely couldn't draw anything at all
        without a live 3D canvas, that was the only way checking the box would
        visibly do anything the first time. That's no longer true."""
        self._update_path_visualization()

    def _update_path_visualization(self):
        """Rebuilds the camera path/positions/look-direction/focus layers from
        the current settings -- reusing CameraPathAnimator.frame_pose() so this
        always matches exactly what Preview/Export/Export Trajectory will
        actually do, whether the source is a generated curve or an imported
        file. See path_visualization.py for the layers themselves and why
        they're safe to leave visible outside of an actual Export run.
        """
        if not self.show_path_checkbox.isChecked():
            self.path_viz.set_visible(False)
            return

        # Deliberately not calling self._get_canvas3d() here -- that creates a 3D
        # view as a side effect if none exists yet, which would mean merely
        # nudging a spinbox opens a 3D view the user never asked for. Use the
        # real canvas if a 3D view already happens to be open (from Center on
        # point clouds/Pick on map/Preview/importing a file/manually opening
        # the 3D view/checking Show camera path preview, see
        # _on_show_path_checkbox_changed) -- otherwise fall back to a headless,
        # invisible stand-in (see _HeadlessCanvas3D above) purely for the
        # map<->scene coordinate math path_visualization.py's update() needs to
        # turn animator positions (scene-local) into the map-CRS coordinates
        # the layer geometries are stored in. That math is confirmed (see
        # _HeadlessMapSettings3D's docstring) to be exactly reproducible
        # without a real canvas, so the 2D map can keep live-updating even
        # with no 3D view open at all -- one never gets created just to
        # compute this.
        canvas3d = self._live_or_headless_canvas3d()

        animator = self._build_animator(canvas3d=canvas3d)
        if animator is None:
            return

        self.path_viz.update(animator, canvas3d.mapSettings(), map_canvas_2d=self.iface.mapCanvas())
        self.path_viz.set_visible(True)

    def _browse_trajectory_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import trajectory", "", "Trajectory files (*.json *.csv)"
        )
        if path:
            self._on_trajectory_file_selected(path)

    def _on_trajectory_file_selected(self, path):
        self.import_path.setText(path)
        # Cleared until a load (below) succeeds -- Preview/Export shouldn't run
        # against a stale trajectory while a new file's mapping is still being set up.
        self._imported_keyframes = None
        self._imported_fps = None

        if path.lower().endswith(".json"):
            self.csv_mapping_section.setVisible(False)
            self._load_json_file(path)
            return

        if not path.lower().endswith(".csv"):
            self.csv_mapping_section.setVisible(False)
            self.import_status.setText("Unsupported file type -- expected .json or .csv")
            return

        try:
            headers = trajectory_io.csv_headers(path)
        except OSError as exc:
            self.csv_mapping_section.setVisible(False)
            self.import_status.setText(f"Failed to read file: {exc}")
            return
        if not headers:
            self.csv_mapping_section.setVisible(False)
            self.import_status.setText("CSV has no header row")
            return

        mapping, coord_space = trajectory_io.suggest_csv_mapping(headers)
        self._populate_csv_mapping(headers, mapping, coord_space)
        self.csv_mapping_section.setVisible(True)

    def _on_csv_orientation_source_changed(self):
        """Shows only the field rows relevant to the selected orientation
        source (look-at target vs orientation angles), instead of leaving
        both visible and letting trajectory_io.load_csv_with_mapping() ignore
        whichever one wasn't chosen -- an ignored-but-still-filled-in row
        invites confusion about which mapping actually took effect."""
        source = _ORIENTATION_SOURCES[self.csv_orientation_source_combo.currentText()]
        show_lookat = source == "lookat"
        for field in trajectory_io.CSV_LOOKAT_FIELDS:
            self._set_csv_row_visible(self.csv_column_combos[field], show_lookat)
        for field in trajectory_io.CSV_ORIENTATION_FIELDS + trajectory_io.CSV_ORIENTATION_OPTIONAL_FIELDS:
            self._set_csv_row_visible(self.csv_column_combos[field], not show_lookat)
        self._set_csv_row_visible(self.csv_angle_convention_combo, not show_lookat)

    def _set_csv_row_visible(self, field_widget, visible):
        field_widget.setVisible(visible)
        label = self.csv_mapping_form.labelForField(field_widget)
        if label is not None:
            label.setVisible(visible)

    def _populate_csv_mapping(self, headers, mapping, coord_space):
        options = [_NO_COLUMN] + headers
        for field, combo in self.csv_column_combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(options)
            guess = mapping.get(field)
            combo.setCurrentText(guess if guess else _NO_COLUMN)
            combo.blockSignals(False)

        # Guess which orientation source this file actually has from which
        # fields suggest_csv_mapping() matched -- still just a starting
        # point for the combo above, not trusted blindly (same as every
        # other guess in this form); "lookat" is the fallback when neither
        # (or, ambiguously, both) got matched.
        if all(mapping.get(f) for f in trajectory_io.CSV_ORIENTATION_FIELDS) and not all(
            mapping.get(f) for f in trajectory_io.CSV_LOOKAT_FIELDS
        ):
            source_label = next(
                label for label, value in _ORIENTATION_SOURCES.items() if value == "angles"
            )
        else:
            source_label = next(
                label for label, value in _ORIENTATION_SOURCES.items() if value == "lookat"
            )
        self.csv_orientation_source_combo.setCurrentText(source_label)
        self._on_csv_orientation_source_changed()  # setCurrentText() above is a no-op if unchanged

        space_label = next(label for label, value in _COORD_SPACES.items() if value == coord_space)
        self.csv_coord_space_combo.setCurrentText(space_label)

        # Always reset to the default rather than carry over from whatever
        # file was loaded previously -- suggest_csv_mapping() deliberately
        # doesn't guess this (see its docstring), so there's nothing to
        # pre-fill it with.
        default_label = next(
            label for label, value in _ANGLE_CONVENTIONS.items()
            if value == trajectory_io.DEFAULT_ANGLE_CONVENTION
        )
        self.csv_angle_convention_combo.setCurrentText(default_label)

    def _load_json_file(self, path):
        # _live_or_headless_canvas3d() rather than _get_canvas3d() -- the only
        # thing map_settings is used for below is load_json_trajectory()'s
        # mapToWorldCoordinates() calls (pure coordinate math, see
        # _HeadlessMapSettings3D's docstring), not anything that needs a real,
        # rendered 3D scene, so importing a trajectory shouldn't force a 3D
        # view open on its own.
        map_settings = self._live_or_headless_canvas3d().mapSettings()
        try:
            keyframes, fps, warnings = trajectory_io.load_json_trajectory(
                path, map_settings, assumed_fps=self.fps.value()
            )
        except trajectory_io.TrajectoryLoadError as exc:
            self.import_status.setText(f"Failed to load: {exc}")
            _log(f"trajectory import failed for {path}: {exc}")
            return
        self._set_imported(path, keyframes, fps, warnings)

    def _load_csv_with_current_mapping(self):
        path = self.import_path.text().strip()
        if not path:
            return
        mapping = {
            field: (combo.currentText() if combo.currentText() != _NO_COLUMN else None)
            for field, combo in self.csv_column_combos.items()
        }
        coord_space = _COORD_SPACES[self.csv_coord_space_combo.currentText()]
        angle_convention = _ANGLE_CONVENTIONS[self.csv_angle_convention_combo.currentText()]
        orientation_source = _ORIENTATION_SOURCES[self.csv_orientation_source_combo.currentText()]

        # See _load_json_file()'s comment -- same reasoning, same fallback.
        # One side effect: load_csv_with_mapping()'s "map" coord_space used to
        # require a live 3D view (map_settings was None otherwise, and it
        # raises TrajectoryLoadError in that case) -- since this now always
        # provides real or headless map_settings, that restriction no longer
        # applies in practice; the guard in trajectory_io.py is harmless dead
        # code, not removed since a future caller could still hit it.
        map_settings = self._live_or_headless_canvas3d().mapSettings()
        try:
            keyframes, fps, warnings = trajectory_io.load_csv_with_mapping(
                path, mapping, coord_space, map_settings, assumed_fps=self.fps.value(),
                angle_convention=angle_convention, orientation_source=orientation_source,
            )
        except trajectory_io.TrajectoryLoadError as exc:
            self.import_status.setText(f"Failed to load: {exc}")
            _log(f"trajectory import failed for {path}: {exc}")
            return
        self._set_imported(path, keyframes, fps, warnings)

    def _set_imported(self, path, keyframes, fps, warnings):
        self._imported_keyframes = keyframes
        self._imported_fps = fps
        self._imported_path = path
        duration = keyframes[-1]["time_s"] if keyframes else 0.0
        summary = f"Loaded: {len(keyframes)} keyframes, ~{duration:.2f}s, ~{fps:.1f} fps (estimated)"
        if warnings:
            summary += "\n" + "\n".join(f"⚠ {w}" for w in warnings)
        self.import_status.setText(summary)
        _log(f"trajectory imported from {path}: {len(keyframes)} keyframes, ~{fps:.2f} fps")
        self._update_path_visualization()

    def _rebuild_param_form(self, curve_name):
        show_look_mode = curve_name == "Fly Through"
        self.look_mode_combo.setVisible(show_look_mode)
        look_mode_label = self.generate_form.labelForField(self.look_mode_combo)
        if look_mode_label is not None:
            look_mode_label.setVisible(show_look_mode)

        self._update_diagram(curve_name)

        # calc_btn is the last row of param_form (added at the end below), but
        # it's a persistent widget that must survive this rebuild, unlike the
        # per-curve param rows the while-loop clears -- QFormLayout.removeRow()
        # deletes the row's widgets, which would destroy calc_btn the first
        # time this ran again. takeRow() removes a row from the layout WITHOUT
        # deleting its widgets, so it's pulled out first (only relevant from
        # the second call onward -- the very first call has nothing to take)
        # and re-added once the new params are in place.
        if self.param_form.indexOf(self.calc_btn) != -1:
            self.param_form.takeRow(self.param_form.rowCount() - 1)

        while self.param_form.rowCount():
            self.param_form.removeRow(0)
        self.param_boxes = {}
        self.param_roles = {}
        _, params = CURVES[curve_name]
        for name, default, role in params:
            box = _spinbox(default)
            box.valueChanged.connect(lambda _: self._update_path_visualization())
            self.param_boxes[name] = box
            self.param_roles[name] = role
            self.param_form.addRow(name, box)

        # Field column only (empty label) -- lines calc_btn up in the same
        # column as the parameter spinboxes above it rather than spanning the
        # full row width.
        self.param_form.addRow("", self.calc_btn)

        self._update_path_visualization()

    def cleanup(self):
        self._stop()
        self.path_viz.remove_from_project()
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self.pick_tool:
            canvas.unsetMapTool(self.pick_tool)

    def _pick_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Export folder")
        if path:
            self.output_dir.setText(path)

    def _pick_project_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Project folder")
        if path:
            self.project_dir.setText(path)  # triggers textChanged -> _on_project_dir_changed

    def _on_project_dir_changed(self):
        # Persisted on every change (Browse, or typing directly) so it survives
        # a QGIS restart or plugin reload -- see _SETTINGS_PROJECT_DIR_KEY's
        # docstring above and __init__'s restore call.
        QgsSettings().setValue(_SETTINGS_PROJECT_DIR_KEY, self.project_dir.text().strip())
        self._refresh_trajectory_list()
        self._refresh_video_list()

    def _run_folder_name(self):
        """Auto-generated subfolder name for one Export/Export Trajectory run --
        a timestamp (so nothing ever overwrites a previous run), optionally
        prefixed with a sanitized Project name if one's set. Filesystem-unsafe
        characters in the name are collapsed to '-' rather than rejected --
        the field is free text, not validated as you type."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        name = self.project_name.text().strip()
        if not name:
            return stamp
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "project"
        return f"{safe}_{stamp}"

    def _resolve_export_dir(self, kind):
        """Where one Export/Export Trajectory run's files should go. kind is
        "trajectory" or "video", selecting which project-folder subtree to use.

        Priority: the Export tab's manual "Export folder" override, if filled
        in -- exact behavior from before Project folder existed, both actions
        sharing one literal folder. Otherwise, if a Project folder is set, a
        fresh timestamped subfolder auto-created under its trajectories/ or
        exports/ subtree. Otherwise (neither set), falls back to prompting via
        Browse, same as the plugin's original behavior.

        Returns None if the user cancels the Browse fallback -- callers should
        bail out without exporting in that case.
        """
        manual = self.output_dir.text().strip()
        if manual:
            os.makedirs(manual, exist_ok=True)
            return manual

        project_dir = self.project_dir.text().strip()
        if project_dir:
            subtree = "trajectories" if kind == "trajectory" else "exports"
            run_dir = os.path.join(project_dir, subtree, self._run_folder_name())
            os.makedirs(run_dir, exist_ok=True)
            _log(f"auto-exporting into project folder: {run_dir}")
            return run_dir

        self._pick_output_dir()
        manual = self.output_dir.text().strip()
        if not manual:
            return None
        os.makedirs(manual, exist_ok=True)
        return manual

    def _refresh_trajectory_list(self):
        """Rescans <project folder>/trajectories/ for previously exported
        trajectory.json files (one per run subfolder, newest first) and
        repopulates the Trajectory tab's combo. Called on Project folder
        change and right after a successful Export Trajectory, so the list
        never goes stale without the user having to do anything."""
        self.trajectory_list_combo.blockSignals(True)
        self.trajectory_list_combo.clear()
        self.trajectory_list_combo.addItem(_NO_TRAJECTORY_SELECTED)

        self._trajectory_list_paths = {}
        project_dir = self.project_dir.text().strip()
        traj_root = os.path.join(project_dir, "trajectories") if project_dir else None
        if traj_root and os.path.isdir(traj_root):
            for name in sorted(os.listdir(traj_root), reverse=True):
                candidate = os.path.join(traj_root, name, "trajectory.json")
                if os.path.isfile(candidate):
                    self._trajectory_list_paths[name] = candidate
                    self.trajectory_list_combo.addItem(name)

        self.trajectory_list_combo.blockSignals(False)

    def _on_trajectory_list_selected(self, index):
        if index <= 0:  # placeholder, or blockSignals-driven repopulation
            return
        path = self._trajectory_list_paths.get(self.trajectory_list_combo.currentText())
        if not path:
            return
        self.import_group.setChecked(True)  # triggers _on_import_group_toggled -> _set_active_source
        self.import_path.setText(path)
        self.csv_mapping_section.setVisible(False)
        self._load_json_file(path)

    def _refresh_video_list(self):
        """Rescans <project folder>/exports/ for previously exported
        camera_path.mp4 files (one per run subfolder, newest first) and
        repopulates the Export tab's list. Runs that only saved frames (video
        checkbox unticked, or ffmpeg missing) don't show up here -- there's no
        video to list -- but the frames are still in that run's subfolder."""
        self.video_list.clear()
        self._video_list_paths = {}
        project_dir = self.project_dir.text().strip()
        exports_root = os.path.join(project_dir, "exports") if project_dir else None
        if not exports_root or not os.path.isdir(exports_root):
            return
        for name in sorted(os.listdir(exports_root), reverse=True):
            mp4_path = os.path.join(exports_root, name, "camera_path.mp4")
            if os.path.isfile(mp4_path):
                self._video_list_paths[name] = mp4_path
                self.video_list.addItem(name)

    def _on_video_list_double_clicked(self, item):
        path = self._video_list_paths.get(item.text())
        if not path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        except Exception as exc:
            _log(f"couldn't open export folder for '{item.text()}': {exc}")

    def _live_or_headless_canvas3d(self):
        """Returns the real 3D canvas if one is already open and alive,
        otherwise a headless, invisible stand-in (_HeadlessCanvas3D) purely
        for map<->scene coordinate math -- never calls _get_canvas3d(), so
        this can never create or show a real 3D view as a side effect.

        Safe wherever the caller only needs mapToWorldCoordinates()/
        worldToMapCoordinates() and not an actual rendered/movable camera
        (screen capture, live setLookingAtPoint() playback): confirmed
        against QGIS 3.44's actual source (see _HeadlessMapSettings3D's
        docstring) that this conversion is pure per-axis origin subtraction
        with no CRS reprojection, and that the origin cancels out of every
        round-trip this plugin does with it -- so the headless stand-in's
        origin (0, 0, 0) is exactly as correct as whatever origin a real
        canvas happens to have.
        """
        if self.canvas3d is not None and not sip.isdeleted(self.canvas3d):
            return self.canvas3d
        return _HeadlessCanvas3D()

    def _get_canvas3d(self, force_refresh=False):
        # Deliberately not falling back to mapCanvases3D()[0]: any stray "3D Map N"
        # view left over from earlier testing (or opened by the user for unrelated
        # work) would get grabbed here, and its scene origin has nothing to do with
        # this project's current layers -- that's what produced wildly-wrong scene
        # coordinates (origin subtraction against an unrelated canvas). Always make
        # (or reuse) our own canvas instead, so its Qgs3DMapSettings origin is
        # actually derived from the layers/extent that are loaded right now.
        #
        # force_refresh matters because a *cached* canvas's origin is frozen at
        # whatever it was when the canvas was created -- QGIS doesn't recompute it
        # as you swap which point cloud is visible later. Reusing the stale canvas
        # for "Center on point clouds" against a since-replaced point cloud converts
        # against an origin that no longer matches the data at all.
        if force_refresh or self.canvas3d is None or sip.isdeleted(self.canvas3d):
            # createNewMapCanvas3D() only reuses the "QuickMapCine" name if
            # nothing is still registered under it -- otherwise it silently
            # falls back to an auto-incremented "3D Map N" and the old
            # registration is left behind as clutter. Clearing both the open
            # canvas (if any) and its persisted view registration first means
            # we land back on the same name instead of piling up new ones.
            self.iface.closeMapCanvas3D("QuickMapCine")
            QgsProject.instance().viewsManager().remove3DView("QuickMapCine")
            self.canvas3d = self.iface.createNewMapCanvas3D("QuickMapCine")
            map_settings = self.canvas3d.mapSettings()

            # Explicitly mirror whatever's checked in the 2D layers panel into
            # the 3D scene's own layer list, rather than trusting whatever
            # createNewMapCanvas3D() defaults to -- cheap and always correct
            # either way, and removes one more variable if a layer (a DEM
            # raster in particular -- see below) doesn't show up as expected.
            try:
                map_settings.setLayers(self.iface.mapCanvas().layers())
            except Exception as exc:
                _log(f"could not set 3D scene layers: {exc}")

            # A freshly created 3D view defaults to terrain rendering ON (a flat
            # QgsFlatTerrainSettings, confirmed in QGIS's own source -- the "3D
            # Configuration > Terrain" checkbox shown checked is just this flag's
            # UI). Before v0.2 there was never a DEM/elevation layer driving it,
            # so it just visually competed with the point cloud/camera path for
            # no benefit -- default it off in that case, same as before.
            #
            # v0.2 added DEM-enabled raster layers as an elevation source (see
            # _is_elevation_layer()) -- when one is visible, configure the 3D
            # view's terrain to actually use its real shape instead of leaving
            # it flat.
            #
            # QgsDemTerrainGenerator (the older, generator-based way of doing
            # this) has never been exposed to Python, in any QGIS version --
            # confirmed against QGIS's own API docs from 3.14 through master.
            # QGIS 3.42 replaced the generator-based terrain API with a new
            # settings-based one (Qgs3DMapSettings.setTerrainSettings() taking
            # a QgsDemTerrainSettings), and *that* one is Python-accessible --
            # confirmed directly against a QGIS 3.44 install via the console
            # (hasattr(mapSettings(), "setTerrainSettings") is True there,
            # while "setTerrainGenerator" and "configureTerrainFromProject"
            # are both False/gone). QGIS documents QgsDemTerrainSettings as an
            # unstable "tech preview" API that may change in a future
            # release, so this is wrapped defensively and never assumed to be
            # there.
            #
            # QGIS versions between this plugin's 3.36 floor and 3.42 predate
            # both the old and new Python-accessible paths, as far as this
            # plugin has found -- there terrain rendering is left off, same
            # as before v0.2, with a log message pointing at the manual
            # workaround (3D view's own Terrain dialog).
            dem_layers = [
                l for l in self.iface.mapCanvas().layers()
                if isinstance(l, QgsRasterLayer) and _is_elevation_layer(l)
            ]
            if dem_layers:
                # Only one raster can drive DEM terrain at a time -- first
                # visible match wins (same "first hit wins" precedence
                # _sample_dem_elevation() already uses).
                configured = False
                try:
                    from qgis._3d import QgsDemTerrainSettings

                    try:
                        dem_settings = QgsDemTerrainSettings()
                    except Exception:
                        dem_settings = QgsDemTerrainSettings.create()
                    dem_settings.setLayer(dem_layers[0])
                    map_settings.setTerrainSettings(dem_settings)
                    map_settings.setTerrainRenderingEnabled(True)
                    configured = True
                    _log(f"configured 3D terrain from DEM layer '{dem_layers[0].name()}' via QgsDemTerrainSettings")
                except Exception as exc:
                    _log(f"QgsDemTerrainSettings terrain setup not available/failed: {exc}")

                if not configured:
                    try:
                        map_settings.setTerrainRenderingEnabled(False)
                    except Exception:
                        pass
                    _log(
                        "This QGIS version doesn't expose a Python-accessible way "
                        "this plugin could find to configure DEM terrain shape "
                        "automatically (needs QGIS 3.42+). Terrain rendering left "
                        "off -- open this 3D view's own 3D Configuration (wrench "
                        "icon) > Terrain, set Type to 'DEM (Raster Layer)' and "
                        f"pick '{dem_layers[0].name()}' manually if you want real "
                        "terrain shape."
                    )
            else:
                try:
                    map_settings.setTerrainRenderingEnabled(False)
                except Exception as exc:
                    _log(f"could not disable default terrain rendering: {exc}")
        return self.canvas3d

    def _pick_point_from_cloud(self, map_point_xy):
        """Find the point-cloud point nearest map_point_xy by querying
        QgsPointCloudDataProvider.identify() on each visible point cloud layer.
        Returns an (x, y, z) tuple in project CRS, or None if no point cloud
        data was found close enough to the click.
        """
        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        layers = [l for l in canvas.layers() if isinstance(l, QgsPointCloudLayer)]
        if not layers:
            return None

        # identify()'s maxError is a level-of-detail knob (in layer coords): using
        # the current screen resolution means "as precise as what's visible now".
        # The search buffer accounts for the click not landing exactly on a point.
        max_error = canvas.mapUnitsPerPixel()
        click_geom = QgsGeometry.fromPointXY(map_point_xy).buffer(max_error * 6, 8)

        best, best_dist = None, None
        for layer in layers:
            reproject = layer.crs().isValid() and layer.crs() != project.crs()
            if reproject:
                to_layer = QgsCoordinateTransform(project.crs(), layer.crs(), project)
                query_geom = QgsGeometry(click_geom)
                query_geom.transform(to_layer)
            else:
                query_geom = click_geom

            results = layer.dataProvider().identify(max_error, query_geom, QgsDoubleRange(), 100)
            for attrs in results:
                x, y, z = attrs.get("X"), attrs.get("Y"), attrs.get("Z")
                if x is None:
                    continue
                if reproject:
                    hit = to_layer.transform(QgsPointXY(x, y), QgsCoordinateTransform.ReverseTransform)
                    x, y = hit.x(), hit.y()
                dist = math.hypot(x - map_point_xy.x(), y - map_point_xy.y())
                if best_dist is None or dist < best_dist:
                    best_dist, best = dist, (x, y, z)

        _log(f"pick_point_from_cloud: {'found ' + str(best) if best else 'no point cloud data near click'}")
        return best

    def _sample_dem_elevation(self, map_point_xy):
        """Samples elevation at map_point_xy from the first visible DEM-
        enabled raster layer (see _is_elevation_layer()) with valid data
        there. Returns a float, or None if there's no such layer or the
        point falls outside its extent/nodata. Used by _on_map_point_picked()
        as the fallback below point clouds -- point cloud data, where
        present, is the more direct/authoritative source (an actual sampled
        surface point, not an interpolated raster cell), so it's tried
        first; this only runs when that comes up empty.
        """
        project = QgsProject.instance()
        layers = [
            l for l in self.iface.mapCanvas().layers()
            if isinstance(l, QgsRasterLayer) and _is_elevation_layer(l)
        ]
        for layer in layers:
            point = map_point_xy
            if layer.crs().isValid() and layer.crs() != project.crs():
                transform = QgsCoordinateTransform(project.crs(), layer.crs(), project)
                point = transform.transform(point)
            band = _dem_band_number(layer.elevationProperties())
            value, ok = layer.dataProvider().sample(point, band)
            if ok and math.isfinite(value):
                _log(f"sample_dem_elevation: hit '{layer.name()}' band {band} -> z={value}")
                return value
        return None

    def _point_cloud_extent(self):
        """Combined map-CRS extent and z-range across every visible
        elevation-source layer (point clouds, always; DEM-enabled raster
        layers -- see _is_elevation_layer()), or None if there are none.
        Shared by _center_on_point_clouds() (focus point) and
        _calculate_automatically() (curve radius/height) -- both used to be
        one combined action; now split per-button, but they still need the
        same underlying geometry, just turned into different numbers.
        Returns (extent, z_lo, z_hi, has_valid_crs, layers); z_lo/z_hi are
        None if no layer had a finite z-range. Name kept from before DEM
        support was added (point clouds were the only source then); not
        renamed to avoid a purely-cosmetic diff across every call site.
        """
        project = QgsProject.instance()
        # mapCanvas().layers() is the same checked/visible layer list the 3D scene
        # itself renders from -- using project.mapLayers() instead would fold in
        # unchecked layers that aren't actually part of what's on screen.
        layers = [l for l in self.iface.mapCanvas().layers() if _is_elevation_layer(l)]
        if not layers:
            return None

        extent = QgsRectangle()
        z_lo, z_hi = None, None
        for layer in layers:
            layer_extent = layer.extent()
            crs_valid = layer.crs().isValid()
            if crs_valid:
                transform = QgsCoordinateTransform(layer.crs(), project.crs(), project)
                layer_extent = transform.transformBoundingBox(layer_extent)
            # else: no CRS to reproject from -- treat the layer's raw coordinates
            # as already being in project space rather than feeding them through a
            # transform that doesn't apply to them (produces huge bogus offsets).
            _log(
                f"  layer '{layer.name()}': crs={layer.crs().authid() or 'invalid'} "
                f"raw_extent={layer.extent().toString()} "
                f"{'-> reprojected ' + layer_extent.toString() if crs_valid else '(kept as-is, no valid CRS)'}"
            )
            extent.combineExtentWith(layer_extent)

            zrange = layer.elevationProperties().calculateZRange(layer)
            _log(f"  layer '{layer.name()}': z range={zrange}")
            if not zrange.isInfinite():
                z_lo = zrange.lower() if z_lo is None else min(z_lo, zrange.lower())
                z_hi = zrange.upper() if z_hi is None else max(z_hi, zrange.upper())

        has_valid_crs = any(layer.crs().isValid() for layer in layers)
        return extent, z_lo, z_hi, has_valid_crs, layers

    def _center_on_point_clouds(self):
        """Moves the focus point to the visible elevation layers' (point
        clouds and/or DEM-enabled rasters -- see _is_elevation_layer())
        combined center. Only touches focus_x/y/z/_focus_map -- see
        _calculate_automatically() for the curve-parameter half of what this
        button used to also do in one combined action. Name kept from before
        DEM support was added -- see _point_cloud_extent()'s docstring."""
        info = self._point_cloud_extent()
        if info is None:
            _log("center_on_point_clouds: no elevation-source layers")
            return
        extent, z_lo, z_hi, has_valid_crs, layers = info
        _log(f"center_on_point_clouds: {len(layers)} elevation-source layer(s): {[l.name() for l in layers]}")

        center_z = (z_lo + z_hi) / 2 if z_lo is not None else 0.0
        map_center = QgsVector3D(extent.center().x(), extent.center().y(), center_z)
        _log(f"combined extent={extent.toString()} -> map_center={map_center}")

        project = QgsProject.instance()
        if has_valid_crs:
            # setLookingAtPoint() expects 3D-scene-local coordinates, not map/project
            # coordinates -- the scene has its own origin and, when the project CRS is
            # geographic, an internally-derived metric CRS too, both handled by
            # mapSettings().mapToWorldCoordinates(). Deliberately not calling
            # self._get_canvas3d() here (this used to force_refresh=True, always
            # closing/reopening a real, visible 3D view just to compute this) --
            # "Center on point clouds" shouldn't open or reopen a 3D view on its
            # own (same reasoning as every other passive/focus action -- see
            # _update_path_visualization()'s docstring). Uses whatever 3D view is
            # already open if there is one, otherwise a headless, invisible
            # stand-in purely for this conversion -- confirmed (see
            # _HeadlessMapSettings3D's docstring) that the origin used here has no
            # effect on _focus_map (the authoritative value, set below straight
            # from map_center with no origin baked in at all) or anything
            # _build_animator() re-derives from it later; it only affects these
            # spinboxes' displayed numbers.
            map_settings = self._live_or_headless_canvas3d().mapSettings()
            _log(f"scene origin={map_settings.origin()} project_crs={project.crs().authid() or 'invalid/none'}")
            scene_center = map_settings.mapToWorldCoordinates(map_center)
        else:
            # ponytail: no layer here has a CRS, so there's nothing valid for
            # mapToWorldCoordinates()'s project<->scene conversion to apply to (QGIS
            # renders such point clouds using their raw local coordinates directly,
            # with no reprojection) -- use the combined center as scene position as-is.
            # A mix of CRS-less and georeferenced point cloud layers isn't handled --
            # there's no single coordinate space they both belong to.
            scene_center = map_center
        _log(f"map_center -> scene_center={scene_center}")
        for box in (self.focus_x, self.focus_y, self.focus_z):
            box.blockSignals(True)
        self.focus_x.setValue(scene_center.x())
        self.focus_y.setValue(scene_center.y())
        self.focus_z.setValue(scene_center.z())
        for box in (self.focus_x, self.focus_y, self.focus_z):
            box.blockSignals(False)
        # map_center is already the map-CRS point -- store it directly rather than
        # round-tripping through the just-set scene-local spinboxes, and it's the
        # authoritative value _build_animator() re-derives scene-local focus from
        # from now on (see this attribute's docstring in __init__). Only when a
        # real map<->scene conversion actually applies (has_valid_crs) -- in the
        # CRS-less case scene_center is used completely unconverted (see comment
        # above), so there's no map-CRS equivalent to track -- clear it (rather
        # than leaving a stale value from some earlier, unrelated pick) so
        # _build_animator() falls back to reading the spinboxes directly.
        self._focus_map = map_center if has_valid_crs else None
        self._update_path_visualization()

    def _calculate_automatically(self):
        """Fills every curve parameter with a "radius" or "height" role
        (see CURVES) from the visible elevation layers' combined extent
        (point clouds and/or DEM-enabled rasters -- see
        _is_elevation_layer()) -- the auto-fill half of what "Center on
        elevation data" used to also do in one combined action. Only touches
        the curve's own param_boxes, not the focus point (see
        _center_on_point_clouds() for that)."""
        info = self._point_cloud_extent()
        if info is None:
            _log("calculate_automatically: no elevation-source layers")
            return
        extent, z_lo, z_hi, _has_valid_crs, layers = info
        _log(f"calculate_automatically: {len(layers)} elevation-source layer(s): {[l.name() for l in layers]}")

        # Half the extent's diagonal guarantees the curve orbits outside the data
        # regardless of aspect ratio; the z span gives a vertical excursion that
        # actually matches this data instead of the curve's fixed default.
        radius = math.hypot(extent.width(), extent.height()) / 2
        height = (z_hi - z_lo) if z_lo is not None else 0.0
        _log(f"fitting curve params to extent: radius={radius:.2f} height={height:.2f}")
        # Blocked while setting several values in a row -- each is wired to redraw
        # the path preview, and firing that mid-batch would draw with some values
        # already updated and others still stale. One redraw at the end instead.
        for box in self.param_boxes.values():
            box.blockSignals(True)
        for name, role in self.param_roles.items():
            if role == "radius":
                self.param_boxes[name].setValue(radius)
            elif role == "height":
                self.param_boxes[name].setValue(height)
        for box in self.param_boxes.values():
            box.blockSignals(False)
        self._update_path_visualization()

    def _generate_trajectory(self):
        """Builds the trajectory from the current settings and renders it
        into the path/points/look-vector preview layers, in both the 2D map
        and 3D view -- but ONLY if a 3D canvas already happens to be open.
        Deliberately does NOT create/open one itself (same reasoning as every
        other field's passive auto-update -- see _update_path_visualization()'s
        docstring: a 2D-only action like this or Pick on map popping open a
        3D view as a side effect is exactly what users didn't want), and
        animator positions are scene-local, so without a live 3D canvas to
        convert them against there is genuinely nothing to draw yet, not even
        in 2D. Use Preview Trajectory (or check Show camera path preview) to
        open the 3D view and actually see a result."""
        self._update_path_visualization()

    def _preview_trajectory(self):
        """Opens the 3D view (creating one if it doesn't exist yet, or
        bringing an existing-but-buried one back to front) and shows the
        static path/points/look-vector preview there -- a quick visual check
        of the trajectory's shape and position against the 3D scene. This is
        deliberately the one remaining action allowed to open a 3D view as a
        side effect of a click, now that Generate Trajectory and Pick on map
        no longer do. Unlike the Export tab's Preview button, this doesn't
        animate the camera through the path at all -- it's just a look,
        not a run.

        Never writes to focus_x/y/z or _focus_map -- it only reads whatever
        focus is already current (via _current_focus(), same as an actual
        Preview/Export run would) and points the camera there. If no focus
        has been set yet, that's whatever focus_x/y/z default to (0,0,0) or
        whatever was last typed/picked; use Center on elevation data first to
        aim it at the loaded data. This used to auto-center on the visible
        elevation layers itself the first time, which silently overwrote a
        focus the user had just typed in if no 3D canvas existed yet when
        they typed it (the display and the internal map-CRS value could go
        out of sync in that case) -- removed rather than patched further, so
        this action can never surprise-edit a value the user set by hand.

        Also points the 3D view's ACTUAL camera at the trajectory's first
        frame (not just the focus_x/y/z display) -- a freshly created 3D
        canvas starts at QGIS's own default camera position, which has
        nothing to do with this project's data, so without this the point
        cloud/path wouldn't actually be in view even once the layers exist.
        Reuses the same setLookingAtPoint() pose CameraPathAnimator itself
        drives during playback, so "centered" here means exactly where frame
        0 of an actual Preview/Export run would start.
        """
        canvas3d = self._get_canvas3d()
        canvas3d.show()
        canvas3d.requestActivate()
        self._update_path_visualization()

        animator = self._build_animator()
        if animator is not None and animator.frame_count > 0:
            controller = canvas3d.cameraController()
            if controller is not None:
                position, center, distance, pitch, yaw = animator.frame_pose(0)
                controller.setLookingAtPoint(center, distance, pitch, yaw)
                # See CameraPathAnimator.log_camera_debug()'s docstring -- same
                # intended-vs-actual comparison _advance() logs every frame during
                # Preview/Export, done once here for the static Preview Trajectory pose.
                animator.log_camera_debug(0, position, center, distance, pitch, yaw, controller)

    def _start_pick_on_map(self):
        canvas = self.iface.mapCanvas()
        canvas.setMapTool(self.pick_tool)

    def _on_map_point_picked(self, point, _button):
        """Sets the focus point from a 2D-map click -- deliberately never
        calls _get_canvas3d(), so this can't pop open a 3D view as a side
        effect of what's otherwise a purely 2D action. _focus_map (map-CRS,
        origin-independent -- see its docstring in __init__) is set directly
        from the pick, which is all _build_animator() actually needs; the
        spinboxes below are converted for display via
        _live_or_headless_canvas3d() (reuses a real 3D canvas if one's
        already open, otherwise a headless stand-in that's exactly as correct
        for this pure coordinate math -- see that method's docstring), so
        they always show the real scene-local numbers immediately rather
        than a map-CRS placeholder that only gets corrected later.
        """
        canvas = self.iface.mapCanvas()
        canvas.setMapTool(self.pan_tool)  # one-shot pick, then always back to Pan

        hit = self._pick_point_from_cloud(point)
        if hit is not None:
            # Got a real point off the cloud surface -- use its genuine z.
            map_point = QgsVector3D(hit[0], hit[1], hit[2])
            _log(f"pick_on_map: hit point cloud at map_point={map_point}")
        else:
            # No point cloud data under the click -- try a DEM-enabled raster
            # layer next (see _sample_dem_elevation()), and only fall back to
            # ground level (z=0) if neither source has anything here.
            dem_z = self._sample_dem_elevation(point)
            if dem_z is not None:
                map_point = QgsVector3D(point.x(), point.y(), dem_z)
                _log(f"pick_on_map: no point cloud hit, sampled DEM z={dem_z} at map_point={map_point}")
            else:
                map_point = QgsVector3D(point.x(), point.y(), 0.0)
                _log(f"pick_on_map: no point cloud or DEM hit, ground map_point={map_point}")
        self._focus_map = map_point

        try:
            display_point = self._live_or_headless_canvas3d().mapSettings().mapToWorldCoordinates(map_point)
        except Exception:
            display_point = map_point

        for box in (self.focus_x, self.focus_y, self.focus_z):
            box.blockSignals(True)
        self.focus_x.setValue(display_point.x())
        self.focus_y.setValue(display_point.y())
        self.focus_z.setValue(display_point.z())
        for box in (self.focus_x, self.focus_y, self.focus_z):
            box.blockSignals(False)
        self._update_path_visualization()

    def _on_focus_spinbox_changed(self):
        """Wired to focus_x/y/z's valueChanged -- fires only for a genuine manual
        edit (programmatic updates elsewhere block signals first). Captures the
        map-CRS equivalent of whatever was just typed, using whatever origin is
        current *right now* -- must happen before _update_path_visualization()
        below, since that can end up growing the 3D scene's extent (path_
        visualization.py), which recenters origin and would make this same
        scene-local number mean something different from here on.

        Uses _live_or_headless_canvas3d(), not _get_canvas3d() -- same reason
        _update_path_visualization() avoids _get_canvas3d(): that call creates
        a real 3D view as a side effect if none exists, which would mean
        typing into a focus field opens a 3D view the user never asked for.
        The headless stand-in gives correct map<->scene math with no visible
        canvas needed (see that method's docstring).

        Previously this skipped updating _focus_map entirely whenever no live
        3D canvas existed yet, leaving it stale at whatever it was last set to
        (e.g. by Center on elevation data) even though the spinbox itself now
        showed the freshly typed value -- the two could disagree, and
        anything reading _focus_map afterwards (including a later Preview
        Trajectory/Export, which derives the actual camera target from
        _focus_map, not the spinboxes) would silently use the old value
        instead of what was just typed. Always recomputing it here, with the
        headless fallback, keeps the two in sync unconditionally.
        """
        scene_focus = QgsVector3D(self.focus_x.value(), self.focus_y.value(), self.focus_z.value())
        try:
            self._focus_map = self._live_or_headless_canvas3d().mapSettings().worldToMapCoordinates(scene_focus)
        except Exception:
            self._focus_map = None
        self._update_path_visualization()

    def _current_focus(self, canvas3d):
        """Scene-local focus point for _build_animator() -- re-derived fresh from
        _focus_map (map-CRS, origin-independent) using whatever origin canvas3d
        currently has, rather than trusting focus_x/y/z directly. Those spinboxes
        hold whatever scene-local numbers were valid *when last set*; if the 3D
        scene's origin has moved since (see _focus_map's docstring in __init__),
        reinterpreting those same numbers against the new origin would silently
        target the wrong real-world location -- reported as the camera path/look
        direction ending up nowhere near the point cloud after tweaking params.
        Falls back to reading the spinboxes directly if there's no map-CRS focus
        to derive from yet (e.g. CRS-less point clouds, or focus never set, or
        this is the very first canvas ever created for this session -- nothing
        could have gone stale yet in that case)."""
        if self._focus_map is not None and not _canvas_is_dead(canvas3d):
            try:
                return canvas3d.mapSettings().mapToWorldCoordinates(self._focus_map)
            except Exception as exc:
                # Canvas can go away between the _canvas_is_dead() check above
                # and this call -- fall back to the spinboxes below rather than
                # crash, but log it so a genuine bug doesn't go unnoticed.
                _log(f"_current_focus: mapToWorldCoordinates failed, using spinbox values: {exc}")
        return QgsVector3D(self.focus_x.value(), self.focus_y.value(), self.focus_z.value())

    def _imported_centroid(self):
        """Average scene-local position across every imported keyframe -- used
        as CameraPathAnimator's focus/rotation-pivot in import mode, since
        there's no curve-generation focus point to reuse there. Cheap (plain
        mean over however many keyframes were loaded) and recomputed fresh
        each build rather than cached, so switching to a different imported
        file is automatically picked up."""
        xs = [kf["position"].x() for kf in self._imported_keyframes]
        ys = [kf["position"].y() for kf in self._imported_keyframes]
        zs = [kf["position"].z() for kf in self._imported_keyframes]
        n = len(xs)
        return QgsVector3D(sum(xs) / n, sum(ys) / n, sum(zs) / n)

    def _build_animator(self, output_dir=None, on_finished=None, on_frame=None, canvas3d=None):
        """canvas3d: explicit canvas (or canvas-like settings-provider) to build
        against. Omitted by Preview/Export/Export Trajectory, which resolve a
        real one via self._get_canvas3d() (creating a visible 3D view as a
        side effect if none exists yet -- correct there, since those need a
        real canvas to actually move a camera in or grab frames from).
        _update_path_visualization() passes one explicitly instead -- the
        real self.canvas3d if a 3D view is already open, otherwise a headless,
        invisible _HeadlessCanvas3D stand-in -- so the 2D-only preview never
        forces a 3D view open just from a passive curve-param/rotation edit.
        """
        if self._is_import_mode():
            if not self._imported_keyframes:
                _log("Import mode is selected but no trajectory file has been loaded -- use Browse.")
                return None
            return CameraPathAnimator(
                canvas3d if canvas3d is not None else self._get_canvas3d(),
                None, None, self._imported_centroid(),
                0.0, self._imported_fps,
                output_dir=output_dir,
                make_video=self.make_video_checkbox.isChecked(),
                look_mode="focus",
                imported_keyframes=self._imported_keyframes,
                rotation_x_deg=self.rotation_x.value(),
                rotation_y_deg=self.rotation_y.value(),
                rotation_z_deg=self.rotation_z.value(),
                on_finished=on_finished,
                on_frame=on_frame,
            )

        curve_name = self.curve_combo.currentText()
        kwargs = {name: box.value() for name, box in self.param_boxes.items()}
        resolved_canvas3d = canvas3d if canvas3d is not None else self._get_canvas3d()
        focus = self._current_focus(resolved_canvas3d)
        look_mode = _LOOK_MODES[self.look_mode_combo.currentText()] if curve_name == "Fly Through" else "focus"
        return CameraPathAnimator(
            resolved_canvas3d,
            curve_name,
            kwargs,
            focus,
            self.duration.value(),
            self.fps.value(),
            output_dir=output_dir,
            make_video=self.make_video_checkbox.isChecked(),
            look_mode=look_mode,
            rotation_x_deg=self.rotation_x.value(),
            rotation_y_deg=self.rotation_y.value(),
            rotation_z_deg=self.rotation_z.value(),
            on_finished=on_finished,
            on_frame=on_frame,
        )

    def _set_run_ui_active(self, active):
        """Toggles the run-status line/progress bar together -- called once
        when a Preview Camera Feed/Export run actually starts, and once (via
        _on_run_finished, CameraPathAnimator's on_finished) whether that run
        completed normally or was cut short (superseded by another Preview
        Camera Feed/Export click -- there's no Stop button any more, see
        _stop())."""
        self.status_label.setVisible(active)
        self.status_progress.setVisible(active)
        if not active:
            self.status_label.clear()
            self.status_progress.setValue(0)

    def _on_run_frame(self, frame_idx, frame_count):
        elapsed = time.monotonic() - self._run_start_time
        pct = int(100 * (frame_idx + 1) / frame_count) if frame_count else 0
        self.status_label.setText(f"Frame {frame_idx + 1}/{frame_count} ({pct}%) -- {elapsed:.1f}s elapsed")
        self.status_progress.setMaximum(max(frame_count, 1))
        self.status_progress.setValue(frame_idx + 1)

    def _on_run_finished(self):
        self.animator = None
        self._set_run_ui_active(False)

    def _preview(self):
        self._stop()
        # Same reasoning as _export() below -- these are real layers, and
        # while Preview doesn't grab the screen (so they'd never end up
        # baked into an exported frame the way they would during an actual
        # Export), showing the static camera-position/path/look markers
        # WHILE the camera is actually flying through them is visually
        # confusing (the moving camera stops matching whichever marker
        # you're looking at) and works against the whole point of watching
        # the real camera feed. Hide unconditionally for the run, restore
        # (respecting whatever the checkbox says) via on_finished, same
        # guarantee (fires exactly once, run completes or is interrupted)
        # _export() relies on. Preview Trajectory -- the static, single-
        # frame version -- is unaffected; this only hides during an actual
        # animated run.
        def _finished():
            self._update_path_visualization()
            self._on_run_finished()

        self.path_viz.set_visible(False)
        animator = self._build_animator(on_finished=_finished, on_frame=self._on_run_frame)
        if animator is None:
            self._update_path_visualization()  # nothing started -- restore immediately
            return
        self.animator = animator
        self._run_start_time = time.monotonic()
        self._set_run_ui_active(True)
        self.animator.start()

    def _export_trajectory(self):
        # Deliberately not routed through _build_animator(output_dir=...) -- passing
        # output_dir there flips CameraPathAnimator.export on and makes it create an
        # empty frames/ subfolder for a render that never happens here. Trajectory
        # export is pure curve math (or a pass-through of imported keyframes), no
        # canvas driving/timer/screen-grab involved.
        out_dir = self._resolve_export_dir("trajectory")
        if out_dir is None:
            return
        # Explicit canvas3d (real if open, headless stand-in otherwise, see
        # _live_or_headless_canvas3d()) rather than letting _build_animator()
        # fall back to _get_canvas3d() -- animator.export_trajectory() only
        # uses it for the optional position_map/look_at_map columns (pure
        # coordinate math), not anything needing a real rendered scene, so
        # exporting trajectory data shouldn't force a 3D view open either.
        # Bonus: those map-CRS columns now get written even when no 3D view
        # was ever opened this session -- previously they were silently
        # omitted in that case (CameraPathAnimator.export_trajectory() skips
        # them when canvas3d is None/dead).
        animator = self._build_animator(canvas3d=self._live_or_headless_canvas3d())
        if animator is None:
            return
        json_path, csv_path = animator.export_trajectory(out_dir)
        _log(f"trajectory exported to {json_path} and {csv_path}")
        self._refresh_trajectory_list()

    def _export(self):
        out_dir = self._resolve_export_dir("video")
        if out_dir is None:
            return
        self._stop()
        # Unlike the rubber-band-only version of this preview, these are real
        # layers and would genuinely appear in the 3D view's screen-grabbed
        # frames if left visible -- hide unconditionally for the run, and
        # restore (respecting whatever the checkbox says) via on_finished,
        # which CameraPathAnimator guarantees fires exactly once whether the
        # run completes normally or is interrupted (superseded by another
        # Preview Camera Feed/Export click -- there's no Stop button any
        # more, see _stop()). Also resets the run-status UI (see
        # _on_run_finished) and refreshes the Export tab's video list -- all
        # three need to happen once export ends, whichever way it ends. Note:
        # this refreshes even if the run was stopped early / never produced a
        # finished mp4 -- _refresh_video_list() only lists runs that actually
        # have a camera_path.mp4, so an interrupted run just won't appear.
        def _finished():
            self._update_path_visualization()
            self._on_run_finished()
            self._refresh_video_list()

        self.path_viz.set_visible(False)
        animator = self._build_animator(output_dir=out_dir, on_finished=_finished, on_frame=self._on_run_frame)
        if animator is None:
            self._update_path_visualization()  # nothing started -- restore immediately
            return
        self.animator = animator
        self._run_start_time = time.monotonic()
        self._set_run_ui_active(True)
        self.animator.start()

    def _stop(self):
        """No longer wired to a Stop button -- called internally at the top
        of _preview()/_export() to cut off whatever's already running before
        starting a new one, and from cleanup() on plugin unload."""
        if self.animator is not None:
            self.animator.stop()
            self.animator = None
