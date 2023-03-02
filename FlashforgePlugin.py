from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from UM.Logger import Logger

# from UM.Extension import Extension

from .FlashforgeDevice import FlashforgeOutputDevice
from .FlashforgeSettings import get_config, init_config
from cura.CuraApplication import CuraApplication


class FlashforgeOutputDevicePlugin(OutputDevicePlugin):
    ##  Called upon launch.
    #   You can use this to make a connection to the device or service, and
    #   register the output device to be displayed to the user.
    def start(self):
        self._application = CuraApplication.getInstance()
        self._application.globalContainerStackChanged.connect(self._checkFlashforgeDevices)
        init_config()
        self._checkFlashforgeDevices()

    def _checkFlashforgeDevices(self):
        global_container_stack = self._application.getGlobalContainerStack()
        if not global_container_stack:
            return

        manager = self.getOutputDeviceManager()

        config = get_config()
        if config:
            Logger.log(
                "d",
                "FlashforgePlugin is active for printer: id:{}, name:{}".format(
                    global_container_stack.getId(), global_container_stack.getName()
                ),
            )
            manager.addOutputDevice(FlashforgeOutputDevice(config))

    ##  Called upon closing.
    #   You can use this to break the connection with the device or service, and
    #   you should unregister the output device to be displayed to the user.
    def stop(self):
        self.getOutputDeviceManager().removeOutputDevice("FlashforgeOutputDevice")
