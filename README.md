# openlifu-sdk

Openwater LIFU SDK — standalone hardware I/O interface library.

This package provides the low-level communication layer for Openwater LIFU devices,
including the TX module and HV controller.

## Installation

```bash
pip install openlifu-sdk
```

Or for development:

```bash
pip install -e ".[dev]"
```

## Building a wheel

```bash
pip install build
python -m build
```

## Usage

```python
from openlifu_sdk import LIFUInterface

interface = LIFUInterface()
tx_connected, hv_connected = interface.is_device_connected()
```

## Examples

See the `examples/` directory for usage scripts.
