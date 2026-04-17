from __future__ import annotations

from importlib import import_module

__all__ = [
    "LIFUInterface",
    "SolutionService",
    "SonicationService",
    "LIFUUserConfig",
    "LIFUTransmitter",
    "LIFUConsole",
    "TriggerMode",
]

_EXPORTS = {
    "LIFUInterface": ("openlifu_sdk.io.LIFUInterface", "LIFUInterface"),
    "SolutionService": ("openlifu_sdk.services.SolutionService", "SolutionService"),
    "SonicationService": ("openlifu_sdk.services.SonicationService", "SonicationService"),
    "LIFUUserConfig": ("openlifu_sdk.io.LIFUUserConfig", "LIFUUserConfig"),
    "LIFUTransmitter": ("openlifu_sdk.io.LIFUTransmitter", "LIFUTransmitter"),
    "LIFUConsole": ("openlifu_sdk.io.LIFUConsole", "LIFUConsole"),
    "TriggerMode": ("openlifu_sdk.io.LIFUTransmitter", "TriggerMode"),
}


def __getattr__(name: str):
    module_name, attribute_name = _EXPORTS[name]
    return getattr(import_module(module_name), attribute_name)
