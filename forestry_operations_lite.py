from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QToolBar

from .forestry_operations_lite_dockwidget import ForestryOperationsLiteDockWidget
import os.path


class ForestryOperationsLite:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr("&Forestry Operations Lite")
        self.action_object_name = "forestry_operations_lite_action"
        self.plugin_is_active = False
        self.dockwidget = None

        locale = QSettings().value("locale/userLocale")[0:2]
        locale_path = os.path.join(
            self.plugin_dir, "i18n", "forestry_operations_lite_{}.qm".format(locale)
        )
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        return QCoreApplication.translate("ForestryOperationsLite", message)

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.setObjectName(self.action_object_name)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addVectorToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToVectorMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):

        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.add_action(
            icon_path,
            text=self.tr("Forestry Operations Lite"),
            callback=self.run,
            add_to_toolbar=True,
            parent=self.iface.mainWindow(),
        )

    def on_close_plugin(self):
        self.dockwidget.closingPlugin.disconnect(self.on_close_plugin)
        self.plugin_is_active = False

    def unload(self):
        if self.dockwidget is not None:
            self.dockwidget.close()
            self.dockwidget = None
        for action in self.actions:
            self.iface.removePluginVectorMenu(self.menu, action)
            self.iface.removeVectorToolBarIcon(action)
        self.actions = []

    def run(self):
        if not self.plugin_is_active:
            self.plugin_is_active = True
            if self.dockwidget is None:
                self.dockwidget = ForestryOperationsLiteDockWidget(self.iface)
            self.dockwidget.closingPlugin.connect(self.on_close_plugin)
            self.dockwidget.initialize_window_mode()
