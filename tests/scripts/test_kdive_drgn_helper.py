"""Contract checks for the in-guest kdive-drgn helper (ADR-0085/0240).

The real drgn run is `live_vm`; these assert the helper's shape so the local + remote
introspect seams can rely on the `run-script` stdin mode and the fixed-helper set staying intact.
"""

from __future__ import annotations

from pathlib import Path

HELPER = Path("deploy/remote-libvirt-guest-helpers/kdive-drgn")


def test_helper_keeps_the_fixed_helpers() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "tasks | modules | sysinfo" in text


def test_helper_has_run_script_stdin_mode() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "run-script)" in text
    # Script comes from stdin into a temp file, never from argv; bounded by the caller timeout.
    assert "mktemp" in text
    assert "timeout" in text
    assert "drgn -k -q" in text
