"""Contract tests for the local-libvirt example host-install compatibility caller."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "install-host.sh"
BASH = shutil.which("bash")

pytestmark = requires_bash(4, 3, "the example installer uses Bash")


def test_installer_delegates_to_canonical_recipe(tmp_path: Path) -> None:
    """The example passes arguments through without retaining a host-install implementation."""
    args_file = tmp_path / "args"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_just = bindir / "just"
    fake_just.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args_file}"\n')
    fake_just.chmod(0o755)

    assert BASH is not None
    result = subprocess.run(
        [BASH, str(SCRIPT), "--dry-run"],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert args_file.read_text().splitlines() == ["prepare-local-libvirt-host", "--dry-run"]
