# Console

Manages the UART link to the console board. Inherits all global commands from `OWComponent` and adds Controller and Power commands.

**USB defaults:** VID `0x0483`, PID `0x57A0`

## Constructor

```python
Console(
    vid: int = 0x0483,
    pid: int = 0x57A0,
    baudrate: int = 921600,
    timeout: float = 5.0,
)
```

## Lifecycle Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `connect()` | `bool` | Open the serial port (auto-discovers by VID/PID) |
| `disconnect()` | — | Close the serial port |
| `start()` | — | Enter async mode (monitor + sender + reader threads) |
| `stop()` | — | Exit async mode and join threads |
| `close()` | — | Stop + disconnect (convenience) |
| `is_connected()` | `bool` | Check if serial port is open |

## Global Commands

Inherited from `OWComponent`. Available on both Transmitter and Console.

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `ping()` | `0x00` | `bool` | Verify device connectivity |
| `get_version()` | `0x02` | `str` | Firmware version (`"vX.Y.Z"`) |
| `echo(echo_data)` | `0x03` | `(bytes, int)` | Round-trip data, returns `(data, length)` |
| `get_hardware_id()` | `0x05` | `str \| None` | Hardware ID (`"DEAD-BEEF-CAFE-..."`) |
| `toggle_led()` | `0x04` | `bool` | Toggle onboard LED |
| `get_temperature()` | `0x06` | `float \| None` | Board temperature in °C |
| `get_ambient()` | `0x07` | `float \| None` | Ambient temperature in °C |

## Power Commands

### Temperature

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `get_temperature1()` | `0x37` | `float` | Temperature sensor 1 in °C (2 decimal places) |
| `get_temperature2()` | `0x38` | `float` | Temperature sensor 2 in °C (2 decimal places) |

```python
temp1 = console.get_temperature1()
temp2 = console.get_temperature2()
print(f"Sensors: {temp1}°C / {temp2}°C")
```

### 12V Rail

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `turn_12v_on()` | `0x35` | `bool` | Enable 12V rail |
| `turn_12v_off()` | `0x36` | `bool` | Disable 12V rail |
| `get_12v_status()` | `0x3E` | `bool` | `True` if 12V is on |

```python
console.turn_12v_on()
print("12V on:", console.get_12v_status())
console.turn_12v_off()
```

### High Voltage (HV) Rail

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `turn_hv_on()` | `0x33` | `bool` | Enable HV output |
| `turn_hv_off()` | `0x34` | `bool` | Disable HV output |
| `get_hv_status()` | `0x3D` | `bool` | `True` if HV is on |
| `set_hv(voltage)` | `0x31` | `bool` | Set HV voltage (float, little-endian) |
| `get_hv()` | `0x32` | `float` | Read current HV setting |

```python
console.set_hv(48.0)
console.turn_hv_on()
print("HV voltage:", console.get_hv())
print("HV on:", console.get_hv_status())
console.turn_hv_off()
```

### Fan Control

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `set_fan(speed)` | `0x39` | `bool` | Set fan speed (0–255) |
| `get_fan()` | `0x3A` | `int` | Read current fan speed |

```python
console.set_fan(128)
print("Fan speed:", console.get_fan())
```

### RGB LED

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `set_rgb(state)` | `0x3B` | `bool` | Set RGB LED state (0=OFF, 1=RED, 2=BLUE, 3=GREEN) |
| `get_rgb()` | `0x3C` | `int` | Get current RGB state (-1 on error) |

```python
console.set_rgb(3)  # green
state = console.get_rgb()  # 0=OFF, 1=RED, 2=BLUE, 3=GREEN
```

### Status / Monitoring

| Method | Command | Returns | Description |
|--------|---------|---------|-------------|
| `get_power_status()` | `0x30` | `bytes` | Raw power status payload |
| `get_voltage_monitor()` | `0x40` | `list[dict]` | 8-channel ADC readings (see below) |

#### `get_voltage_monitor()` return format

Returns a list of 8 dicts, one per channel:

```python
[
    {"channel": 0, "raw_adc": 2048, "voltage": 12.5, "converted_voltage": 25.0},
    {"channel": 1, "raw_adc": 2100, "voltage": 13.1, "converted_voltage": 26.2},
    ...
]
```

| Key | Type | Description |
|-----|------|-------------|
| `channel` | `int` | Channel index (0–7) |
| `raw_adc` | `int` | Raw ADC reading (uint16) |
| `voltage` | `float` | Voltage in volts (3 decimal places) |
| `converted_voltage` | `float` | Converted voltage (3 decimal places) |

## Controller Commands

Controller commands (`0x13`–`0x1A`) are supported but do not yet have dedicated convenience methods. Use `send()` directly:

```python
from ow_comms.config import OW_CTRL_START_SWTRIG, OW_CTRL_STOP_SWTRIG, OW_CTRL_GET_MODULE_COUNT

# Start software trigger
console.send(OW_CTRL_START_SWTRIG)

# Get module count
resp = console.send(OW_CTRL_GET_MODULE_COUNT)
print("Modules:", resp.data[0] if resp else "N/A")

# Stop software trigger
console.send(OW_CTRL_STOP_SWTRIG)
```

### Available Controller command constants

| Constant | Byte | Description |
|----------|------|-------------|
| `OW_CTRL_SET_SWTRIG` | `0x13` | Set software trigger parameters |
| `OW_CTRL_GET_SWTRIG` | `0x14` | Get software trigger parameters |
| `OW_CTRL_START_SWTRIG` | `0x15` | Start software trigger |
| `OW_CTRL_STOP_SWTRIG` | `0x16` | Stop software trigger |
| `OW_CTRL_STATUS_SWTRIG` | `0x17` | Get software trigger status |
| `OW_CTRL_GET_MODULE_COUNT` | `0x1A` | Get connected module count |

## Low-Level Send

```python
# Blocking — returns OWUartPacket or None on timeout
resp = console.send(command, addr=0, reserved=0, data=None, timeout=5.0)

# Non-blocking (async mode only) — returns packet ID
pid = console.send_async(command, addr=0, reserved=0, data=None, timeout=5.0)
```

The packet type (`OW_CMD`, `OW_CONTROLLER`, or `OW_POWER`) is resolved automatically from the command byte.

## Signals

| Signal | Args | When |
|--------|------|------|
| `signal_connected` | `(desc, port)` | USB device detected and port opened |
| `signal_disconnected` | `(desc,)` | USB device removed |
| `signal_data_received` | `(desc, OWUartPacket)` | Any packet received (response or unsolicited) |
| `signal_error` | `(desc, packet_id, message)` | Send failure or timeout |

## Example

```python
from ow_comms import Console

con = Console()
if con.connect():
    print("Ping:", con.ping())
    print("Version:", con.get_version())
    print("HWID:", con.get_hardware_id())

    con.turn_12v_on()
    print("12V:", con.get_12v_status())
    print("Temp1:", con.get_temperature1(), "°C")
    print("Temp2:", con.get_temperature2(), "°C")

    con.set_fan(200)
    con.set_rgb(0, 0, 255)

    con.turn_12v_off()
    con.close()
```
