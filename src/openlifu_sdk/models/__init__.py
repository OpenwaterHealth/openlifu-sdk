from __future__ import annotations

from importlib import import_module

__all__ = ["tx_registers"]


def __getattr__(name: str):
	if name != "tx_registers":
		raise AttributeError(name)
	return import_module("openlifu_sdk.models.tx_registers")

