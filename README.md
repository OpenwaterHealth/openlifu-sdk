# openlifu-sdk

[![Tests](https://github.com/OpenwaterHealth/openlifu-sdk/actions/workflows/test.yml/badge.svg)](https://github.com/OpenwaterHealth/openlifu-sdk/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/openlifu-sdk)](https://pypi.org/project/openlifu-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/openlifu-sdk)](https://pypi.org/project/openlifu-sdk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Openwater LIFU SDK — standalone hardware I/O interface library.

This package provides the low-level communication layer for Openwater LIFU (Low-Intensity Focused Ultrasound) devices, including the TX beamformer module and the HV (high-voltage) controller console.

## Features

- Full USB-serial (VCP) communication with the LIFU TX module and HV console
- Solution programming: delays, apodizations, pulse sequences
- Voltage safety enforcement (duty-cycle / sequence-time / voltage table checks)
- Asynchronous USB hot-plug monitoring
- Firmware update (DFU) for TX modules over USB and I²C
- Demo/test mode (no hardware required) for CI and unit testing
- Bundled libusb DLLs for Windows (win32 / win64)

## Requirements

- Python ≥ 3.12
- `numpy`, `pandas`, `xarray`, `pyserial`, `pyusb`

## Installation

```bash
pip install openlifu-sdk
```

For development (includes linting and testing tools):

```bash
git clone https://github.com/OpenwaterHealth/openlifu-sdk.git
cd openlifu-sdk
pip install -e ".[dev]"
```

## Quick Start

```python
from openlifu_sdk import LIFUInterface

# Connect to hardware (set TX_test_mode=True / HV_test_mode=True for demo mode)
interface = LIFUInterface()
tx_connected, hv_connected = interface.is_device_connected()
print(f"TX: {tx_connected}  HV: {hv_connected}")

# Program a sonication solution
solution = {
    "name": "example",
    "voltage": 20.0,
    "pulse": {"frequency": 500e3, "duration": 2e-5, "amplitude": 1.0},
    "delays": [[0.0] * 64],       # 64-channel delay array (seconds)
    "apodizations": [[1.0] * 64], # 64-channel apodization array
    "sequence": {
        "pulse_interval": 0.1,
        "pulse_count": 10,
        "pulse_train_interval": 1.0,
        "pulse_train_count": 1,
    },
}

with interface:
    interface.set_solution(solution)
    interface.start_sonication()
    # ... wait for sonication to complete ...
    interface.stop_sonication()
```

## Architecture Overview

```
openlifu_sdk/
├── __init__.py            # Public API: LIFUInterface, LIFUInterfaceStatus
├── io/
│   ├── LIFUInterface.py   # High-level orchestration: solution safety, sonication control
│   ├── LIFUTXDevice.py    # TX beamformer: register map, pulse/delay profiles, DFU
│   ├── LIFUHVController.py # HV console: voltage, fans, temperature, LEDs
│   ├── LIFUUart.py        # USB-serial transport: framing, CRC, async hot-plug
│   ├── LIFUConfig.py      # Protocol constants (packet types, commands)
│   ├── LIFUSignal.py      # Qt-style observer/signal pattern
│   ├── LIFUDFU.py         # Firmware update (USB DFU + I2C DFU via UART passthrough)
│   └── LIFUUserConfig.py  # Device user-config wire format (header + JSON)
└── util/
    ├── units.py           # SI unit conversion utilities
    └── annotations.py     # Typed annotation helpers for dataclass fields
```

## Examples

See the [`examples/`](examples/) directory for hardware-targeted scripts covering:
- Basic connectivity and ping (`test_transmitter.py`)
- Register read/write (`test_registers.py`)
- Solution programming (`test_solution.py`)
- Firmware update (`test_fw_update.py`)
- Async mode (`test_async.py`)

## Building a wheel

```bash
pip install build
python -m build
```

## Running tests

```bash
pytest unit-test/ -v
```

With coverage:

```bash
pytest unit-test/ -v --cov=src --cov-report=term-missing
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding conventions, and the pull request process.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a history of notable changes.

## License

MIT — see [LICENSE](LICENSE).
