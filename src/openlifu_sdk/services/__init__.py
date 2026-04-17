from __future__ import annotations

from importlib import import_module

__all__ = [
    "SolutionService",
]

_EXPORTS = {
    "SolutionService": ("openlifu_sdk.services.SolutionService", "SolutionService"),
}


def __getattr__(name: str):
    module_name, attribute_name = _EXPORTS[name]
    return getattr(import_module(module_name), attribute_name)

