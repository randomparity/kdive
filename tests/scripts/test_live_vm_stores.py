"""Behavioral tests for scripts/live-vm/lib.sh via subprocess-source (ADR-0388)."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "live-vm" / "lib.sh"
BASH = shutil.which("bash")


def _run(
    snippet: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source lib.sh, then run ``snippet`` with positional args $1.. — capturing output."""
    assert BASH is not None, "bash is required"
    return subprocess.run(
        [BASH, "-c", f'source "{LIB}" && {snippet}', "_", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_du_bytes_reports_size(tmp_path: Path) -> None:
    f = tmp_path / "blob"
    f.write_bytes(b"x" * 4096)
    r = _run('du_bytes "$1"', str(f))
    assert r.returncode == 0
    assert int(r.stdout.strip()) >= 4096


def test_enforce_budget_passes_at_and_under_ceiling(tmp_path: Path) -> None:
    d = tmp_path / "set"
    d.mkdir()
    (d / "f").write_bytes(b"y" * 1000)
    size = int(_run('du_bytes "$1"', str(d)).stdout.strip())
    ok = _run('enforce_budget "$1" "$2" "$3"', str(d), str(size), "staged set")
    assert ok.returncode == 0, ok.stderr


def test_enforce_budget_fails_loud_over_ceiling(tmp_path: Path) -> None:
    d = tmp_path / "set"
    d.mkdir()
    (d / "f").write_bytes(b"y" * 4096)
    size = int(_run('du_bytes "$1"', str(d)).stdout.strip())
    over = _run('enforce_budget "$1" "$2" "$3"', str(d), str(size - 1), "staged set")
    assert over.returncode != 0
    assert "staged set" in over.stderr
    assert str(size - 1) in over.stderr


def test_require_free_space_passes_and_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "df", 'echo "Avail"; echo "5000"')  # 5000 bytes available
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    ok = _run('require_free_space "$1" "$2" "$3"', str(tmp_path), "4000", "tcg", env=env)
    assert ok.returncode == 0, ok.stderr
    no = _run('require_free_space "$1" "$2" "$3"', str(tmp_path), "6000", "tcg", env=env)
    assert no.returncode != 0
    assert "tcg" in no.stderr


def test_sha256_ok_roundtrip_and_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "art"
    f.write_bytes(b"payload")
    digest = _run('sha256_of "$1"', str(f)).stdout.strip()
    assert len(digest) == 64
    assert _run('sha256_ok "$1" "$2"', str(f), digest).returncode == 0
    f.write_bytes(b"payload-truncated-changed")  # byte change -> digest differs
    bad = _run('sha256_ok "$1" "$2"', str(f), digest)
    assert bad.returncode != 0  # non-fatal: a mismatch is status 1 (rebuild), not a die


def test_build_ids_match_equal_mismatch_and_empty() -> None:
    assert _run('build_ids_match "$1" "$2"', "abc123", "abc123").returncode == 0
    mism = _run('build_ids_match "$1" "$2"', "abc123", "def456")
    assert mism.returncode != 0 and "mismatch" in mism.stderr
    empty = _run('build_ids_match "$1" "$2"', "", "")  # vacuous-match guard
    assert empty.returncode != 0 and "empty" in empty.stderr


def test_elf_build_id_reads_and_dies_on_empty(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dbg = tmp_path / "vmlinux.debug"
    dbg.write_bytes(b"\x7fELF" + b"\x00" * 60)
    _stub(bindir, "eu-readelf", 'echo "    Build ID: dbg99"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    assert _run('elf_build_id "$1"', str(dbg), env=env).stdout.strip() == "dbg99"
    _stub(bindir, "eu-readelf", "true")  # no id
    none = _run('elf_build_id "$1"', str(dbg), env=env)
    assert none.returncode != 0 and "build-id" in none.stderr


def test_kernel_build_id_reads_bare_elf_and_dies_on_empty(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    elf = tmp_path / "vmlinux"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    _stub(bindir, "eu-readelf", 'echo "    Build ID: deadbeefcafe"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    got = _run('kernel_build_id "$1"', str(elf), env=env)
    assert got.returncode == 0
    assert got.stdout.strip() == "deadbeefcafe"
    _stub(bindir, "eu-readelf", "true")  # prints nothing
    none = _run('kernel_build_id "$1"', str(elf), env=env)
    assert none.returncode != 0 and "build-id" in none.stderr


def test_assert_same_fs_same_and_cross_device(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "stat", "echo 42")  # every path reports device 42
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    same = _run('assert_same_fs "$1" "$2"', "/a", "/b", env=env)
    assert same.returncode == 0, same.stderr
    # `stat -c %d -- <path>` puts the path LAST, not at $3 (which is `--`). Key on the last arg.
    _stub(
        bindir, "stat", 'for a; do last="$a"; done; case "$last" in */a) echo 1;; *) echo 2;; esac'
    )
    env2 = {"PATH": f"{bindir}:/usr/bin:/bin"}
    cross = _run('assert_same_fs "$1" "$2"', "/a", "/b", env=env2)
    assert cross.returncode != 0 and "filesystem" in cross.stderr


def test_manifest_write_read_roundtrip(tmp_path: Path) -> None:
    m = tmp_path / "MANIFEST"
    _run(
        'write_manifest "$1" "$2" "$3" "$4" "$5" "$6"',
        str(m),
        "kernel-6.1-nvr",
        "bid42",
        "rootsha",
        "kernsha",
        "dbgsha",
    )
    assert _run('manifest_field "$1" "$2"', str(m), "kernel_nvr").stdout.strip() == "kernel-6.1-nvr"
    assert _run('manifest_field "$1" "$2"', str(m), "build_id").stdout.strip() == "bid42"
    assert _run('manifest_field "$1" "$2"', str(m), "debuginfo_sha256").stdout.strip() == "dbgsha"


def test_store_manifest_matches_and_absent(tmp_path: Path) -> None:
    m = tmp_path / "MANIFEST"
    _run('write_manifest "$1" "$2" "$3" "$4" "$5" "$6"', str(m), "nvrA", "b", "r", "k", "d")
    assert _run('store_manifest_matches "$1" "$2"', str(m), "nvrA").returncode == 0
    assert _run('store_manifest_matches "$1" "$2"', str(m), "nvrB").returncode != 0
    absent = _run('store_manifest_matches "$1" "$2"', str(tmp_path / "none"), "nvrA")
    assert absent.returncode != 0  # absent manifest is stale, not an error/crash


def test_commit_set_flips_symlink_and_prunes(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "set-aaa").mkdir()
    (store / "set-bbb").mkdir()
    # commit_set requires same-fs; real stat works since both are under tmp_path.
    c1 = _run('commit_set "$1" "$2"', str(store), str(store / "set-aaa"))
    assert c1.returncode == 0, c1.stderr
    assert (store / "current").resolve() == (store / "set-aaa").resolve()
    # A same-NVR rebuild: a NEW populated set replaces the pointed-at one atomically.
    (store / "set-ccc").mkdir()
    c2 = _run('commit_set "$1" "$2"', str(store), str(store / "set-ccc"))
    assert c2.returncode == 0, c2.stderr
    assert (store / "current").resolve() == (store / "set-ccc").resolve()
    _run('prune_other_sets "$1"', str(store))
    remaining = sorted(p.name for p in store.glob("set-*"))
    assert remaining == ["set-ccc"]  # only the pointed-at set survives
