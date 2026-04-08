# Quick start / Usage

Basic usage example:

```python
from openlifu_sdk import LIFUInterface

interface = LIFUInterface()
tx_connected, hv_connected = interface.is_device_connected()
print("TX connected:", tx_connected, "HV connected:", hv_connected)

# Use context manager to ensure proper close
with LIFUInterface() as iface:
    tx_connected, hv_connected = iface.is_device_connected()
    # Load a solution and start sonication (example shape):
    # solution = {...}
    # iface.set_solution(solution)
    # iface.start_sonication()

```

See `examples/` for runnable scripts demonstrating device programming, DFU, testing and more.