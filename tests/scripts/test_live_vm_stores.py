"""Behavioral tests for scripts/live-vm/lib.sh via subprocess-source (ADR-0388)."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "live-vm" / "lib.sh"
BASH = shutil.which("bash") or "bash"


def _run(
    snippet: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source lib.sh, then run ``snippet`` with positional args $1.. — capturing output."""
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


def _produce_stubs(
    bindir: Path, build_id: str = "beef01", build_marker: Path | None = None
) -> None:
    """Stub the host-only tools produce_rootfs_and_kernel drives: build-fs (via python3), the
    libguestfs kernel-extract pair, and eu-readelf."""
    mark = f'echo x >> "{build_marker}"; ' if build_marker else ""
    _stub(
        bindir,
        "python3",
        f'{mark}dest=""; ws=""; want=""; for a in "$@"; do '
        'case "$want" in dest) dest="$a";; ws) ws="$a";; esac; want=""; '
        '[ "$a" = "--dest" ] && want=dest; [ "$a" = "--workspace" ] && want=ws; done; '
        '[ -n "$ws" ] && mkdir -p "$ws"; '
        'echo "$@" > "$(dirname "$dest")/build-fs.argv"; : > "$dest"',
    )
    # /boot has a rescue kernel too; the deterministic selection must skip it (it sorts first).
    _stub(
        bindir,
        "virt-ls",
        'printf "config-6.1\\nvmlinuz-0-rescue-abc\\nvmlinuz-6.1-test\\ninitrd.img\\n"',
    )
    _stub(
        bindir,
        "virt-copy-out",
        'src=""; for a in "$@"; do case "$a" in /boot/*) src="$a";; esac; destdir="$a"; done; '
        # octal \177 == 0x7f: portable across dash and bash (printf \xHH is a bash-only extension).
        'printf "\\177ELF" > "${destdir}/$(basename "$src")"',
    )
    _stub(bindir, "eu-readelf", f'echo "    Build ID: {build_id}"')


def test_produce_rootfs_and_kernel(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dest = tmp_path / "set"
    dest.mkdir()
    _produce_stubs(bindir)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "KDIVE_PYTHON": "python3"}
    r = _run('produce_rootfs_and_kernel "$1" "$2"', str(dest), "rocky10-debug", env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "beef01"
    assert (dest / "rootfs.qcow2").exists()
    assert (dest / "vmlinux").exists()  # the rootfs's own kernel, extracted
    assert (dest / ".kver").read_text() == "6.1-test"  # rescue kernel skipped, real one chosen
    argv = (dest / "build-fs.argv").read_text()
    assert "--workspace" in argv and str(dest) in argv


def test_produce_rootfs_and_kernel_dies_when_build_produces_no_rootfs(tmp_path: Path) -> None:
    # A build that exits 0 without writing the qcow2 must fail HERE with a clear message, not deep
    # at the virt-ls below as the misleading `rootfs.qcow2: No such file` (#1320).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dest = tmp_path / "set"
    dest.mkdir()
    _produce_stubs(bindir)
    _stub(bindir, "python3", "exit 0")  # build-fs "succeeds" but writes no rootfs
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "KDIVE_PYTHON": "python3"}
    r = _run('produce_rootfs_and_kernel "$1" "$2"', str(dest), "rocky10-debug", env=env)
    assert r.returncode != 0
    assert "produced no rootfs" in r.stderr
    assert not (dest / "vmlinux").exists()  # failed before the kernel extract
    assert not (dest / ".build").exists()


WARM = ROOT / "scripts" / "live-vm" / "warm-store.sh"


def _warm_env(bindir: Path, store: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "KDIVE_PYTHON": "python3",
        "KDIVE_WARM_STORE_DIR": str(store),
        "KDIVE_WARM_STORE_TARGET_NVR": "kernel-6.1-test",  # contains the built kver "6.1-test"
        "KDIVE_WARM_STORE_IMAGE": "rocky10-debug",
        "DEBUGINFOD_URLS": "https://debuginfod.example",
    }
    env.update(extra)
    return env


def _debuginfod_ok(bindir: Path) -> None:
    # Cache under a build-id subdir (distinct from the script's vmlinux.debug destination).
    _stub(
        bindir,
        "debuginfod-find",
        'd="$DEBUGINFOD_CACHE_PATH/$2"; mkdir -p "$d"; '
        'printf "\\177ELF" > "$d/debuginfo"; echo "$d/debuginfo"',
    )


