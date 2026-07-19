# Guest-image + debuginfo provisioning (#1292) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide the tooling that produces and stages the live tiers' guest images + matching `vmlinux` debuginfo as two asymmetric stores (a persistent self-hosted warm store and an ephemeral hosted TCG `/mnt` set under a measured disk budget).

**Architecture:** Three bash scripts under `scripts/live-vm/` (a shared `lib.sh` unit-tested seam, `warm-store.sh`, `stage-tcg-images.sh`), plus a persistent warm-store directory added to sub-issue B's Ansible role and a documented disk budget. Debuginfo is fetched by the kernel's ELF build-id via `debuginfod`, so the match holds by construction and works cross-arch. Scope is tooling only — no `.github/workflows` edits and no provisioned-System stand-up (both sub-issue D).

**Tech Stack:** Bash (guarded by `shfmt -i 2` + `shellcheck` via `just lint-shell`), pytest (subprocess-source behavioral tests, the repo's mutation-proven shell-test pattern), Ansible (`just lint-ansible test-ansible`).

**Spec:** `docs/superpowers/specs/2026-07-19-guest-image-debuginfo-provisioning-1292-design.md`
**ADR:** `docs/adr/0388-guest-image-debuginfo-provisioning.md`

## Global Constraints

- **Bash style:** 2-space indent (`shfmt -i 2`); `shellcheck`-clean. Scripts under `scripts/` are auto-discovered by `just lint-shell` (`shfmt -f scripts | xargs shellcheck`). No inline `shellcheck disable` without a justification comment.
- **`lib.sh` is SOURCED, never executed** — it defines functions only, no side effects at source time. Header `#!/usr/bin/env bash` + a "SOURCED, never executed" comment, matching `scripts/live-stack/lib.sh`.
- **Fail loud, never silent** — every helper that can fail calls `die` with an actionable message (operation, input, ceiling/expected). This mirrors `require_free_http_port` in `scripts/live-stack/lib.sh`.
- **Stdout discipline** — the store scripts emit *only* the eval-safe `KDIVE_LIVE_VM_*` wiring block on stdout; all human/progress text goes to stderr. `build-fs`'s own stdout must be captured, never passed through.
- **Consumer env vars (verbatim, from `src/kdive/config/external_env.py`):** `KDIVE_LIVE_VM_ROOTFS` (bootable rootfs qcow2), `KDIVE_LIVE_VM_BZIMAGE` (kernel image), `KDIVE_LIVE_VM_VMLINUX` (vmlinux debuginfo; also satisfies `KDIVE_LIVE_VM_GDBMI_VMLINUX`).
- **Line length ≤100; absolute imports only in Python; Google-style docstrings on non-trivial Python.**
- **Guardrail suite:** `just lint-shell lint-ansible test-ansible test` (plus the doc guards for Task 7). CI runs each recipe individually.
- **No new dependency.** `du`, `df`, `sha256sum`, `numfmt`, `stat`, `sed`, `awk`, `mktemp`, `flock`, `ln`, `mv` are coreutils/util-linux; `eu-readelf`, `debuginfod-find`, `extract-vmlinux` are host-only tools exercised in the live proof, stubbed in tests.
- **Scope fences:** do **not** edit `.github/workflows/*`; do **not** add live-stack/System bring-up. Those are sub-issue D.

---

### Task 1: `lib.sh` — disk/budget helpers

**Files:**
- Create: `scripts/live-vm/lib.sh`
- Test: `tests/scripts/test_live_vm_stores.py`

**Interfaces:**
- Produces: `die MSG`; `du_bytes PATH` → prints bytes; `report_usage LABEL PATH` (→ stderr); `enforce_budget PATH CEILING_BYTES WHAT` (die if `du_bytes > CEILING`); `require_free_space PATH NEEDED_BYTES WHAT` (die if `df` avail `< NEEDED`).

- [ ] **Step 1: Write the failing tests**

Create `tests/scripts/test_live_vm_stores.py`:

```python
"""Behavioral tests for scripts/live-vm/lib.sh via subprocess-source (ADR-0388)."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "live-vm" / "lib.sh"
BASH = shutil.which("bash")


def _run(snippet: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source lib.sh, then run `snippet` with positional args $1.. — capturing output."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q`
Expected: FAIL (lib.sh does not exist / functions not defined).

- [ ] **Step 3: Write `scripts/live-vm/lib.sh` with the disk helpers**

```bash
#!/usr/bin/env bash
# Shared helpers for the live-vm image stores (warm-store.sh, stage-tcg-images.sh).
# SOURCED, never executed (ADR-0388): defines functions only, no side effects at source time.

# Fail loud with an actionable message and a non-zero exit (the require_* pattern from
# scripts/live-stack/lib.sh).
die() {
  printf 'live-vm store: %s\n' "$*" >&2
  exit 1
}

# Apparent size of PATH in bytes.
du_bytes() {
  du -sb -- "$1" | cut -f1
}

# Human-readable measured-usage line to STDERR (stdout is the eval-safe wiring block only).
report_usage() {
  local label="$1" path="$2" bytes
  bytes="$(du_bytes "$path")"
  printf 'live-vm usage: %s=%s bytes (%s)\n' "$label" "$bytes" "$(numfmt --to=iec "$bytes")" >&2
}

# Post-stage footprint cap: die if PATH exceeds CEILING_BYTES; else report. Boundary: == passes.
enforce_budget() {
  local path="$1" ceiling="$2" what="$3" bytes
  bytes="$(du_bytes "$path")"
  if [ "$bytes" -gt "$ceiling" ]; then
    die "$what exceeds budget: ${bytes} bytes > ceiling ${ceiling} bytes at ${path}"
  fi
  printf 'live-vm usage: %s=%s bytes (ceiling %s)\n' "$what" "$bytes" "$ceiling" >&2
}

# Best-effort pre-check (NOT a reservation): die if the fs holding PATH has < NEEDED_BYTES free.
require_free_space() {
  local path="$1" needed="$2" what="$3" free
  free="$(df -B1 --output=avail -- "$path" | tail -n1 | tr -d ' ')"
  if [ "$free" -lt "$needed" ]; then
    die "$what needs ${needed} bytes free at ${path}, only ${free} available"
  fi
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint the shell**

Run: `just lint-shell`
Expected: clean (no shellcheck/shfmt diff).

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/lib.sh tests/scripts/test_live_vm_stores.py
git commit -m "feat(1292): live-vm store disk/budget helpers"
```

---

### Task 2: `lib.sh` — integrity + build-id match helpers

**Files:**
- Modify: `scripts/live-vm/lib.sh`
- Test: `tests/scripts/test_live_vm_stores.py`

**Interfaces:**
- Produces: `sha256_of FILE` → prints digest; `verify_sha256 FILE EXPECTED` (die on mismatch); `build_ids_match A B` (die if either empty or unequal); `kernel_build_id KERNEL_IMAGE` → prints build-id (die on empty); `assert_same_fs A B` (die if different device).

- [ ] **Step 1: Write the failing tests (append to `tests/scripts/test_live_vm_stores.py`)**

```python
def test_verify_sha256_roundtrip_and_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "art"
    f.write_bytes(b"payload")
    digest = _run('sha256_of "$1"', str(f)).stdout.strip()
    assert len(digest) == 64
    ok = _run('verify_sha256 "$1" "$2"', str(f), digest)
    assert ok.returncode == 0
    f.write_bytes(b"payload-truncated-changed")  # byte change -> digest differs
    bad = _run('verify_sha256 "$1" "$2"', str(f), digest)
    assert bad.returncode != 0
    assert "digest mismatch" in bad.stderr


def test_build_ids_match_equal_mismatch_and_empty() -> None:
    assert _run('build_ids_match "$1" "$2"', "abc123", "abc123").returncode == 0
    mism = _run('build_ids_match "$1" "$2"', "abc123", "def456")
    assert mism.returncode != 0 and "mismatch" in mism.stderr
    empty = _run('build_ids_match "$1" "$2"', "", "")  # vacuous-match guard
    assert empty.returncode != 0 and "empty" in empty.stderr


def test_kernel_build_id_reads_bare_elf_and_dies_on_empty(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Bare-ELF path: a file whose first 4 bytes are the ELF magic; stub eu-readelf to emit an id.
    elf = tmp_path / "vmlinux"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 60)
    _stub(bindir, "eu-readelf", 'echo "    Build ID: deadbeefcafe"')
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    got = _run('kernel_build_id "$1"', str(elf), env=env)
    assert got.returncode == 0
    assert got.stdout.strip() == "deadbeefcafe"
    # Empty extraction -> die (never a vacuous empty id).
    _stub(bindir, "eu-readelf", "true")  # prints nothing
    none = _run('kernel_build_id "$1"', str(elf), env=env)
    assert none.returncode != 0 and "build-id" in none.stderr


def test_assert_same_fs_same_and_cross_device(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "stat", 'echo 42')  # every path reports device 42
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    same = _run('assert_same_fs "$1" "$2"', "/a", "/b", env=env)
    assert same.returncode == 0, same.stderr
    _stub(bindir, "stat", 'case "$3" in */a) echo 1;; *) echo 2;; esac')
    env2 = {"PATH": f"{bindir}:/usr/bin:/bin"}
    cross = _run('assert_same_fs "$1" "$2"', "/a", "/b", env=env2)
    assert cross.returncode != 0 and "filesystem" in cross.stderr
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k "sha256 or build_id or same_fs"`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Append the helpers to `scripts/live-vm/lib.sh`**

```bash
# Content digest of FILE.
sha256_of() {
  sha256sum -- "$1" | cut -d' ' -f1
}

# Re-check FILE against EXPECTED digest (completeness — build-id survives truncation, a digest does not).
verify_sha256() {
  local file="$1" expected="$2" actual
  actual="$(sha256_of "$file")"
  [ "$actual" = "$expected" ] || die "digest mismatch for ${file}: got ${actual}, expected ${expected}"
}

# Post-fetch match assertion. Die if EITHER id is empty (even if both are) — no vacuous match.
build_ids_match() {
  local a="$1" b="$2"
  { [ -n "$a" ] && [ -n "$b" ]; } || die "empty build-id (a='${a}' b='${b}') — refusing vacuous match"
  [ "$a" = "$b" ] || die "build-id mismatch: kernel=${a} debuginfo=${b}"
}

# Read the .note.gnu.build-id from the ACTUAL staged kernel artifact (not repo metadata).
# Bare vmlinux ELF (common for ppc64le pseries) is read directly; a compressed bzImage/vmlinuz is
# first decompressed. Die (never empty) if no id — an empty id must not flow into the match.
kernel_build_id() {
  local image="$1" magic vmlinux id
  magic="$(head -c4 -- "$image" | od -An -tx1 | tr -d ' ')"
  if [ "$magic" = "7f454c46" ]; then
    vmlinux="$image"
  else
    vmlinux="$(mktemp)"
    extract-vmlinux "$image" >"$vmlinux" 2>/dev/null || die "cannot extract vmlinux from ${image}"
  fi
  id="$(eu-readelf -n "$vmlinux" 2>/dev/null | awk '/Build ID:/{print $NF}')"
  [ -n "$id" ] || die "no build-id in kernel image ${image}"
  printf '%s\n' "$id"
}

# rename(2) is atomic only within one filesystem: die unless A and B share a device.
assert_same_fs() {
  local a="$1" b="$2"
  [ "$(stat -c %d -- "$a")" = "$(stat -c %d -- "$b")" ] ||
    die "temp and destination not on one filesystem: ${a} vs ${b}"
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Lint**

Run: `just lint-shell`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/lib.sh tests/scripts/test_live_vm_stores.py
git commit -m "feat(1292): live-vm store integrity + build-id match helpers"
```

---

### Task 3: `lib.sh` — manifest + set-commit helpers

**Files:**
- Modify: `scripts/live-vm/lib.sh`
- Test: `tests/scripts/test_live_vm_stores.py`

**Interfaces:**
- Produces: `write_manifest MANIFEST NVR BUILD_ID ROOTFS_SHA KERNEL_SHA DEBUGINFO_SHA` (atomic); `manifest_field MANIFEST KEY` → prints value (rc 1 if absent); `store_manifest_matches MANIFEST TARGET_NVR` (rc 0 iff recorded NVR equals target); `commit_set STORE NEW_SET_DIR` (atomic `current`-symlink flip); `prune_other_sets STORE` (rm every `set-*` dir not pointed at by `current`; tolerant of missing `current`).

- [ ] **Step 1: Write the failing tests (append)**

```python
def test_manifest_write_read_roundtrip(tmp_path: Path) -> None:
    m = tmp_path / "MANIFEST"
    _run(
        'write_manifest "$1" "$2" "$3" "$4" "$5" "$6"',
        str(m), "kernel-6.1-nvr", "bid42", "rootsha", "kernsha", "dbgsha",
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k "manifest or commit_set"`
Expected: FAIL.

- [ ] **Step 3: Append the helpers**

```bash
# Record the pinned inputs atomically (write-temp-then-rename).
write_manifest() {
  local manifest="$1" nvr="$2" build_id="$3" rootfs_sha="$4" kernel_sha="$5" debuginfo_sha="$6" tmp
  tmp="$(mktemp -- "${manifest}.XXXXXX")"
  {
    printf 'kernel_nvr=%s\n' "$nvr"
    printf 'build_id=%s\n' "$build_id"
    printf 'rootfs_sha256=%s\n' "$rootfs_sha"
    printf 'kernel_sha256=%s\n' "$kernel_sha"
    printf 'debuginfo_sha256=%s\n' "$debuginfo_sha"
  } >"$tmp"
  mv -f -- "$tmp" "$manifest"
}

# Print a recorded field; rc 1 when the manifest is absent (stale, not an error).
manifest_field() {
  local manifest="$1" key="$2"
  [ -f "$manifest" ] || return 1
  sed -n "s/^${key}=//p" "$manifest"
}

# rc 0 iff the recorded NVR label equals TARGET_NVR (freshness trigger; necessary, not sufficient).
store_manifest_matches() {
  local manifest="$1" target_nvr="$2" have
  have="$(manifest_field "$manifest" kernel_nvr)" || return 1
  [ "$have" = "$target_nvr" ]
}

# Atomic commit point: flip the `current` symlink onto NEW_SET_DIR via one rename. A directory
# rename cannot atomically replace a populated destination (the same-NVR rebuild case); a symlink
# swap can, regardless of whether a prior same-NVR set exists.
commit_set() {
  local store="$1" new_set="$2"
  assert_same_fs "$store" "$new_set"
  ln -sfn -- "$(basename -- "$new_set")" "${store}/.current.tmp"
  mv -Tf -- "${store}/.current.tmp" "${store}/current"
}

# Remove every set-* dir not pointed at by `current` (post-commit prune AND entry orphan-sweep).
# Tolerant of a missing `current` (first run): then nothing is kept.
prune_other_sets() {
  local store="$1" keep="" d
  [ -L "${store}/current" ] && keep="$(readlink -- "${store}/current")"
  for d in "${store}"/set-*/; do
    [ -d "$d" ] || continue
    [ "$(basename -- "$d")" = "$keep" ] || rm -rf -- "$d"
  done
}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `just lint-shell`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/lib.sh tests/scripts/test_live_vm_stores.py
git commit -m "feat(1292): live-vm store manifest + atomic set-commit helpers"
```

---

### Task 4: `warm-store.sh` — self-hosted warm store refresh

**Files:**
- Create: `scripts/live-vm/warm-store.sh`
- Test: `tests/scripts/test_live_vm_stores.py`

**Interfaces:**
- Consumes: every `lib.sh` helper from Tasks 1–3.
- Produces: an executable script that emits the three-var wiring block on stdout. Env: `KDIVE_WARM_STORE_DIR` (default `/var/lib/kdive/warm-store`), `KDIVE_WARM_STORE_FORCE` (skip warm fast-path). Reads target NVR + builds via `build-fs`; fetches debuginfo via `debuginfod-find` with `DEBUGINFOD_CACHE_PATH` pinned into the store.

- [ ] **Step 1: Write the failing behavioral tests (append)**

These stub `build-fs`, `debuginfod-find`, `eu-readelf`, `extract-vmlinux`, and the distro-NVR resolver on PATH so the orchestration is exercised without a real host. The script reads the target NVR from `KDIVE_WARM_STORE_TARGET_NVR` when set (the seam the test drives; the real default resolves from the distro base).

```python
def _warm_env(bindir: Path, store: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "KDIVE_WARM_STORE_DIR": str(store),
        "KDIVE_WARM_STORE_TARGET_NVR": "kernel-6.1-test",
    }
    env.update(extra)
    return env


WARM = ROOT / "scripts" / "live-vm" / "warm-store.sh"


def test_warm_store_syntax_valid() -> None:
    assert subprocess.run([BASH, "-n", str(WARM)], check=False).returncode == 0


def test_warm_store_stdout_is_exactly_three_wiring_lines(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    # build-fs writes rootfs + kernel into $1 (the set dir) and prints ITS OWN eval-safe block,
    # which warm-store must capture and NOT leak.
    _stub(
        bindir,
        "build-fs",
        'dst="$1"; : > "$dst/rootfs.qcow2"; printf "\\x7fELF" > "$dst/vmlinux"; '
        'echo "KDIVE_LIVE_VM_ROOTFS=$dst/rootfs.qcow2"',
    )
    _stub(bindir, "eu-readelf", 'echo "    Build ID: beef01"')
    _stub(bindir, "debuginfod-find", 'p="$DEBUGINFOD_CACHE_PATH/vmlinux.debug"; echo dbg > "$p"; echo "$p"')
    r = subprocess.run(
        [BASH, str(WARM)], capture_output=True, text=True, check=False,
        env=_warm_env(bindir, store),
    )
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    keys = sorted(ln.split("=", 1)[0] for ln in lines)
    assert keys == ["KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_VMLINUX"]
    # No build-fs-origin duplicate and no mktemp build-dir path — every path is under current/.
    for ln in lines:
        assert "/current/" in ln.split("=", 1)[1]


def test_warm_store_second_run_is_warm_and_skips_build(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    marker = tmp_path / "build-fs.calls"
    _stub(
        bindir,
        "build-fs",
        f'echo x >> "{marker}"; dst="$1"; : > "$dst/rootfs.qcow2"; printf "\\x7fELF" > "$dst/vmlinux"',
    )
    _stub(bindir, "eu-readelf", 'echo "    Build ID: beef01"')
    _stub(bindir, "debuginfod-find", 'p="$DEBUGINFOD_CACHE_PATH/vmlinux.debug"; echo dbg > "$p"; echo "$p"')
    env = _warm_env(bindir, store)
    first = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    assert second.returncode == 0, second.stderr
    assert marker.read_text().count("x") == 1  # warm: build-fs ran once, not twice


def test_warm_store_force_rebuilds(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    marker = tmp_path / "build-fs.calls"
    _stub(
        bindir,
        "build-fs",
        f'echo x >> "{marker}"; dst="$1"; : > "$dst/rootfs.qcow2"; printf "\\x7fELF" > "$dst/vmlinux"',
    )
    _stub(bindir, "eu-readelf", 'echo "    Build ID: beef01"')
    _stub(bindir, "debuginfod-find", 'p="$DEBUGINFOD_CACHE_PATH/vmlinux.debug"; echo dbg > "$p"; echo "$p"')
    env = _warm_env(bindir, store)
    subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env)
    env_force = _warm_env(bindir, store, KDIVE_WARM_STORE_FORCE="1")
    subprocess.run([BASH, str(WARM)], capture_output=True, text=True, check=False, env=env_force)
    assert marker.read_text().count("x") == 2  # force skips the warm fast-path
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k warm`
Expected: FAIL (warm-store.sh missing).

- [ ] **Step 3: Write `scripts/live-vm/warm-store.sh`**

```bash
#!/usr/bin/env bash
# Refresh the self-hosted live-vm warm store: a bootable rootfs + kernel + matching vmlinux
# debuginfo, kept warm (NVR-pinned) between nightly runs (ADR-0388). Emits the eval-safe
# KDIVE_LIVE_VM_* wiring block on stdout; human progress goes to stderr.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

STORE="${KDIVE_WARM_STORE_DIR:-/var/lib/kdive/warm-store}"
mkdir -p -- "$STORE"

# Serialize refreshes; the consuming boot (sub-issue D) takes a shared lock on the same file.
exec 9>"${STORE}/.lock"
flock 9

manifest() { printf '%s/current/MANIFEST' "$STORE"; }

emit_wiring() {
  printf 'KDIVE_LIVE_VM_ROOTFS=%s/current/rootfs.qcow2\n' "$STORE"
  printf 'KDIVE_LIVE_VM_BZIMAGE=%s/current/vmlinux\n' "$STORE"
  printf 'KDIVE_LIVE_VM_VMLINUX=%s/current/vmlinux.debug\n' "$STORE"
}

resolve_target_nvr() {
  # Test seam / operator override; the real default resolves the kernel NVR from the distro base.
  if [ -n "${KDIVE_WARM_STORE_TARGET_NVR:-}" ]; then
    printf '%s\n' "$KDIVE_WARM_STORE_TARGET_NVR"
  else
    die "distro-base NVR resolution unimplemented in CI; set KDIVE_WARM_STORE_TARGET_NVR (host-only)"
  fi
}

is_warm() {
  local target="$1" cur="${STORE}/current"
  [ "${KDIVE_WARM_STORE_FORCE:-0}" = "1" ] && return 1
  [ -e "${cur}/MANIFEST" ] || return 1
  store_manifest_matches "$(manifest)" "$target" || return 1
  verify_sha256 "${cur}/rootfs.qcow2" "$(manifest_field "$(manifest)" rootfs_sha256)" &&
    verify_sha256 "${cur}/vmlinux" "$(manifest_field "$(manifest)" kernel_sha256)" &&
    verify_sha256 "${cur}/vmlinux.debug" "$(manifest_field "$(manifest)" debuginfo_sha256)"
}

main() {
  local target new dbg build_id
  target="$(resolve_target_nvr)"
  prune_other_sets "$STORE" # entry sweep: reclaim any crashed refresh's orphan set dirs.

  if is_warm "$target"; then
    report_usage "warm-store" "$STORE"
    emit_wiring
    return 0
  fi

  new="$(mktemp -d -- "${STORE}/set-XXXXXX")"
  # shellcheck disable=SC2064  # expand $new now so the trap cleans this exact dir on any pre-commit exit.
  trap "rm -rf -- '$new'" EXIT

  # Capture build-fs's OWN eval-safe stdout so it never leaks into our wiring block.
  build-fs "$new" >/dev/null
  build_id="$(kernel_build_id "${new}/vmlinux")"

  export DEBUGINFOD_CACHE_PATH="${new}" # pin the ~1.2 GB download onto the store's filesystem.
  dbg="$(debuginfod-find debuginfo "$build_id")" ||
    die "debuginfod-find failed for build-id ${build_id} (DEBUGINFOD_URLS set? kernel published?)"
  cp -- "$dbg" "${new}/vmlinux.debug"
  build_ids_match "$build_id" "$(kernel_build_id "${new}/vmlinux")"

  write_manifest "${new}/MANIFEST" "$target" "$build_id" \
    "$(sha256_of "${new}/rootfs.qcow2")" "$(sha256_of "${new}/vmlinux")" \
    "$(sha256_of "${new}/vmlinux.debug")"

  commit_set "$STORE" "$new"
  trap - EXIT # committed: the set is now live, do not remove it.
  prune_other_sets "$STORE"
  report_usage "warm-store" "$STORE"
  emit_wiring
}

main "$@"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k warm`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `just lint-shell`
Expected: clean (the one `SC2064` disable carries a justification comment).

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/warm-store.sh tests/scripts/test_live_vm_stores.py
git commit -m "feat(1292): warm-store.sh self-hosted warm store refresh"
```

---

### Task 5: `stage-tcg-images.sh` — hosted TCG `/mnt` image set

**Files:**
- Create: `scripts/live-vm/stage-tcg-images.sh`
- Test: `tests/scripts/test_live_vm_stores.py`

**Interfaces:**
- Consumes: every `lib.sh` helper.
- Produces: an executable script emitting the three-var wiring block. Env: `KDIVE_TCG_STAGE_DIR` (default `/mnt/kdive-tcg`), `KDIVE_TCG_BUDGET_BYTES` (default = documented ceiling). Pins `DEBUGINFOD_CACHE_PATH` under the stage dir. Three distinct fail-loud fetch outcomes.

- [ ] **Step 1: Write the failing behavioral tests (append)**

```python
STAGE = ROOT / "scripts" / "live-vm" / "stage-tcg-images.sh"


def test_stage_tcg_syntax_valid() -> None:
    assert subprocess.run([BASH, "-n", str(STAGE)], check=False).returncode == 0


def _stage_env(bindir: Path, stage: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "KDIVE_TCG_STAGE_DIR": str(stage),
        "KDIVE_TCG_BUDGET_BYTES": "1000000000",  # 1 GB, generous for the stubbed tiny files
    }
    env.update(extra)
    return env


def _stage_stubs(bindir: Path) -> None:
    # stage-ppc64le-set writes rootfs + a bare-ELF kernel into $1.
    _stub(
        bindir,
        "stage-ppc64le-set",
        'dst="$1"; : > "$dst/rootfs.qcow2"; printf "\\x7fELF" > "$dst/vmlinux"',
    )
    _stub(bindir, "eu-readelf", 'echo "    Build ID: cafe02"')
    _stub(bindir, "df", 'echo Avail; echo 900000000000')  # plenty free


def test_stage_tcg_happy_path_emits_wiring(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt"
    stage.mkdir()
    _stage_stubs(bindir)
    _stub(bindir, "debuginfod-find", 'p="$DEBUGINFOD_CACHE_PATH/vmlinux.debug"; echo dbg > "$p"; echo "$p"')
    r = subprocess.run(
        [BASH, str(STAGE)], capture_output=True, text=True, check=False,
        env=_stage_env(bindir, stage),
    )
    assert r.returncode == 0, r.stderr
    keys = sorted(ln.split("=", 1)[0] for ln in r.stdout.splitlines() if ln.strip())
    assert keys == ["KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_VMLINUX"]


def test_stage_tcg_distinguishes_fetch_failure_tiers(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt"
    stage.mkdir()
    _stage_stubs(bindir)
    # Not-found (index lag): debuginfod-find exits 1 with an empty result.
    _stub(bindir, "debuginfod-find", 'exit 1')
    lag = subprocess.run(
        [BASH, str(STAGE)], capture_output=True, text=True, check=False,
        env=_stage_env(bindir, stage, DEBUGINFOD_URLS="https://debuginfod.example"),
    )
    assert lag.returncode != 0 and "not yet published" in lag.stderr
    # Infra not configured: DEBUGINFOD_URLS unset.
    (stage / "kdive-tcg").exists() and shutil.rmtree(stage / "kdive-tcg", ignore_errors=True)
    infra = subprocess.run(
        [BASH, str(STAGE)], capture_output=True, text=True, check=False,
        env=_stage_env(bindir, stage),  # no DEBUGINFOD_URLS
    )
    assert infra.returncode != 0 and "not configured" in infra.stderr


def test_stage_tcg_fails_loud_when_disk_too_full(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stage = tmp_path / "mnt"
    stage.mkdir()
    _stage_stubs(bindir)
    _stub(bindir, "df", 'echo Avail; echo 10')  # only 10 bytes free
    r = subprocess.run(
        [BASH, str(STAGE)], capture_output=True, text=True, check=False,
        env=_stage_env(bindir, stage, DEBUGINFOD_URLS="https://debuginfod.example"),
    )
    assert r.returncode != 0 and "free" in r.stderr
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k stage_tcg`
Expected: FAIL.

- [ ] **Step 3: Write `scripts/live-vm/stage-tcg-images.sh`**

```bash
#!/usr/bin/env bash
# Stage the hosted-runner TCG image set (ppc64le rootfs + kernel + matching debuginfo) onto the
# runner's /mnt scratch, under a measured, enforced disk budget (ADR-0388). Debuginfo is fetched
# on demand by build-id via debuginfod. Emits the eval-safe KDIVE_LIVE_VM_* wiring block on stdout.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

STAGE="${KDIVE_TCG_STAGE_DIR:-/mnt/kdive-tcg}"
BUDGET="${KDIVE_TCG_BUDGET_BYTES:-7000000000}" # ~7 GB whole-budget ceiling (see spec disk budget).
mnt_root="$(dirname -- "$STAGE")"

trap 'rm -rf -- "$STAGE"' EXIT # a failed run leaves no half-populated /mnt for the next to trust.
rm -rf -- "$STAGE"
mkdir -p -- "$STAGE"
export DEBUGINFOD_CACHE_PATH="$STAGE" # pin the ~1.2 GB download onto /mnt, not the small root fs.

# 1. Pre-stage best-effort free-space check for the WHOLE budget (staged set + cache copy + vmcore).
require_free_space "$mnt_root" "$BUDGET" "hosted TCG image set"

# 2. Stage the ppc64le rootfs + kernel; read the build-id from the actual staged artifact.
stage-ppc64le-set "$STAGE"
build_id="$(kernel_build_id "${STAGE}/vmlinux")"

# 3. Fetch matching debuginfo by build-id, with three DISTINCT fail-loud outcomes.
[ -n "${DEBUGINFOD_URLS:-}" ] ||
  die "debuginfod fetch infra not configured: set DEBUGINFOD_URLS to a server indexing ppc64le kernels"
if dbg="$(debuginfod-find debuginfo "$build_id" 2>/dev/null)"; then
  cp -- "$dbg" "${STAGE}/vmlinux.debug"
else
  rc=$?
  # debuginfod-find: exit 1 == not found (index lag); other non-zero == transient/network.
  if [ "$rc" -eq 1 ]; then
    die "debuginfo not yet published for build-id ${build_id} (distro index lag)"
  fi
  die "transient debuginfod error (rc=${rc}) fetching build-id ${build_id}; retry the run"
fi
build_ids_match "$build_id" "$(kernel_build_id "${STAGE}/vmlinux")"

# 4. Post-stage footprint cap on the staged set only.
enforce_budget "$STAGE" "$BUDGET" "hosted TCG image set"

trap - EXIT # staged successfully; keep the set.
report_usage "tcg-stage" "$STAGE"
printf 'KDIVE_LIVE_VM_ROOTFS=%s/rootfs.qcow2\n' "$STAGE"
printf 'KDIVE_LIVE_VM_BZIMAGE=%s/vmlinux\n' "$STAGE"
printf 'KDIVE_LIVE_VM_VMLINUX=%s/vmlinux.debug\n' "$STAGE"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q -k stage_tcg`
Expected: PASS.

- [ ] **Step 5: Full test file + lint**

Run: `uv run python -m pytest tests/scripts/test_live_vm_stores.py -q && just lint-shell`
Expected: all PASS; lint clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/live-vm/stage-tcg-images.sh tests/scripts/test_live_vm_stores.py
git commit -m "feat(1292): stage-tcg-images.sh hosted /mnt image set under budget"
```

---

### Task 6: Ansible — persistent warm-store directory

**Files:**
- Modify: `deploy/ansible/inventory/group_vars/live_vm_runners.yml`

**Interfaces:**
- Consumes: sub-issue B's `live_vm_host` role, which loops over `live_vm_staging_dirs` to create+own each dir. Adding an entry makes the role create the warm-store dir persistently (runner-owned, world-traversable, AppArmor-dynamic — no static label).

- [ ] **Step 1: Add the `warm_store_dir` var and loop entry**

Edit `deploy/ansible/inventory/group_vars/live_vm_runners.yml` — add `warm_store_dir` and append it to the `live_vm_staging_dirs` list:

```yaml
---
# Throwaway-rootfs overlay area (KDIVE_LIVE_VM_ROOTFS's parent) + the provisioned-System
# install staging check-local-libvirt.sh asserts. Both labeled virt_image_t, both traversable.
live_vm_staging_dir: /var/lib/kdive/live-vm
install_staging_dir: /var/lib/kdive/install
# Persistent warm store for the native-KVM nightly's rootfs + kernel + matching debuginfo (#1292,
# ADR-0388). Kept warm between runs; refreshed idempotently by scripts/live-vm/warm-store.sh.
warm_store_dir: /var/lib/kdive/warm-store
# The staging dirs live_vm_host creates, labels, and asserts — one source of truth for the loops.
live_vm_staging_dirs:
  - "{{ live_vm_staging_dir }}"
  - "{{ install_staging_dir }}"
  - "{{ warm_store_dir }}"
# Persistent repo checkout + venv the worker's guestfs/drgn import uses; D reuses via KDIVE_PYTHON.
live_vm_venv: /opt/kdive
live_vm_repo_url: https://github.com/randomparity/kdive.git
live_vm_repo_version: main
```

- [ ] **Step 2: Lint Ansible**

Run: `just lint-ansible`
Expected: clean.

- [ ] **Step 3: Run the Ansible regression suite**

Run: `just test-ansible`
Expected: PASS (the existing `live_vm_host` verify/idempotence coverage exercises the loop; one additive entry does not change its shape).

- [ ] **Step 4: Commit**

```bash
git add deploy/ansible/inventory/group_vars/live_vm_runners.yml
git commit -m "feat(1292): add persistent warm-store dir to the live_vm runner host"
```

---

### Task 7: Docs — disk-budget section in the runbook

**Files:**
- Modify: `docs/operating/runbooks/self-hosted-kvm-runner.md`

**Interfaces:** none (documentation). Records the acceptance-criterion disk budget and how to produce each store.

- [ ] **Step 1: Add a "Guest-image stores and disk budget" section**

Append to `docs/operating/runbooks/self-hosted-kvm-runner.md` (before the `## Maintenance` section) a section that:
- Names the two stores and their scripts: `scripts/live-vm/warm-store.sh` (self-hosted, persistent, `KDIVE_WARM_STORE_DIR=/var/lib/kdive/warm-store`) and `scripts/live-vm/stage-tcg-images.sh` (hosted `/mnt`, ephemeral).
- Copies the disk-budget derivation table from the spec (rootfs ~2 GB, kernel ~0.1 GB, debuginfo ~1.2 GB → staged ceiling ~3.5 GB; + transient cache ~1.2 GB + vmcore headroom ~2 GB → whole budget ~7 GB), and states the ~2 GB vmcore headroom assumes a ≤~2 GB-RAM guest.
- States the enforced `/mnt` ceiling (`KDIVE_TCG_BUDGET_BYTES`, default ~7 GB) and that the warm store only reports usage.
- Records the operator command to capture the first real measurement:
  `KDIVE_WARM_STORE_DIR=/var/lib/kdive/warm-store scripts/live-vm/warm-store.sh` (stderr prints the measured `live-vm usage:` line), and the equivalent for the TCG stage.
- Notes the prerequisites: `DEBUGINFOD_URLS` set to a distro debuginfod that indexes the kernel debuginfo; the refresh-vs-boot shared lock the consumer (sub-issue D) must take.

Use plain, factual prose (no "robust"/"comprehensive"/etc. — the doc-style guard). Operator commands invoke the scripts directly (not `just`), per the operator-doc convention.

- [ ] **Step 2: Run the doc guards**

Run: `just docs-links docs-paths docs-check`
Expected: all clean.

- [ ] **Step 3: Commit**

```bash
git add docs/operating/runbooks/self-hosted-kvm-runner.md
git commit -m "docs(1292): document the guest-image stores and disk budget"
```

---

## Final verification

- [ ] **Run the full guardrail suite**

Run: `just lint-shell lint-ansible test-ansible test`
Expected: all green. (`just ci` for the complete PR gate before pushing.)

- [ ] **Confirm scope fences held**

Run: `git diff --name-only origin/main...HEAD`
Expected: only `scripts/live-vm/*`, `tests/scripts/test_live_vm_stores.py`, `deploy/ansible/inventory/group_vars/live_vm_runners.yml`, `docs/**` — **no** `.github/workflows/*`, **no** live-stack/System changes.

## Self-review notes (spec coverage)

- Warm store (persistent, NVR-pinned, idempotent, integrity-verified, temp-then-swap) → Tasks 3–4 + 6.
- Hosted TCG `/mnt` set (debuginfod by build-id, three fail-loud tiers, free-space + footprint gates, cache pinned onto `/mnt`) → Tasks 2, 5.
- Build-id match by construction + digests + die-on-empty → Tasks 2–3, exercised in 4–5.
- Three-var wiring contract + stdout purity (capture `build-fs`) → Tasks 4–5.
- Measured disk budget documented → Task 7.
- Refresh-vs-boot shared lock as a D boundary → `warm-store.sh` `flock` + Task 7 note.
- No CI wiring, no System stand-up → enforced by the final scope-fence check.
