import re
from typing import Optional
from pathlib import Path

from cura.CuraApplication import CuraApplication
from cura.MachineAction import MachineAction

from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Logger import Logger
from UM.i18n import i18nCatalog

from PyQt6.QtCore import QObject, pyqtSignal as QSignal, pyqtProperty as QProperty, pyqtSlot as QSlot

from .FlashforgeSettings import delete_config, get_config, save_config

catalog = i18nCatalog("cura")


class FlashforgeAction(MachineAction):
    def __init__(self, parent: QObject = None) -> None:
        super().__init__("FlashforgeAction", catalog.i18nc("@action", "Connect Flashforge"))
        self._application = CuraApplication.getInstance()
        self._application.globalContainerStackChanged.connect(self._onGlobalContainerStackChanged)
        ContainerRegistry.getInstance().containerAdded.connect(self._onContainerAdded)

        self._qml_url = Path(Path(__file__).parent, "resources", "FlashforgeConfigUI.qml")

    def _onGlobalContainerStackChanged(self) -> None:
        self.printerSettingsAddrChanged.emit()

    def _onContainerAdded(self, container: "ContainerInterface") -> None:
        # Add this action as a supported action to all machine definitions
        if isinstance(container, DefinitionContainer) and container.getMetaDataEntry("type") == "machine":
            self._application.getMachineActionManager().addSupportedAction(container.getId(), self.getKey())

    printerSettingsAddrChanged = QSignal()

    @QProperty(str, notify=printerSettingsAddrChanged)
    def printerSettingUrl(self) -> Optional[str]:
        s = get_config()
        if s:
            return s.get("address", "")
        return ""

    @QSlot(str)
    def saveConfig(self, address):
        save_config(address)
        Logger.log("d", "config saved")

        # trigger a stack change to reload the output devices
        self._application.globalContainerStackChanged.emit()

    @QSlot()
    def deleteConfig(self):
        if delete_config():
            Logger.log("d", "config deleted")

            # trigger a stack change to reload the output devices
            self._application.globalContainerStackChanged.emit()
        else:
            Logger.log("d", "no config to delete")

    @QSlot(str, result=bool)
    def validUrl(self, newUrl):
        if re.match(r"^((25[0-5]|(2[0-4]|1\d|[1-9]|)\d)\.?\b){4}$", newUrl):
            return True
        return False
