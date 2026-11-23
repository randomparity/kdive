"""Setting declarations must import independently of registry initialization (ADR-0087)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from kdive.config.manifest import SETTING_MODULES


@pytest.mark.parametrize("module_name", SETTING_MODULES)
@pytest.mark.parametrize("config_first", [False, True])
def test_setting_modules_import_in_either_order(module_name: str, config_first: bool) -> None:
    program = """
import importlib
import sys

if sys.argv[2] == "True":
    importlib.import_module("kdive.config")
module = importlib.import_module(sys.argv[1])
config = importlib.import_module("kdive.config")
settings = {setting.name: setting for setting in config.all_settings()}
assert all(settings[setting.name] is setting for setting in module.SETTINGS)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, module_name, str(config_first)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
