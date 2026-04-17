from __future__ import annotations

from importlib import import_module

__all__ = [
    "LIFUInterface",
    "SolutionService",
    "SonicationService",
    "TriggerMode",
    "LIFUUserConfig",
]

_EXPORTS = {
    "LIFUInterface": ("openlifu_sdk.io.LIFUInterface", "LIFUInterface"),
    "SolutionService": ("openlifu_sdk.services.SolutionService", "SolutionService"),
    "SonicationService": ("openlifu_sdk.services.SonicationService", "SonicationService"),
    "TriggerMode": ("openlifu_sdk.io.LIFUTransmitter", "TriggerMode"),
    "LIFUUserConfig": ("openlifu_sdk.io.LIFUUserConfig", "LIFUUserConfig"),
}

def __getattr__(name: str):
    module_name, attribute_name = _EXPORTS[name]
    return getattr(import_module(module_name), attribute_name)

