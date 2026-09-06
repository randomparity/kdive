"""Installed local external-boot authority native carrier (#2151)."""

import pytest

from tests.live_vm.installed_local_authority_support import (
    run_installed_local_authority_normal_operations,
)


@pytest.mark.live_vm
def test_installed_local_authority_normal_operations() -> None:
    """Prove installed activate, release, and cleanup through MCP and fixed workers."""
    run_installed_local_authority_normal_operations()
