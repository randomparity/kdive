"""Structural no-bypass guard: the whole ``kdive.cli.*`` package avoids services + creds.

ADR-0089 decision 5: the operator host holds only the bearer token and the server URL.
This guard walks every module under ``kdive.cli`` and asserts none of them pulls in a
``kdive.services`` object or any database/object-store credential setting, so the boundary
cannot erode through the transport, dispatch, or a future verb.
"""

from __future__ import annotations

import importlib
import pkgutil

import kdive.cli

_FORBIDDEN_SETTINGS = {
    "KDIVE_DATABASE_URL",
    "KDIVE_S3_ENDPOINT_URL",
    "KDIVE_S3_BUCKET",
    "KDIVE_SECRETS_ROOT",
}


def _walk_cli_modules() -> list[str]:
    names = [kdive.cli.__name__]
    for info in pkgutil.walk_packages(kdive.cli.__path__, kdive.cli.__name__ + "."):
        names.append(info.name)
    return names


def test_cli_imports_no_services_module() -> None:
    for name in _walk_cli_modules():
        module = importlib.import_module(name)
        for attr in dir(module):
            obj = getattr(module, attr)
            origin = getattr(obj, "__module__", "")
            assert not origin.startswith("kdive.services"), (
                f"{name} pulls in kdive.services via {attr}"
            )


def test_cli_reads_no_db_or_objectstore_credentials() -> None:
    from kdive.config.registry import Setting

    for name in _walk_cli_modules():
        module = importlib.import_module(name)
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, Setting):
                assert obj.name not in _FORBIDDEN_SETTINGS, (
                    f"{name} references credential setting {obj.name} via {attr}"
                )
