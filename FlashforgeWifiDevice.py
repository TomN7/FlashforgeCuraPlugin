from pathlib import Path
from typing import cast
from enum import Enum
from io import StringIO
import os, re

from PyQt6.QtCore import QThread, pyqtSignal as QSignal
from PyQt6.QtWidgets import QWidget
from PyQt6.QtNetwork import QTcpSocket

from UM.Application import Application  # To find the scene to get the current g-code to write.
from UM.FileHandler.WriteFileJob import WriteFileJob  # To serialise nodes to text.
from UM.Logger import Logger
from UM.OutputDevice.OutputDevice import OutputDevice  # An interface to implement.
from UM.OutputDevice import OutputDeviceError  # For when something goes wrong.
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin  # The class we need to extend.
from UM.Mesh.MeshWriter import MeshWriter
from UM.PluginRegistry import PluginRegistry
from UM.Message import Message
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication

catalog = i18nCatalog("cura")

MSGSIZE = 1460
IPADDR = "192.168.1.50"


class TcpClient(QWidget):
    messageReceived = QSignal(str)
    errorOccured = QSignal(str)
    progressUpdate = QSignal(int)
    sendComplete = QSignal()
    connected = QSignal()

    totalBytesSent = 0
    tranferSize = 0
    delayOnFinish = 0

    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port
        self.socket = QTcpSocket()
        self.socket.readyRead.connect(self.receiveMessage)
        self.socket.errorOccurred.connect(self.handleError)
        self.socket.bytesWritten.connect(self.onProgress)
        self.thread = QThread()
        self.moveToThread(self.thread)
        self.thread.start()

    def __del__(self):
        self.disconnect()

    def connect(self):
        self.socket.connectToHost(self.host, self.port)
        self.connected.emit()

    def disconnect(self):
        self.socket.close()

    def sendMessage(self, message: str):
        self.socket.write(message.encode("ascii"))

    def sendData(self, data: bytes, pauseOnFinish: int = 0):
        self.tranferSize = len(data)
        self.totalBytesSent = 0
        self.delayOnFinish = pauseOnFinish

        transmitted = 0
        while transmitted < len(data):
            transmitted += self.socket.write(data[transmitted : transmitted + MSGSIZE])

    def onProgress(self, payloadBytes):
        self.totalBytesSent += payloadBytes
        self.progressUpdate.emit(self.totalBytesSent)

        if self.tranferSize and self.totalBytesSent >= self.tranferSize:
            self.tranferSize = 0
            if self.delayOnFinish:
                QThread.msleep(self.delayOnFinish)
            self.sendComplete.emit()

    def receiveMessage(self):
        data = self.socket.readAll().data().decode("ascii")
        if data:
            self.messageReceived.emit(data)

    def handleError(self, error):
        self.errorOccured.emit(str(error))


class CommsState(Enum):
    Ready = 0
    SendHeader = 1
    SendFile = 2
    SendFooter = 3
    SendStart = 4


