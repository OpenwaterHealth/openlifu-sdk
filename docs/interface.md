# OWInterface

Top-level facade that manages both the Transmitter and Console components. Use this as the single entry point for communicating with the LIFU hardware.

## Constructor

```python
OWInterface(
    baudrate: int = 921600,
    timeout: float = 5.0,
    tx_vid: int = 0x0483,
    tx_pid: int = 0x57AF,
    con_vid: int = 0x0483,
    con_pid: int = 0x57A0,
)
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `baudrate` | `921600` | Serial baud rate for both components |
| `timeout` | `5.0` | Per-command response timeout in seconds |
| `tx_vid` | `0x0483` | Transmitter USB Vendor ID |
| `tx_pid` | `0x57AF` | Transmitter USB Product ID |
| `con_vid` | `0x0483` | Console USB Vendor ID |
| `con_pid` | `0x57A0` | Console USB Product ID |

## Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `transmitter` | `Transmitter` | Transmitter component instance |
| `console` | `Console` | Console component instance |

## Methods

### `connect() -> tuple[bool, bool]`

Connect both components. Returns `(tx_ok, con_ok)`.

```python
iface = OWInterface()
tx_ok, con_ok = iface.connect()
```

### `disconnect()`

Disconnect both components.

### `start()`

Enter async mode for both components. Launches monitor, sender, and reader threads on each.

### `stop()`

Leave async mode for both components. Joins threads, drains queues, disconnects.

## Usage Patterns

### Synchronous (scripts)

```python
from ow_comms import OWInterface

iface = OWInterface()
tx_ok, con_ok = iface.connect()

if tx_ok:
    iface.transmitter.ping()
    print(iface.transmitter.get_version())
    iface.transmitter.close()

if con_ok:
    iface.console.ping()
    iface.console.turn_12v_on()
    iface.console.close()
```

### Sync Timeout Note

The synchronous path uses `OWUart.send_packet()` without background threads. A machine-dependent timeout issue was traced to the sync receive loop rather than to transmitter firmware.

Root cause:
- The original sync loop performed repeated short serial reads while waiting for the matching packet.
- On some hosts, especially during TX7332 traffic, valid responses could arrive near the end of the command timeout window or in smaller fragments.
- That made the loop vulnerable to timing out even though the device response was effectively in flight.

Fix:
- The sync receive loop now recalculates the underlying serial timeout from the remaining overall command deadline on each iteration.
- This keeps per-read blocking behavior consistent with the caller's timeout budget and removed the intermittent false timeouts seen across different machines.

### Asynchronous (PyQt applications)

```python
from ow_comms import OWInterface

iface = OWInterface()
iface.start()

# Signals fire on worker threads — bridge to pyqtSignal for UI safety
iface.transmitter.signal_connected.connect(on_tx_connected)
iface.console.signal_data_received.connect(on_console_data)

# Non-blocking sends
iface.transmitter.send_async(0x00)  # ping

# On shutdown
iface.stop()
```

### Individual components

You can also use `Transmitter` or `Console` directly without the facade:

```python
from ow_comms import Transmitter

tx = Transmitter()
tx.connect()
tx.ping()
tx.close()
```
