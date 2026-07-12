
# Packet Types
from __future__ import annotations

# Packet structure constants
OW_START_BYTE = 0xAA
OW_END_BYTE = 0xDD
ID_COUNTER = 0  # Initializing the ID counter


OW_ACK = 0xE0
OW_NAK = 0xE1
OW_CMD = 0xE2
OW_RESP = 0xE3
OW_DATA = 0xE4
OW_ONE_WIRE = 0xE5
OW_TX7332 = 0xE6
OW_AFE_READ = 0xE7
OW_AFE_SEND = 0xE8
OW_I2C_PASSTHRU = 0xE9
OW_CONTROLLER = 0xEA
OW_POWER = 0xEB
OW_ONEWIRE_RESP = 0xEC
OW_ERROR = 0xEF

OW_SUCCESS = 0x00
OW_UNKNOWN_COMMAND = 0xFC
OW_BAD_CRC = 0xFD
OW_INVALID_PACKET = 0xFE
OW_UNKNOWN_ERROR = 0xFF

# Global Commands
OW_CMD_PING = 0x00
OW_CMD_PONG = 0x01
OW_CMD_VERSION = 0x02
OW_CMD_ECHO = 0x03
OW_CMD_TOGGLE_LED = 0x04
OW_CMD_HWID = 0x05
OW_CMD_GET_TEMP = 0x06
OW_CMD_GET_AMBIENT = 0x07
OW_CMD_ASYNC = 0x09
OW_CMD_USR_CFG = 0x0A
OW_CMD_CLEAR_CONFIG = 0x0B  # Phase 2: chain broadcast to drop stale I2C addresses
OW_CMD_DISCOVERY = 0x0C
OW_CMD_DFU = 0x0D
OW_CMD_NOP = 0x0E
OW_CMD_RESET = 0x0F

# Node operating mode reported by OW_CTRL_GET_MODULE_MODE / discovery (Phase 2)
NODE_MODE_UNKNOWN = 0x00
NODE_MODE_APP = 0x01          # module is running application firmware
NODE_MODE_BOOTLOADER = 0x02   # module is in the secure bootloader (I2C DFU)

# Controller Commands
OW_CTRL_SET_SWTRIG = 0x13
OW_CTRL_GET_SWTRIG = 0x14
OW_CTRL_START_SWTRIG = 0x15
OW_CTRL_STOP_SWTRIG = 0x16
OW_CTRL_STATUS_SWTRIG = 0x17
OW_CTRL_GET_MODULE_COUNT = 0x1A
OW_CTRL_GET_MODULE_MODE = 0x1B  # Phase 2: per-module NodeMode (app vs bootloader)
OW_CTRL_ENUMERATE = 0x1C        # Phase 2: re-run clear-config + discovery walk

# TX7332 Commands
OW_TX7332_STATUS = 0x20
OW_TX7332_ENUM = 0x21
OW_TX7332_WREG = 0x22
OW_TX7332_RREG = 0x23
OW_TX7332_WBLOCK = 0x24
OW_TX7332_VWREG = 0x25
OW_TX7332_VWBLOCK = 0x26
OW_TX7332_RBLOCK = 0x27
OW_TX7332_DEVICE_COUNT = 0x2C
OW_TX7332_DEMO = 0x2D
OW_TX7332_RESET = 0x2F

# Power Commands
OW_POWER_STATUS = 0x30
OW_POWER_SET_HV = 0x31
OW_POWER_GET_HV = 0x32
OW_POWER_HV_ON = 0x33
OW_POWER_HV_OFF = 0x34
OW_POWER_12V_ON = 0x35
OW_POWER_12V_OFF = 0x36
OW_POWER_GET_TEMP1 = 0x37
OW_POWER_GET_TEMP2 = 0x38
OW_POWER_SET_FAN = 0x39
OW_POWER_GET_FAN = 0x3A
OW_POWER_SET_RGB = 0x3B
OW_POWER_GET_RGB = 0x3C
OW_POWER_GET_HVON = 0x3D
OW_POWER_GET_12VON = 0x3E
OW_POWER_SET_DACS = 0x3F
OW_POWER_VMON = 0x40
OW_POWER_RAW_DAC = 0x41
OW_POWER_HV_ENABLE = 0x42

# ---------------------------------------------------------------------------
# Command sets – used by LIFU to validate commands per component
# ---------------------------------------------------------------------------

GLOBAL_COMMANDS = {
    OW_CMD_PING, OW_CMD_PONG, OW_CMD_VERSION, OW_CMD_ECHO,
    OW_CMD_TOGGLE_LED, OW_CMD_HWID, OW_CMD_GET_TEMP, OW_CMD_GET_AMBIENT,
    OW_CMD_ASYNC, OW_CMD_USR_CFG, OW_CMD_DISCOVERY, OW_CMD_DFU,
    OW_CMD_NOP, OW_CMD_RESET,
}

TX7332_COMMANDS = {
    OW_TX7332_STATUS, OW_TX7332_ENUM, OW_TX7332_WREG, OW_TX7332_RREG,
    OW_TX7332_WBLOCK, OW_TX7332_VWREG, OW_TX7332_VWBLOCK, OW_TX7332_RBLOCK,
    OW_TX7332_DEVICE_COUNT, OW_TX7332_DEMO, OW_TX7332_RESET,
}

CONTROLLER_COMMANDS = {
    OW_CTRL_SET_SWTRIG, OW_CTRL_GET_SWTRIG, OW_CTRL_START_SWTRIG,
    OW_CTRL_STOP_SWTRIG, OW_CTRL_STATUS_SWTRIG, OW_CTRL_GET_MODULE_COUNT,
    OW_CTRL_GET_MODULE_MODE, OW_CTRL_ENUMERATE,
}

