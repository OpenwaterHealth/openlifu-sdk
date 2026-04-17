# Transmitter

Manages the UART link to the transmitter board. Inherits all global commands from `OWComponent` and supports TX7332 commands.

**USB defaults:** VID `0x0483`, PID `0x57AF`

## Constructor

```python
Transmitter(
    vid: int = 0x0483,
    pid: int = 0x57AF,
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

## TX7332 Commands

TX7332 commands (`0x20`–`0x2F`) are supported but do not yet have dedicated convenience methods. Use `send()` directly:

```python
from ow_comms import Transmitter
from ow_comms.config import OW_TX7332_STATUS, OW_TX7332_WREG

tx = Transmitter()
tx.connect()

# Read TX7332 status
resp = tx.send(OW_TX7332_STATUS)
print(resp.data.hex())

# Write a register (addr=0, data=register bytes)
resp = tx.send(OW_TX7332_WREG, addr=0, data=bytearray([0x01, 0x02, 0x03, 0x04]))
```

### Available TX7332 command constants

| Constant | Byte | Description |
|----------|------|-------------|
| `OW_TX7332_STATUS` | `0x20` | Get TX7332 status |
| `OW_TX7332_ENUM` | `0x21` | Enumerate TX7332 devices |
| `OW_TX7332_WREG` | `0x22` | Write register |
| `OW_TX7332_RREG` | `0x23` | Read register |
| `OW_TX7332_WBLOCK` | `0x24` | Write block |
| `OW_TX7332_VWREG` | `0x25` | Verified write register |
| `OW_TX7332_VWBLOCK` | `0x26` | Verified write block |
| `OW_TX7332_RBLOCK` | `0x27` | Read block |
| `OW_TX7332_DEVICE_COUNT` | `0x2C` | Get device count |
| `OW_TX7332_DEMO` | `0x2D` | Demo mode |
| `OW_TX7332_RESET` | `0x2F` | Reset TX7332 |

## Low-Level Send

For any command (global or TX7332), use `send()` for blocking or `send_async()` for non-blocking:

```python
# Blocking — returns OWUartPacket or None on timeout
resp = tx.send(command, addr=0, reserved=0, data=None, timeout=5.0)

# Non-blocking (async mode only) — returns packet ID
pid = tx.send_async(command, addr=0, reserved=0, data=None, timeout=5.0)
```

The packet type (`OW_CMD` vs `OW_TX7332`) is resolved automatically from the command byte.

## Signals

| Signal | Args | When |
|--------|------|------|
| `signal_connected` | `(desc, port)` | USB device detected and port opened |
| `signal_disconnected` | `(desc,)` | USB device removed |
| `signal_data_received` | `(desc, OWUartPacket)` | Any packet received (response or unsolicited) |
| `signal_error` | `(desc, packet_id, message)` | Send failure or timeout |

## Example

```python
from ow_comms import Transmitter

tx = Transmitter()
if tx.connect():
    print("Ping:", tx.ping())
    print("Version:", tx.get_version())

    echo, length = tx.echo(b"Hello LIFU!")
    print(f"Echo: {echo.decode()} ({length} bytes)")

    print("HWID:", tx.get_hardware_id())
    tx.close()
```
