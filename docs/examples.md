# Examples

The repository contains many example scripts in the top-level `examples/` directory. Highlights:

- `demo.py` — basic device demo and interaction.
- `test_comms.py`, `test_async.py` — communication and async-mode examples.
- `test_fw_update.py`, `test_tx_dfu.py` — firmware update / DFU examples.
- `test_registers.py`, `test_user_config.py` — register/configuration utilities.

Run examples with the repository in editable mode or after installing the package. Example:

```bash
python examples/demo.py
```

Read each example header for specific device assumptions (connected hardware, external power, test modes).