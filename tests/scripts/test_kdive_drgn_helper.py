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
    assert "drgn_args" in text


def test_helper_loads_btf_explicitly_when_present() -> None:
    """BBR F1 / #1090: bare `drgn -k` silently resolves nothing on guests whose drgn does not
    auto-load BTF. The helper must pass -s explicitly instead of relying on drgn's auto-load.
    """
    text = HELPER.read_text(encoding="utf-8")
    assert '-s "$btf_path"' in text
    assert "/sys/kernel/btf/vmlinux" in text


def _run_helper(tmp_path, *args, btf_present, stdin=None):
    """Run kdive-drgn against a fake `drgn` on PATH that records its argv.

    KDIVE_BTF_PATH stands in for /sys/kernel/btf/vmlinux (which requires root to create), so the
    BTF-present and BTF-absent branches are both exercisable without a live kernel — the actual
    `drgn -k`/`-s` attach stays the live_vm-gated piece.
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

    if btf_present:
        btf_path = tmp_path / "vmlinux"
        btf_path.write_text("fake-btf")
    else:
        btf_path = tmp_path / "no-such-vmlinux"

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "KDIVE_BTF_PATH": str(btf_path),
    }

    proc = subprocess.run(
        ["bash", str(HELPER), *args],
        input=stdin,
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return proc, recorded


def test_run_script_passes_btf_flag_when_btf_present(tmp_path) -> None:
    proc, recorded = _run_helper(
        tmp_path,
        "run-script",
        "7",
        btf_present=True,
        stdin=b"print(prog['init_uts_ns'])\n",
    )

    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip() == "drgn-ran-ok"
    recorded_text = recorded.read_text()
    assert "-k -s" in recorded_text  # live-kernel mode, explicit BTF, quiet, staged script path
    assert "-q" in recorded_text
    assert "script=print(prog['init_uts_ns'])" in recorded_text  # stdin landed in the temp file


def test_run_script_omits_btf_flag_when_btf_absent(tmp_path) -> None:
    proc, recorded = _run_helper(
        tmp_path,
        "run-script",
        "7",
        btf_present=False,
        stdin=b"print(prog['init_uts_ns'])\n",
    )

    assert proc.returncode == 0, proc.stderr.decode()
    recorded_text = recorded.read_text()
    assert "-s" not in recorded_text  # falls back to drgn's own default debug-info search
    assert "argv=-k -q" in recorded_text


def test_fixed_helper_passes_btf_flag_when_btf_present(tmp_path) -> None:
    proc, recorded = _run_helper(tmp_path, "sysinfo", btf_present=True)

    assert proc.returncode == 0, proc.stderr.decode()
    recorded_text = recorded.read_text()
    assert "-k -s" in recorded_text
