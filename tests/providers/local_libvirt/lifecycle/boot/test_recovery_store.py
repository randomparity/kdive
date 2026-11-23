"""Local durable stores must not load their lifecycle coordinator."""

from __future__ import annotations

import subprocess
import sys


def test_storage_import_does_not_load_lifecycle_coordinator() -> None:
    program = """
import sys
from kdive.providers.local_libvirt.lifecycle.boot import recovery_store

assert recovery_store.RecoveryMetadataStore.__module__ == recovery_store.__name__
assert recovery_store.TargetProjectionStore.__module__ == recovery_store.__name__
assert "kdive.providers.local_libvirt.lifecycle.boot.external_boot" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
