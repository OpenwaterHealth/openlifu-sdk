# openlifu-sdk

## Disclaimer

CAUTION - Investigational device. Limited by Federal (or United States) law to investigational use. The system described here has not been evaluated by the FDA and is not designed for the treatment or diagnosis of any disease. It is provided AS-IS, with no warranties. User assumes all liability and responsibility for identifying and mitigating risks associated with using this software.

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

Documentation
-------------

Detailed SDK documentation is available in the `docs/` folder. Start with [docs/README.md](docs/README.md) for installation, usage examples, and an API reference.