def test_warm_store_syntax_valid() -> None:
    assert subprocess.run([BASH, "-n", str(WARM)], check=False).returncode == 0


def test_warm_store_requires_the_pins(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "KDIVE_WARM_STORE_DIR": str(store)}  # no pins
    r = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert r.returncode != 0
    assert "KDIVE_WARM_STORE_TARGET_NVR" in r.stderr


def test_warm_store_requires_debuginfod_urls_before_building(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    marker = tmp_path / "build.calls"
    _produce_stubs(bindir, build_marker=marker)
    _debuginfod_ok(bindir)
    env = _warm_env(bindir, store)
    del env["DEBUGINFOD_URLS"]  # misconfigured: no fetch infra
    r = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert r.returncode != 0 and "not configured" in r.stderr
    assert not marker.exists()  # failed fast — the multi-GB build never ran


def test_warm_store_dies_on_pin_kernel_mismatch(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    _produce_stubs(bindir)  # builds kernel version "6.1-test"
    _debuginfod_ok(bindir)
    r = subprocess.run(
        [BASH, str(WARM)],
        capture_output=True,
        text=True,
        check=False,
        env=_warm_env(bindir, store, KDIVE_WARM_STORE_TARGET_NVR="kernel-9.9-wrong"),
    )
    assert r.returncode != 0 and "does not contain" in r.stderr


def test_warm_store_stdout_is_exactly_three_wiring_lines(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    _produce_stubs(bindir)
    _debuginfod_ok(bindir)
    r = subprocess.run(
        [BASH, str(WARM)], capture_output=True, text=True, check=False, env=_warm_env(bindir, store)
    )
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    keys = sorted(ln.split("=", 1)[0] for ln in lines)
    assert keys == ["KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_VMLINUX"]
    for ln in lines:  # every path resolves through current/, none leaks a build-fs/mktemp path
        assert "/current/" in ln.split("=", 1)[1]
    # The committed set holds the wired artifacts and no debuginfod cache / kver scratch
    # (build-fs.argv is a test-stub artifact the real build-fs never writes).
    names = {p.name for p in (store / "current").iterdir()}
    assert {"MANIFEST", "rootfs.qcow2", "vmlinux", "vmlinux.debug"} <= names
    assert ".dbgcache" not in names and ".kver" not in names


def test_warm_store_dies_on_debuginfo_kernel_mismatch(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    _produce_stubs(bindir)
    _debuginfod_ok(bindir)
    # eu-readelf returns a DIFFERENT id for the fetched debuginfo than for the kernel -> the REAL
    # match reads the debuginfo and dies (proves the assertion is not kernel-vs-itself).
    _stub(
        bindir,
        "eu-readelf",
        'for a; do last="$a"; done; case "$last" in *vmlinux.debug) echo "    Build ID: WRONG";; '
        '*) echo "    Build ID: beef01";; esac',
    )
    r = subprocess.run(
        [BASH, str(WARM)], capture_output=True, text=True, check=False, env=_warm_env(bindir, store)
    )
    assert r.returncode != 0 and "mismatch" in r.stderr


def test_warm_store_second_run_is_warm_and_skips_build(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    marker = tmp_path / "build.calls"
    _produce_stubs(bindir, build_marker=marker)
    _debuginfod_ok(bindir)
    env = _warm_env(bindir, store)
    first = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert second.returncode == 0, second.stderr
    assert marker.read_text().count("x") == 1  # warm: build ran once, not twice


def test_warm_store_force_rebuilds(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    marker = tmp_path / "build.calls"
    _produce_stubs(bindir, build_marker=marker)
    _debuginfod_ok(bindir)
    subprocess.run(
        [BASH, str(WARM)], capture_output=True, text=True, check=False, env=_warm_env(bindir, store)
    )
    subprocess.run(
        [BASH, str(WARM)],
        capture_output=True,
        text=True,
        check=False,
        env=_warm_env(bindir, store, KDIVE_WARM_STORE_FORCE="1"),
    )
    assert marker.read_text().count("x") == 2  # force skips the warm fast-path


STAGE = ROOT / "scripts" / "live-vm" / "stage-tcg-images.sh"


def test_stage_tcg_syntax_valid() -> None:
    assert subprocess.run([BASH, "-n", str(STAGE)], check=False).returncode == 0


def _stage_env(bindir: Path, stage: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "KDIVE_PYTHON": "python3",
        "KDIVE_TCG_STAGE_DIR": str(stage),
        "KDIVE_TCG_IMAGE": "rocky10-ppc64le-debug",
        "KDIVE_TCG_BUDGET_BYTES": "1000000000",  # 1 GB, generous for the stubbed tiny files
    }
    env.update(extra)
    return env


def _stage_stubs(bindir: Path) -> None:
    _produce_stubs(bindir, build_id="cafe02")  # python3/virt-ls/virt-copy-out/eu-readelf
    _debuginfod_ok(bindir)  # present for require_tools; individual tests override as needed
    _stub(bindir, "df", "echo Avail; echo 900000000000")  # plenty free


def test_stage_tcg_happy_path_emits_wiring(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt" / "kdive-tcg"
    stage.parent.mkdir()  # /mnt; the script creates the stage dir itself
    _stage_stubs(bindir)
    _debuginfod_ok(bindir)  # shared with the warm-store tests
    r = subprocess.run(
        [BASH, str(STAGE)],
        capture_output=True,
        text=True,
        check=False,
        env=_stage_env(bindir, stage, DEBUGINFOD_URLS="https://debuginfod.example"),
    )
    assert r.returncode == 0, r.stderr
    keys = sorted(ln.split("=", 1)[0] for ln in r.stdout.splitlines() if ln.strip())
    assert keys == ["KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_VMLINUX"]


def test_stage_tcg_distinguishes_fetch_failure_tiers(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt" / "kdive-tcg"
    stage.parent.mkdir()
    _stage_stubs(bindir)
    _stub(bindir, "debuginfod-find", "exit 1")  # not-found (index lag)
    lag = subprocess.run(
        [BASH, str(STAGE)],
        capture_output=True,
        text=True,
        check=False,
        env=_stage_env(bindir, stage, DEBUGINFOD_URLS="https://debuginfod.example"),
    )
    assert lag.returncode != 0 and "not yet published" in lag.stderr
    infra = subprocess.run(  # DEBUGINFOD_URLS unset -> fails before the build
        [BASH, str(STAGE)],
        capture_output=True,
        text=True,
        check=False,
        env=_stage_env(bindir, stage),
    )
    assert infra.returncode != 0 and "not configured" in infra.stderr


def test_stage_tcg_refuses_top_level_stage_dir(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stage_stubs(bindir)
    # A top-level override (dirname == "/") must be refused BEFORE any rm -rf of the mount root.
    r = subprocess.run(
        [BASH, str(STAGE)],
        capture_output=True,
        text=True,
        check=False,
        env=_stage_env(
            bindir, Path("/kdive-tcg-nonexistent"), DEBUGINFOD_URLS="https://debuginfod.example"
        ),
    )
    assert r.returncode != 0 and "refusing" in r.stderr.lower()


def test_stage_tcg_fails_loud_when_disk_too_full(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt" / "kdive-tcg"
    stage.parent.mkdir()
    _stage_stubs(bindir)
    _stub(bindir, "df", "echo Avail; echo 10")  # only 10 bytes free
    r = subprocess.run(
        [BASH, str(STAGE)],
        capture_output=True,
        text=True,
        check=False,
        env=_stage_env(bindir, stage, DEBUGINFOD_URLS="https://debuginfod.example"),
    )
    assert r.returncode != 0 and "free" in r.stderr


def test_require_tools_passes_and_names_missing(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "present-tool", "exit 0")
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    ok = _run('require_tools "$1"', "present-tool:somepkg", env=env)
    assert ok.returncode == 0, ok.stderr
    miss = _run(
        'require_tools "$1" "$2"', "present-tool:somepkg", "absent-xyz:get-it-here", env=env
    )
    assert miss.returncode != 0
    assert "absent-xyz" in miss.stderr and "get-it-here" in miss.stderr
    assert "present-tool" not in miss.stderr  # only the missing one is named


def test_kernel_build_id_names_extract_vmlinux_when_missing(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A non-ELF (compressed) kernel image, with extract-vmlinux absent from PATH.
    kimg = tmp_path / "bzImage"
    kimg.write_bytes(b"\x1f\x8b\x08" + b"\x00" * 60)  # gzip magic, not ELF
    _stub(bindir, "eu-readelf", 'echo "    Build ID: x"')  # present, but not reached
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}  # no extract-vmlinux
    r = _run('kernel_build_id "$1"', str(kimg), env=env)
    assert r.returncode != 0 and "extract-vmlinux" in r.stderr