class FlashforgeOutputDevice(OutputDevice):
    def __init__(self):
        super().__init__("FlashforgeOutputDevice")
        self.setName("Flashforge Output Device")
        self.setShortDescription("Flashforge Print")
        self.setDescription("Send job to a Flashforge Printer.")
        self.setIconName("print")

        self.application = CuraApplication.getInstance()
        global_container_stack = self.application.getGlobalContainerStack()
        self._name = global_container_stack.getName()

        self.ipaddr = IPADDR
        self._stage = CommsState.Ready
        self.client = TcpClient(self.ipaddr, 8899)
        self.client.connected.connect(self.onConnect)
        self.client.messageReceived.connect(self.onResponse)
        self.client.errorOccured.connect(self.onNetworkError)
        self.client.progressUpdate.connect(self.onUploadProgress)
        self.client.sendComplete.connect(self.onTransmitComplete)

        self.responseCounter = 0

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

        if self._stage != CommsState.Ready:
            raise OutputDeviceError.DeviceBusyError()

        # Make sure post-processing plugin are run on the gcode
        self.writeStarted.emit(self)

        self.outformat = "gcode"
        codeWriter = cast(MeshWriter, PluginRegistry.getInstance().getPluginObject("GCodeWriter"))
        self._stream = StringIO()
        if not codeWriter.write(self._stream, None):
            Logger.log("e", "MeshWriter failed: %s" % codeWriter.getInformation())
            self.writeError.emit(self)

        self._stream.seek(0)
        self._gcode = self._stream.getvalue()

        # Add Extruder index to Hotend and Bed Temp Commands
        self._gcode = re.sub(r"(M1(?:40|04) S\d+)(\.0)?(?: T\d)?", r"\1 T0", self._gcode)

        # Remove decimals from fan speed commands
        self._gcode = re.sub(r"(M106 S\d+)\.\d+", r"\1", self._gcode)

        # Save the GCode to disk for debugging
        with open(Path(os.getenv("temp"), "flashforge.gcode"), "w") as gcode_file:
            gcode_file.write(self._gcode)

        # Prepare file_name for upload
        if file_name:
            file_name = Path(file_name).name
        else:
            file_name = "%s." % Application.getInstance().getPrintInformation().jobName

        self._postData = self._gcode.encode("ascii")
        self._file_name = file_name.replace("DNX_", "", 1)
        self._file_size = len(self._postData)
        self._header = f"~M28 {self._file_size} 0:/user/{self._file_name}\r\n"
        self._footer = "~M29\r\n"
        self._startCommand = f"~M23 0:/user/{self._file_name}\r\n"

        Logger.log("d", f"File: {self._file_name}")
        Logger.log("d", f"Size: {self._file_size} bytes")

        # show a progress message
        self._message = Message(
            catalog.i18nc("@info:progress", "Uploading to {}...").format(self._name),
            0,
            False,
            progress=0.0,
        )
        self._message.show()
        Logger.log("d", "Connecting to Flashforge...")

        self._stage = CommsState.SendHeader
        self.client.connect()

    def onConnect(self):
        self.client.sendMessage(self._header)

    def onTransmitComplete(self):
        if self._stage == CommsState.SendFile:
            self._stage = CommsState.SendFooter
            self.client.sendMessage(self._footer)

    def onResponse(self, response: str):
        splitResponse = response.splitlines()
        self.responseCounter += len(splitResponse)

        for line in splitResponse:
            Logger.log("d", f"[PRINTER]: {line}")

        if self._stage == CommsState.SendHeader:
            if self.responseCounter >= 3:
                self.responseCounter = 0
                self._stage = CommsState.SendFile
                self.client.sendData(self._postData, 500)

        elif self._stage == CommsState.SendFooter:
            if self.responseCounter >= 3:
                self.responseCounter = 0
                self._stage = CommsState.SendStart
                self.client.sendMessage(self._startCommand)

        elif self._stage == CommsState.SendStart:
            if self.responseCounter >= 4:
                self.responseCounter = 0
                self.reset()

    def onUploadProgress(self, bytesSent):
        if self._stage == CommsState.SendFile and bytesSent > 0:
            progress = float(bytesSent) * 100.0 / self._file_size
            self.writeProgress.emit(self, progress)
            if self._message:
                self._message.setProgress(progress)

    def reset(self):
        self._stage = CommsState.Ready
        self.client.disconnect()
        if self._message:
            self._message.hide()
            self._message = None

    def onNetworkError(self, error):
        Logger.log("e", repr(error))
        if self._message:
            self._message.hide()
            self._message = None
        message = Message(
            catalog.i18nc("@info:status", f"There was a network error: {repr(error)}"),
            0,
            False,
        )
        message.show()

        self.writeError.emit(self)
        self._stage = CommsState.Ready


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
