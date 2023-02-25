from pathlib import Path
from typing import cast
import socket
from time import sleep, monotonic
import os
import re

from UM.Application import Application  # To find the scene to get the current g-code to write.
from UM.FileHandler.WriteFileJob import WriteFileJob  # To serialise nodes to text.
from UM.Logger import Logger
from UM.OutputDevice.OutputDevice import OutputDevice  # An interface to implement.
from UM.OutputDevice.OutputDeviceError import WriteRequestFailedError  # For when something goes wrong.
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin  # The class we need to extend.
from UM.Mesh.MeshWriter import MeshWriter
from UM.PluginRegistry import PluginRegistry
from UM.Message import Message
from UM.i18n import i18nCatalog

from io import BytesIO, StringIO

catalog = i18nCatalog("cura")

MSGSIZE = 1024
IPADDR = "192.168.1.50"


class FlashforgeOutputDevicePlugin(OutputDevicePlugin):
    ##  Called upon launch.
    #   You can use this to make a connection to the device or service, and
    #   register the output device to be displayed to the user.
    def start(self):
        self.getOutputDeviceManager().addOutputDevice(FlashforgeOutputDevice())

    ##  Called upon closing.
    #   You can use this to break the connection with the device or service, and
    #   you should unregister the output device to be displayed to the user.
    def stop(self):
        self.getOutputDeviceManager().removeOutputDevice("FlashforgeOutputDevice")


class FlashforgeOutputDevice(OutputDevice):
    def __init__(self):
        super().__init__("FlashforgeOutputDevice")
        self.setName("Flashforge Output Device")
        self.setShortDescription("Flashforge Print")
        self.setDescription("Send job to a Flashforge Printer.")
        self.setIconName("print")

        self.ipaddr = IPADDR

    ##  Called when the user clicks on the button to save to this device.
    #
    #   The primary function of this should be to select the correct file writer
    #   and file format to write to.
    #
    #   \param nodes A list of scene nodes to write to the file. This may be one
    #   or multiple nodes. For instance, if the user selects a couple of nodes
    #   to write it may have only those nodes. If the user wants the entire
    #   scene to be written, it will be the root node. For the most part this is
    #   not your concern, just pass this to the correct file writer.
    #   \param file_name A name for the print job, if available. If no such name
    #   is available but you still need a name in the device, your plug-in is
    #   expected to come up with a name. You could try `uuid.uuid4()`.
    #   \param limit_mimetypes Limit the possible MIME types to use to serialise
    #   the data. If None, no limits are imposed.
    #   \param file_handler What file handler to get the mesh from.
    #   \kwargs Some extra parameters may be passed here if other plug-ins know
    #   for certain that they are talking to your plug-in, not to some other
    #   output device.
    def requestWrite(self, nodes, file_name=None, limit_mimetypes=None, file_handler=None, **kwargs):

        # Make sure post-processing plugin are run on the gcode
        self.writeStarted.emit(self)

        self.outformat = "gcode"
        codeWriter = cast(MeshWriter, PluginRegistry.getInstance().getPluginObject("GCodeWriter"))
        self._stream = StringIO()
        if not codeWriter.write(self._stream, None):
            Logger.log("e", "MeshWriter failed: %s" % codeWriter.getInformation())
            return

        self._stream.seek(0)
        self._gcode = self._stream.getvalue()

        # Add Extruder index to Hotend and Bed Temp Commands
        self._gcode = re.sub(r"(M1(?:40|04) S\d+)(\.0)?(?: T\d)?", r"\1 T0", self._gcode)

        # Remove decimals from fan speed commands
        self._gcode = re.sub(r"(M106 S\d+)\.\d+", r"\1", self._gcode)

        # Save the GCode to disk for debugging
        with open(Path(os.getenv("temp"), "flashforge.gcode"), "w") as gcode_file:
            gcode_file.write(self._gcode)

        self._postData = self._gcode.encode("ascii")

        # Prepare file_name for upload
        if file_name:
            file_name = Path(file_name).name
        else:
            file_name = "%s." % Application.getInstance().getPrintInformation().jobName

        self._file_name = file_name[:10] + "." + self.outformat

        self.startUpload()

    def startUpload(self):

        filesize = len(self._postData)

        header = f"~M28 {filesize} 0:/user/{self._file_name}\r\n"
        footer = "~M29\r\n"
        start = f"~M23 0:/user/{self._file_name}\r\n"

        Logger.log("d", f"File: {self._file_name}")
        Logger.log("d", f"Size: {filesize} bytes")
        Logger.log("d", f"Header: {header}")

        # Staring a TCP socket.
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Connect to the printer server
        address = (self.ipaddr, 8899)
        client.connect(address)

        # # Show a progress message
        # self._message = Message(
        #     catalog.i18nc("@info:progress", "Uploading to {}...").format(self._name),
        #     0,
        #     False,
        #     -1,
        # )
        # self._message.show()

        # Send the file_name to the printer
        client.send(header.encode("ascii"))
        msg = client.recv(MSGSIZE).decode("ascii")
        Logger.log("d", f"[PRINTER]: {msg}")

        client.sendfile(BytesIO(self._postData))

        sleep(0.5)
        client.send(footer.encode("ascii"))

        msg = client.recv(MSGSIZE).decode("ascii")
        Logger.log("d", f"[PRINTER]: {msg}")

        # Start the print job
        client.send(start.encode("ascii"))
        msg = client.recv(MSGSIZE).decode("ascii")
        Logger.log("d", f"[PRINTER]: {msg}")

        client.close()

    def _onProgress(self, progress):
        if self._message:
            self._message.setProgress(progress)
        self.writeProgress.emit(self, progress)

    def _onUploadProgress(self, bytesSent, bytesTotal):
        if bytesTotal > 0:
            self._onProgress(int(bytesSent * 100 / bytesTotal))

    def _onNetworkError(self, reply, error):
        Logger.log("e", repr(error))
        if self._message:
            self._message.hide()
            self._message = None

        errorString = ""
        if reply:
            errorString = reply.errorString()

        message = Message(
            catalog.i18nc("@info:status", "There was a network error: {} {}").format(error, errorString),
            0,
            False,
        )
        message.show()

        self.writeError.emit(self)
        self._resetState()
