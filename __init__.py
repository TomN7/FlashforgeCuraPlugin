from . import FlashforgePlugin, FlashforgeAction


def getMetaData():
    return {}


def register(app):
    return {
        "output_device": FlashforgePlugin.FlashforgeOutputDevicePlugin(),
        "machine_action": FlashforgeAction.FlashforgeAction(),
    }
