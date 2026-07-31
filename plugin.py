import os.path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction


class QuickMapCinePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "QuickMapCine", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("QuickMapCine", self.action)

    def unload(self):
        self.iface.removePluginMenu("QuickMapCine", self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dock is not None:
            # The focus marker and pick tool are parented to the map canvas, not the
            # dock -- they'd otherwise outlive a plugin reload and pile up on screen.
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)

    def toggle_dock(self):
        if self.dock is None:
            from .dockwidget import CameraPathDockWidget
            self.dock = CameraPathDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            # addDockWidget() already shows it -- toggling visibility right after
            # would immediately hide the dock that was just created.
            self.dock.show()
            return
        self.dock.setVisible(not self.dock.isVisible())
