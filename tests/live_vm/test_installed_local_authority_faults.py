"""Installed authority restart-recovery fault carrier (#2151, ADR-0622)."""

import pytest

from tests.live_vm.installed_local_authority_support import (
    run_installed_local_authority_restart_recovery,
)


@pytest.mark.live_vm
def test_installed_local_authority_restart_after_provider_effect() -> None:
    """Require restart recovery after the host effect pauses before provider-returned journaling."""
    run_installed_local_authority_restart_recovery()
