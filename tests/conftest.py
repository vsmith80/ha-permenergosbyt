"""Shared test helpers.

Component files are loaded directly via importlib, bypassing
custom_components/permenergosbyt/__init__.py (which imports scheduler.py
and pulls in a large chunk of homeassistant.* - not needed for the
api.py/const.py unit tests). const.py still needs two homeassistant
symbols just to *import* (ConfigEntry, DeviceInfo); when the real
homeassistant package isn't installed, minimal stand-ins are registered
in sys.modules instead of pulling in the full framework.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "permenergosbyt"
FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"


def load_component_module(name: str, filename: str):
    """Import one component file standalone (no package __init__.py involved)."""
    module_name = f"permenergosbyt_test_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, COMPONENT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    # Must be registered before exec: dataclasses resolves deferred
    # (`from __future__ import annotations`) type hints via
    # sys.modules[cls.__module__], which fails on an unregistered module.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def install_minimal_homeassistant_stubs() -> None:
    """Register just enough of homeassistant.* for const.py to import.

    const.py only uses ConfigEntry as a type hint (never instantiates or
    calls it) and DeviceInfo as a plain constructor call - a TypedDict is
    just a dict at runtime, so a builtin dict is a behaviorally exact
    stand-in. Skips itself if a real homeassistant install is importable,
    so this never shadows the real package in an environment that has it.
    """
    if importlib.util.find_spec("homeassistant") is not None:
        return
    if "homeassistant.config_entries" in sys.modules:
        return

    sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    sys.modules["homeassistant.helpers.device_registry"] = device_registry


def read_fixture(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")
