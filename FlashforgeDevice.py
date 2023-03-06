from pathlib import Path
from typing import cast
from enum import Enum
from io import StringIO
import os, re

from PyQt6.QtCore import QThread, pyqtSignal as QSignal, QByteArray, QBuffer, QIODevice
from PyQt6.QtWidgets import QWidget
from PyQt6.QtNetwork import QTcpSocket
from PyQt6.QtGui import QImage

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
from cura.Snapshot import Snapshot

catalog = i18nCatalog("cura")

MSGSIZE = 1460

# Helper function that extracts values from gcode to add to the binary header.
def getValue(line, key, default=None):
    if key not in line:
        return default
    else:
        subPart = line[line.find(key) + len(key) :]
        m = re.search("^-?[0-9]+\\.?[0-9]*", subPart)
    try:
        return float(m.group(0))
    except:
        return default


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
    CheckStatus = 5


class FlashforgeOutputDevice(OutputDevice):
    def __init__(self, config):
        self.application = CuraApplication.getInstance()
        global_container_stack = self.application.getGlobalContainerStack()
        printername = global_container_stack.getName()

        super().__init__(printername)
        self.setName(printername)
        self.setShortDescription("Flashforge Print")
        self.setDescription("Send job to a Flashforge Printer.")
        self.setIconName("print")

        self.ipaddr = config.get("address", "0.0.0.0")
        self._stage = CommsState.Ready
        self.client = TcpClient(self.ipaddr, 8899)
        self.client.connected.connect(self.onConnect)
        self.client.messageReceived.connect(self.onResponse)
        self.client.errorOccured.connect(self.onNetworkError)
        self.client.progressUpdate.connect(self.onUploadProgress)
        self.client.sendComplete.connect(self.onTransmitComplete)

        self.responseCounter = 0
        self._message = None

        Logger.log("d", f"New Flashforge created | IP: {self.ipaddr}")

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
        self.formatGCode()

        # Prepare file_name for upload
        if file_name:
            file_name = Path(file_name).name
        else:
            file_name = "%s." % self.application.getPrintInformation().jobName

        self._attempts = 0
        if self._gxcode:
            self._postData = self._gxcode
            self.outformat = "gx"
        else:
            self._postData = self._gcode.encode("ascii")
        self._file_name = file_name.replace("DNX_", "", 1) + "." + self.outformat
        self._file_size = len(self._postData)
        self._header = f"~M28 {self._file_size} 0:/user/{self._file_name}\r\n"
        self._footer = "~M29\r\n"
        self._startCommand = f"~M23 0:/user/{self._file_name}\r\n"
        self._statusCommand = "~M119\r\n"

        Logger.log("d", f"File: {self._file_name}")
        Logger.log("d", f"Size: {self._file_size} bytes")

        Logger.log("d", "Connecting to Flashforge...")
        self.client.connect()

    def formatGCode(self):
        # Add Extruder index to Hotend and Bed Temp Commands
        self._gcode = re.sub(r"(M1(?:40|04) S\d+)(?:\.\d+)?(?: T\d)?", r"\1 T0", self._gcode)

        # Remove decimals from fan speed commands
        self._gcode = re.sub(r"(M106 S\d+)\.\d+", r"\1", self._gcode)

        # Change all G0 commands to G1 to match Flashprint
        self._gcode = re.sub(r"^G0 (.*)", r"G1 \1", self._gcode)

        # Save the GCode to disk for debugging
        with open(Path(os.getenv("temp"), "flashforge.gcode"), "w") as gcode_file:
            gcode_file.write(self._gcode)

        printInfo = self.application.getPrintInformation()

        try:
            printtime = int(re.search(r";TIME:(\d+)", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            printtime = 0

        try:
            materialLength1 = int(1000 * printInfo.materialLengths[1])
        except IndexError:
            materialLength1 = 0

        try:
            resolution = float(re.search(r";Layer height: ([\d\.]+)", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            resolution = 0.0

        try:
            bedtemp = int(re.search(r"M140 S(\d+) T0", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            bedtemp = 0

        try:
            nozzletemp0 = int(re.search(r"M104 S(\d+) T0", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            nozzletemp0 = 0

        try:
            nozzletemp1 = int(re.search(r"M104 S(\d+) T1", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            nozzletemp1 = 0

        try:
            layercount = int(re.search(r";LAYER_COUNT:(\d+)", self._gcode).group(1))
        except Exception as ex:
            Logger.debug(f"Regex Exception: {ex}")
            layercount = 0

        # Generate Thumbnail
        Logger.debug("Creating thumbnail image...")
        thumb = None

        try:
            thumb = Snapshot.snapshot(width=320, height=320).convertToFormat(QImage.Format.Format_RGB666)
            thumbdata = QByteArray()
            thumbbuf = QBuffer(thumbdata)
            thumbbuf.open(QIODevice.OpenModeFlag.WriteOnly)
            thumb.save(thumbbuf, format="BMP")
            thumbsize = thumbdata.length()

            Logger.debug("Creating GX File Header")

            self._gxcode = bytearray("xgcode 1.0\n\0".encode("ascii"))
            self._gxcode += bytes.fromhex("00000000")  # Unknown
            self._gxcode += (58).to_bytes(4, "little")  # Pointer to start of Thumbnail
            self._gxcode += (58 + thumbsize).to_bytes(4, "little")  # Pointer to start of GCode
            self._gxcode += (58 + thumbsize).to_bytes(4, "little")  # Pointer to start of GCode

            # Offset 0x1C: Print Time, Seconds
            Logger.debug(f"Print Time: {printtime} Seconds")
            self._gxcode += printtime.to_bytes(4, "little")

            # Offset 0x20: Main Extruder Filament use, mm
            materialLength0 = int(1000 * printInfo.materialLengths[0])
            Logger.debug(f"Material Length 0: {materialLength0} mm")
            self._gxcode += materialLength0.to_bytes(4, "little")

            # Offset 0x24: Alternate Extruder Filament use, mm
            Logger.debug(f"Material Length 1: {materialLength1} mm")
            self._gxcode += materialLength1.to_bytes(4, "little")

            # Offset 0x28: Multi-extruder type - flashprint uses 0x01 on my machine
            self._gxcode += int(1).to_bytes(2, "little")

            # Offset 0x2A: Layer Height, microns
            resolutionMicron = int(resolution * 1000)
            Logger.debug(f"Layer Height: {resolutionMicron} micron")
            self._gxcode += resolutionMicron.to_bytes(2, "little")

            # Offset 0x2C: Unknown
            self._gxcode += bytes.fromhex("0000")

            # Offset 0x2E: Perimeter Shell Count (assumed 3, until I can figure out how to extract it)
            self._gxcode += int(3).to_bytes(2, "little")

            # offset 0x30: Print Speed, mm/s (assumed 0 until I can figure out how to extract it)
            self._gxcode += int(0).to_bytes(2, "little")

            # Offset 0x32: Initial Bed Temperature, °C
            Logger.debug(f"Bed Temperature: {bedtemp} °C")
            self._gxcode += bedtemp.to_bytes(2, "little")

            # Offset 0x34: Initial Main Extruder Temperature, °C
            Logger.debug(f"Extruder Temperature 0: {nozzletemp0} °C")
            self._gxcode += nozzletemp0.to_bytes(2, "little")

            # Offset 0x36: Initial Alternate Extruder Temperature, °C
            Logger.debug(f"Extruder Temperature 1: {nozzletemp1} °C")
            self._gxcode += nozzletemp1.to_bytes(2, "little")

            # Offset 0x38: Unknown
            self._gxcode += bytes.fromhex("FEFF")

            Logger.debug(f"Layer Count: {layercount}")

            # Offset 0x3A: Thumbnail
            Logger.debug("Inserting Thumbnail to GX File")
            self._gxcode += thumbdata.data()

            # Add some print comments that the machine might be able to read
            Logger.debug("Adding Metadata to GX File")
            self._gxcode += (";machine_type: Adventurer 4 Series\r\n").encode("ascii")  # Todo: Extract from Cura
            self._gxcode += (f";right_extruder_material: {printInfo.materialNames[0]}\r\n").encode("ascii")
            self._gxcode += (f";right_extruder_temperature: {nozzletemp0}\r\n").encode("ascii")
            self._gxcode += (f";platform_temperature: {bedtemp}\r\n").encode("ascii")
            self._gxcode += (f";layer_height: {resolution}\r\n").encode("ascii")
            self._gxcode += (f";layer_count: {layercount}\r\n").encode("ascii")
            # self._gxcode += (f";base_print_speed: {printSpeed}\r\n").encode("ascii")
            # self._gxcode += (f";travel_speed: {travelSpeed}\r\n").encode("ascii")
            self._gxcode += (";start gcode\r\n").encode("ascii")

            # Replace all Layer comments to flashprint format (to increment progress bar)
            gxcode = re.sub(r";LAYER:\d+", f";layer:{resolution}", self._gcode)

            # Finally attach the GCode
            Logger.debug("Attaching GCode to GX File")
            self._gxcode += gxcode.encode("ascii")

        except Exception:
            Logger.logException("w", "Failed to Generate GX File")
            self._gxcode = None

    def onConnect(self):
        self.startTransfer()

    def startTransfer(self):
        # show a progress message
        self._attempts += 1
        self._message = Message(
            catalog.i18nc("@info:progress", f"Uploading to {self._name} (Attempt {self._attempts})..."),
            0,
            False,
            progress=0.0,
        )
        self._message.show()
        self._stage = CommsState.SendHeader
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
                self.client.sendData(self._postData, 750)

        elif self._stage == CommsState.SendFooter:
            if self.responseCounter >= 3:
                self.responseCounter = 0
                self._stage = CommsState.SendStart
                self.client.sendMessage(self._startCommand)

        elif self._stage == CommsState.SendStart:
            if self.responseCounter >= 4:
                self.responseCounter = 0
                self._stage = CommsState.CheckStatus
                QThread.msleep(1000)
                self.client.sendMessage(self._statusCommand)

        elif self._stage == CommsState.CheckStatus:
            if self.responseCounter >= 8:
                self.responseCounter = 0
                if "BUILDING_FROM_SD" in splitResponse[2]:
                    self.reset()
                    self.client.disconnect()
                elif "PAUSED" in splitResponse[2]:
                    QThread.msleep(5000)
                    self.client.sendMessage(self._statusCommand)
                else:
                    self.reset()
                    if self._attempts < 10:
                        self.startTransfer()

    def onUploadProgress(self, bytesSent):
        if self._stage == CommsState.SendFile and bytesSent > 0:
            progress = float(bytesSent) * 100.0 / self._file_size
            self.writeProgress.emit(self, progress)
            if self._message:
                self._message.setProgress(progress)

    def reset(self):
        self._stage = CommsState.Ready
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