POWER_COMMANDS = {
    OW_POWER_STATUS, OW_POWER_SET_HV, OW_POWER_GET_HV, OW_POWER_HV_ON,
    OW_POWER_HV_OFF, OW_POWER_12V_ON, OW_POWER_12V_OFF, OW_POWER_GET_TEMP1,
    OW_POWER_GET_TEMP2, OW_POWER_SET_FAN, OW_POWER_GET_FAN, OW_POWER_SET_RGB,
    OW_POWER_GET_RGB, OW_POWER_GET_HVON, OW_POWER_GET_12VON, OW_POWER_SET_DACS,
    OW_POWER_VMON, OW_POWER_RAW_DAC, OW_POWER_HV_ENABLE,
}

TRIGGER_MODE_SEQUENCE = 0
TRIGGER_MODE_CONTINUOUS = 1
TRIGGER_MODE_SINGLE = 2

TRIGGER_STATUS_READY = 0
TRIGGER_STATUS_RUNNING = 1
TRIGGER_STATUS_ERROR = 2
TRIGGER_STATUS_NOT_CONFIGURED = 3

HW_ID_DATA_LENGTH = 12

OW_VID = 0x0483
OW_CONSOLE_PID = 0x57A0
OW_TRANSMITTER_PID = 0x57AF

# Per-command UART round-trip timeout. Real round trips are sub-millisecond
# for most commands; the generous default is here to absorb Python GIL /
# thread-scheduler jitter when the SDK is driven from a heavily threaded
# host (Qt UI + qasync + telemetry poll thread + sender/reader/monitor
# threads). 1.0 s was historically tight enough that scheduling slop on
# Windows under GUI load occasionally manifested as spurious
# LIFUCommunicationError timeouts; 2.0 s comfortably absorbs that without
# noticeably delaying legitimate failures.
DEFAULT_TIMEOUT = 2.0
USB_POLL_INTERVAL = 0.5     # seconds – USB presence polling in async mode
MAX_DATA_LEN = 4096         # maximum payload bytes (sanity guard)
HW_ID_DATA_LENGTH = 12      # hardware ID is 12 bytes

SETTLE_TIME_HV_ON = 5
SETTLE_TIME_HV_OFF = 15

TEMPERATURE_DATA_LENGTH = 4  # temperature responses are 4 bytes (float32)

# ---------------------------------------------------------------------------
# LIFU SDK error codes
#
# These stable numeric codes are attached to every :class:`LIFUError`
# raised by the I/O layer so that calling applications can programmatically
# distinguish failure modes (e.g. a timeout vs. a device NAK) without
# string-matching exception messages.
#
# Codes are grouped by tens:
#   10xx – transport / connection errors
#   20xx – device-reported errors (firmware returned OW_ERROR)
#   30xx – protocol / response-parsing errors
#   40xx – higher-level operation failures
# ---------------------------------------------------------------------------

# Transport / connection
LIFU_ERR_NOT_CONNECTED        = 1001  # serial port not open
LIFU_ERR_SEND_FAILED          = 1002  # OS/serial error while transmitting
LIFU_ERR_RESPONSE_TIMEOUT     = 1003  # no response received in time
LIFU_ERR_HARDWARE_IN_USE      = 1004  # another live process owns the hardware interface

# Device-reported
LIFU_ERR_DEVICE_NAK           = 2001  # firmware returned OW_ERROR packet

# Protocol / response parsing
LIFU_ERR_BAD_PAYLOAD_LENGTH   = 3001  # payload length not what we expected
LIFU_ERR_BAD_PAYLOAD_FORMAT   = 3002  # malformed JSON / struct / encoding
LIFU_ERR_EMPTY_RESPONSE       = 3003  # response had no payload when one was required

# Higher-level operations
LIFU_ERR_HV_NOT_SETTLED       = 4001  # HV rail failed to settle in time
LIFU_ERR_MODULE_COUNT_MISMATCH = 4002 # detected modules != requested modules
LIFU_ERR_SOLUTION_INVALID     = 4003  # solution failed safety check
LIFU_ERR_SONICATION_FAILED    = 4004  # start/stop sonication did not complete cleanly
LIFU_ERR_NO_TRIGGER_STATUS = 4005  # sonication trigger status could not be determined after sonication

LIFU_ERROR_MESSAGES: dict[int, str] = {
    LIFU_ERR_NOT_CONNECTED:        "Device is not connected",
    LIFU_ERR_SEND_FAILED:          "Failed to transmit packet to device",
    LIFU_ERR_RESPONSE_TIMEOUT:     "Timed out waiting for device response",
    LIFU_ERR_HARDWARE_IN_USE:      "Hardware interface is already in use by another process",
    LIFU_ERR_DEVICE_NAK:           "Device returned an error response",
    LIFU_ERR_BAD_PAYLOAD_LENGTH:   "Device response payload has unexpected length",
    LIFU_ERR_BAD_PAYLOAD_FORMAT:   "Device response payload is malformed",
    LIFU_ERR_EMPTY_RESPONSE:       "Device returned an empty response",
    LIFU_ERR_HV_NOT_SETTLED:       "High-voltage rail failed to settle",
    LIFU_ERR_MODULE_COUNT_MISMATCH:"Detected module count does not match requested",
    LIFU_ERR_SOLUTION_INVALID:     "Solution failed safety validation",
    LIFU_ERR_SONICATION_FAILED:    "Sonication start/stop did not complete successfully",
}