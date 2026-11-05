"""Native remote authority uploaded-vmlinux staging, drgn and restore proof (#3512)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.live_stack.authority_vmlinux import run_remote_authority_vmlinux

pytestmark = pytest.mark.live_stack


def test_remote_authority_stages_the_uploaded_vmlinux(tmp_path: Path) -> None:
    run_remote_authority_vmlinux(tmp_path)
