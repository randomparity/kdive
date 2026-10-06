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


def _run_helper(tmp_path, *args, stdin=None):
    """Run kdive-drgn against a fake `drgn` on PATH that records its argv.

    The actual `drgn -k` attach stays the live_vm-gated piece.
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

    # A readable fake BTF file at the path the pre-#3121 helper reacted to, so the test bites on
    # hosts without /sys/kernel/btf/vmlinux (macOS) too.
    btf_file = tmp_path / "vmlinux"
    btf_file.write_text("fake-btf")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "KDIVE_BTF_PATH": str(btf_file),
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


def test_helper_never_passes_symbols_flag(tmp_path) -> None:
    """#3121 / ADR-0723: no released drgn reads kernel BTF, so every mode runs `drgn -k -q` and
    leaves symbol lookup to drgn's default search of the staged /usr/lib/debug vmlinux. The
    environment holds a readable fake BTF file, which the helper must ignore.
    """
    for index, (args, stdin) in enumerate(
        [(("run-script", "7"), b"print(1)\n"), (("sysinfo",), None)]
    ):
        workdir = tmp_path / str(index)
        workdir.mkdir()
        proc, recorded = _run_helper(workdir, *args, stdin=stdin)
        assert proc.returncode == 0, proc.stderr.decode()
        argv_line = recorded.read_text().splitlines()[0]
        assert argv_line.startswith("argv=-k -q "), argv_line
        assert " -s" not in argv_line
