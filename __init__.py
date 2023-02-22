from . import FlashforgeWifiDevice


def getMetaData():
    return {}


def register(app):
    plugin = FlashforgeWifiDevice.FlashforgeOutputDevicePlugin()
    return {
        "output_device": plugin,
    }
