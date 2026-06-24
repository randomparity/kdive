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


def test_run_script_pipes_stdin_to_drgn_k_argv(tmp_path) -> None:
    """Functionally prove the run-script branch: stdin -> temp file -> `timeout drgn -k -q <file>`.

    A fake `drgn` on PATH records its argv and the staged script's contents, so this exercises the
    real bash plumbing (mktemp, stdin read, timeout wrapper, exec argv) without needing root or a
    live kernel — the actual `drgn -k` attach stays the live_vm-gated piece.
    """
    import os
    import subprocess

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    recorded = tmp_path / "recorded.txt"
    fake_drgn = fake_bin / "drgn"
    fake_drgn.write_text(
        "#!/bin/bash\n"
        f'echo "argv=$*" > "{recorded}"\n'
        # last arg is the staged script path; echo its contents to prove stdin landed there
        'for a in "$@"; do last="$a"; done\n'
        f'echo "script=$(cat "$last")" >> "{recorded}"\n'
        'echo "drgn-ran-ok"\n'
    )
    fake_drgn.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    proc = subprocess.run(
        ["bash", str(HELPER), "run-script", "7"],
        input=b"print(prog['init_uts_ns'])\n",
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "drgn-ran-ok"
    recorded_text = recorded.read_text()
    assert "-k -q" in recorded_text  # live-kernel mode, quiet, with the staged script path
    assert "script=print(prog['init_uts_ns'])" in recorded_text  # stdin landed in the temp file
