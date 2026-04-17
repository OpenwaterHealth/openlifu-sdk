
# Import packet-type registration from the shared SDK so LIFU-specific
# commands resolve to the correct packet type without polluting the base SDK.
from ow_comms.component import register_command_packet_types

# ---------------------------------------------------------------------------
# USB IDs
# ---------------------------------------------------------------------------

OW_CONSOLE_PID = 0x57A0
OW_TRANSMITTER_PID = 0x57AF

# ---------------------------------------------------------------------------
# LIFU Specific Command Codes
# ---------------------------------------------------------------------------

# Packet Types
OW_TX7332 = 0xE6
OW_AFE_READ = 0xE7
OW_AFE_SEND = 0xE8
OW_CONTROLLER = 0xEA
OW_POWER = 0xEB


# Controller Commands
OW_CTRL_SET_SWTRIG = 0x13
OW_CTRL_GET_SWTRIG = 0x14
OW_CTRL_START_SWTRIG = 0x15
OW_CTRL_STOP_SWTRIG = 0x16
OW_CTRL_STATUS_SWTRIG = 0x17
OW_CTRL_GET_MODULE_COUNT = 0x1A

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

TX7332_COMMANDS = {
    OW_TX7332_STATUS, OW_TX7332_ENUM, OW_TX7332_WREG, OW_TX7332_RREG,
    OW_TX7332_WBLOCK, OW_TX7332_VWREG, OW_TX7332_VWBLOCK, OW_TX7332_RBLOCK,
    OW_TX7332_DEVICE_COUNT, OW_TX7332_DEMO, OW_TX7332_RESET,
}

CONTROLLER_COMMANDS = {
    OW_CTRL_SET_SWTRIG, OW_CTRL_GET_SWTRIG, OW_CTRL_START_SWTRIG,
    OW_CTRL_STOP_SWTRIG, OW_CTRL_STATUS_SWTRIG, OW_CTRL_GET_MODULE_COUNT,
}

POWER_COMMANDS = {
    OW_POWER_STATUS, OW_POWER_SET_HV, OW_POWER_GET_HV, OW_POWER_HV_ON,
    OW_POWER_HV_OFF, OW_POWER_12V_ON, OW_POWER_12V_OFF, OW_POWER_GET_TEMP1,
    OW_POWER_GET_TEMP2, OW_POWER_SET_FAN, OW_POWER_GET_FAN, OW_POWER_SET_RGB,
    OW_POWER_GET_RGB, OW_POWER_GET_HVON, OW_POWER_GET_12VON, OW_POWER_SET_DACS,
    OW_POWER_VMON, OW_POWER_RAW_DAC, OW_POWER_HV_ENABLE,
}

register_command_packet_types(TX7332_COMMANDS, OW_TX7332)
register_command_packet_types(CONTROLLER_COMMANDS, OW_CONTROLLER)
register_command_packet_types(POWER_COMMANDS, OW_POWER)
