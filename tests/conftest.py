"""Test harness for the scheduling code.

The full integration needs the core Roborock integration and python-roborock,
which are not required to test schedules. The package is registered without
running its __init__, and the few modules that would pull those in (entity,
raw_commands, runtime) are replaced by minimal stand-ins before services.py
is imported.

Run (needs pytest-homeassistant-custom-component):

    pytest tests
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "custom_components.roborock_live_map"


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    if PKG in sys.modules:
        return
    ccs = _module("custom_components", __path__=[str(ROOT / "custom_components")])
    pkg = _module(PKG, __path__=[str(ROOT / "custom_components" / "roborock_live_map")])
    ccs.roborock_live_map = pkg

    async def run_command(action, description):
        return await action

    class VacuumRuntime:  # stand-in: only what the WebSocket commands touch
        def __init__(self, schedules):
            self.schedules = schedules
            self.remote = None

    def runtime_for_entity(hass, entity_id):
        return hass.data.get("test_runtimes", {}).get(entity_id)

    _module(f"{PKG}.entity", run_command=run_command)
    _module(f"{PKG}.raw_commands")
    _module(
        f"{PKG}.runtime",
        VacuumRuntime=VacuumRuntime,
        all_runtimes=lambda hass: list(hass.data.get("test_runtimes", {}).values()),
        runtime_for_entity=runtime_for_entity,
    )


_install_stubs()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom integrations in every test."""
