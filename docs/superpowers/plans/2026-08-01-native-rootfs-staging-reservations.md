# Native Rootfs Staging Reservations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or
> executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** Reserve uploaded-rootfs staging blocks atomically so two different-base stagers cannot
jointly overcommit the staging filesystem.

**Architecture:** Bind Linux `fallocate(2)` with an explicit 64-bit ctypes ABI, reserve the base on
the existing flock guard descriptor, and make both stagers write through duplicates of that
descriptor without `O_TRUNC`. Unsupported filesystems retain ADR-0450's advisory behavior; gzip
shrinks its upper-bound reservation to verified output length before publish.

**Tech Stack:** Python 3.14, ctypes/libc, POSIX file descriptors, pytest, Ruff, ty.

## Global Constraints

- Work only on `feat/rootfs-staging-reservation-1546`, based on `main`, in the external worktree.
- Use assigned ADR 0530. Migration 0096 is reserved but unnecessary; create no migration.
- Preserve different-base parallelism; add no global lock, sibling-partial accounting, setting,
  dependency, schema, or MCP contract.
- Never call `os.posix_fallocate`; unsupported native allocation must degrade with a warning.
- Keep every failure path under the existing partial-discard `finally`.
- Run focused tests after each red/green step and `just ci` before completion.

---

### Task 1: Bind native fallocate with a 64-bit ABI

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`
- Test: `tests/providers/local_libvirt/test_rootfs_upload_fetch.py`

**Interfaces:**
- Produces: `_native_fallocate(fd: int, length: int) -> None`, raising `OSError` with captured
  native errno on failure.
- Produces: module-private `_fallocate(fd, mode, offset, length) -> int` call seam with ctypes
  `restype=c_int` and `argtypes=(c_int, c_int, c_long, c_long)`.
- Consumes: supported-host invariant `ctypes.sizeof(ctypes.c_long) == 8`.

- [ ] **Step 1: Write failing ABI and errno tests**

Add imports for `ctypes` and import `_native_fallocate`. Add a fake callable that records its four
integer arguments and returns a configured result. Monkeypatch the module `_fallocate` seam and
assert a `3 * 1024**3` length arrives unchanged:

```python
def test_native_fallocate_preserves_lengths_above_two_gib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, int]] = []

    def _call(fd: int, mode: int, offset: int, length: int) -> int:
        calls.append((fd, mode, offset, length))
        return 0

    monkeypatch.setattr(rootfs_upload_fetch, "_fallocate", _call)
    _native_fallocate(17, 3 * 1024**3)
    assert calls == [(17, 0, 0, 3 * 1024**3)]
```

Add a second test whose fake sets `ctypes.set_errno(errno.ENOSPC)` and returns `-1`; assert the
helper raises `OSError` with `errno == ENOSPC`. Assert `_fallocate.argtypes` and `.restype` on the
real bound function before monkeypatching so a missing prototype cannot pass through the fake.

Add `test_native_fallocate_allocates_a_real_temporary_file`. Open a one-MiB temporary file with
`os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)`, call the unpatched `_native_fallocate`, and
assert `os.fstat(fd).st_size == requested` and `st_blocks * 512 >= requested`. Skip only when the
real call raises `ENOSYS` or `EOPNOTSUPP`, including that errno in the skip reason; every other
error fails. Close and unlink the file in `finally`. This smoke proves the configured ctypes symbol
and native mode-zero allocation work on the host, while the fake-backed test remains the
deterministic proof for capacity contention.

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_preserves_lengths_above_two_gib \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_captures_errno \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_allocates_a_real_temporary_file -q
```

Expected: collection fails because `_native_fallocate` does not exist.

- [ ] **Step 3: Implement the native binding**

In `rootfs_upload_fetch.py`, import `ctypes` and `Callable`, then bind libc at module initialization:

```python
if ctypes.sizeof(ctypes.c_long) != 8:
    raise RuntimeError("local-libvirt rootfs staging requires a 64-bit native off_t")

_libc = ctypes.CDLL(None, use_errno=True)
_fallocate: Callable[[int, int, int, int], int] = _libc.fallocate
_fallocate.restype = ctypes.c_int
_fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]


def _native_fallocate(fd: int, length: int) -> None:
    ctypes.set_errno(0)
    if _fallocate(fd, 0, 0, length) == 0:
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number))
```

If `ty` rejects assigning the dynamic ctypes symbol to `Callable`, isolate the cast at that one
assignment; do not add a project-wide ignore or a wrapper abstraction.

- [ ] **Step 4: Run focused tests, lint, and type checks**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_preserves_lengths_above_two_gib \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_captures_errno \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_fallocate_allocates_a_real_temporary_file -q
just lint
just type
```

Expected: all three tests pass; lint and type checks report no warnings.

The real-filesystem smoke must pass on the local implementation host. A skip is acceptable only on
an explicitly unsupported filesystem and must be reported; the PR's Linux CI run supplies the
second production-seam arm.

- [ ] **Step 5: Mutation-check the ABI test**

Temporarily narrow `_fallocate`'s length argument to `ctypes.c_int`, run
`test_native_fallocate_preserves_lengths_above_two_gib`, and confirm it fails because the observed
length is truncated. Restore `c_long` and rerun the test green.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py
git commit -m "feat(rootfs): bind native staging allocation"
```

### Task 2: Write through the guard descriptor and shrink verified output

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`
- Test: `tests/providers/local_libvirt/test_rootfs_upload_fetch.py`

**Interfaces:**
- Changes: `_stage_identity(..., partial_fd: int, ...) -> int`; returns bytes written.
- Changes: `_stage_gzip(..., partial_fd: int, ...) -> int`; returns verified decompressed bytes.
- Consumes: `_StagingBudget.required` as the identity HEAD length.
- Produces: `_require_identity_length(actual: int, expected: int, ...) -> None`, raising an
  attributable `INFRASTRUCTURE_FAILURE` before format verification.

- [ ] **Step 1: Write failing descriptor, identity-length, and gzip-shrink tests**

Add a direct `_stage_identity` test that opens a partial with `os.open`, preallocates it with
`os.ftruncate(fd, len(payload))`, calls the stager with that descriptor, and asserts the file still
equals the payload rather than being emptied or padded; this fails while the stager accepts only a
path and reopens it with `"wb"`.

Add a store fake whose HEAD reports `len(_QCOW2) + 8`, whose GET returns `_QCOW2`, and whose stored
checksum matches `_QCOW2`. Assert staging raises `INFRASTRUCTURE_FAILURE`, details contain
`expected_bytes` and `actual_bytes`, no base publishes, and no partial remains.

Add a gzip stage test with `uncompressed_size=len(canonical) + 4096`; monkeypatch the qcow2 gate to
record `partial.stat().st_size` and assert it sees exactly `len(canonical)`, proving shrink happens
before the gate.

- [ ] **Step 2: Run the three tests and verify red**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_stage_identity_writes_without_truncating_the_guarded_inode \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_stage_identity_rejects_a_get_shorter_than_head \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_stage_gzip_shrinks_its_reservation_before_the_format_gate -q
```

Expected: identity preallocation is truncated, the short GET can publish a padded file, and gzip
still has the declared-bound length.

- [ ] **Step 3: Refactor both writers onto duplicated guard descriptors**

Pass `guard_fd` into both stagers. Replace pathname writer opens with:

```python
writer_fd = os.dup(partial_fd)
with os.fdopen(writer_fd, "wb", closefd=True) as writer:
    os.lseek(writer.fileno(), 0, os.SEEK_SET)
    # existing bounded stream/hash or gzip decode
```

Count identity bytes as chunks are written and return the count after checksum verification.
Return `result.uncompressed_bytes` from `_stage_gzip` after its existing error annotation and
checksum logging. Do not move or weaken either checksum gate.

- [ ] **Step 4: Enforce exact identity length and gzip shrink before the qcow2 gate**

After identity staging, compare its returned count with `budget.required`. On mismatch raise:

```python
CategorizedError(
    "uploaded rootfs object length changed between HEAD and GET; retry, and if it persists "
    "repair the object-store boundary",
    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
    details={
        "system_id": str(system_id),
        "dest": str(dest),
        "expected_bytes": budget.required,
        "actual_bytes": actual,
    },
)
```

After successful gzip staging, call `os.ftruncate(guard_fd, actual)` before
`_require_still_linked` and `_require_qcow2_magic`. Identity needs no truncate because exact length
is now proven.

- [ ] **Step 5: Run focused and existing writer tests**

Run the three new tests plus existing identity streaming, short-read, gzip streaming, gzip-bomb,
checksum, flock-lifetime, fsync, and marker tests in the same module. Then run `just lint` and
`just type`.

Expected: all selected tests and both static gates pass.

- [ ] **Step 6: Mutation-check both new guards**

Temporarily remove the identity length comparison and confirm its short-GET test fails. Restore it.
Temporarily remove the gzip `ftruncate` and confirm the gate-size test fails. Restore it and rerun
both green.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py
git commit -m "refactor(rootfs): keep staging reservations through writers"
```

### Task 3: Integrate reservation failure, degrade, and concurrency contracts

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`
- Test: `tests/providers/local_libvirt/test_rootfs_upload_fetch.py`
- Modify: `docs/superpowers/specs/2026-08-01-native-rootfs-staging-reservations-design.md`
- Modify: `docs/superpowers/plans/2026-08-01-native-rootfs-staging-reservations.md`

**Interfaces:**
- Produces: `_reserve_staging_space(fd: int, partial: Path, budget: _StagingBudget | None,
  system_id: UUID) -> bool`; `True` means native blocks are reserved, `False` means no knowable
  budget or explicit unsupported-filesystem degrade.
- Consumes: `_native_fallocate`, `_StagingBudget`, `_flocked_partial`, existing warning logger and
  `CategorizedError` taxonomy.
- Produces: reservation failure details `system_id`, `dest`, `requested_bytes`, `budget_source`,
  and `errno`.

- [ ] **Step 1: Write the deterministic two-stager race test**

Use two threads, different destination tokens under one `tmp_path`, and separate `_FakeStore`
instances. Monkeypatch `_native_fallocate` with an allocator that waits at a `threading.Barrier(2)`,
then debits a shared capacity under `threading.Lock`; capacity holds either complete base but not
both and the loser raises `OSError(ENOSPC, ...)`. Pin `disk_usage` so both advisory checks pass.

Assert both callers reached the barrier, exactly one destination published, exactly one store
opened its stream, exactly one caller raised `CategorizedError(INFRASTRUCTURE_FAILURE)`, and that
error's `system_id`, `dest`, `requested_bytes`, `budget_source`, and `errno` identify the loser.
After both threads join, assert the shared staging directory contains no `*.partial`, proving the
new pre-download capacity failure still unwinds through the existing discard `finally`.

- [ ] **Step 2: Write the unsupported-native-allocation degrade test**

Monkeypatch `_native_fallocate` to raise `OSError(EOPNOTSUPP, ...)` and monkeypatch
`os.posix_fallocate` to raise `AssertionError` if called. Stage a valid identity base and assert it
publishes, the stream is consumed, no partial remains, and the warning says native reservation is
unsupported and advisory protection remains.

Add one non-capacity/non-support test (`EIO`) asserting it maps to the existing staging fault and
cleans the partial rather than degrading.

- [ ] **Step 3: Run the new tests and verify red**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_concurrent_native_reservations_admit_exactly_one_stager \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_unsupported_native_reservation_degrades_without_posix_fallocate \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py::test_native_reservation_io_failure_does_not_degrade -q
```

Expected: reservation is not called, both racers publish, and no degrade warning exists.

- [ ] **Step 4: Implement reservation integration**

Call `_reserve_staging_space` immediately after `_flocked_partial` yields and before codec dispatch.
For `ENOSYS`/`EOPNOTSUPP`, warn and return `False`. For `ENOSPC`/`EDQUOT`, raise a reservation-specific
`CategorizedError` with the scalar details above and a free-capacity/re-issue remedy. Re-raise every
other `OSError` so the outer `_staging_fault` handles it. Skip when `budget is None`, preserving the
unsupported-codec and missing-gzip-bound error precedence.

Update the module's Capacity and Concurrency documentation to cite ADR-0530, state the native
guarantee and explicit degrade, and remove the obsolete claim that the precheck is always only
advisory. Keep ADR-0450's floor limitation explicit.

- [ ] **Step 5: Run focused module tests and static gates**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_rootfs_upload_fetch.py -q
just lint
just type
```

Expected: the complete module passes with no warnings.

- [ ] **Step 6: Mutation-check the concurrency proof**

Temporarily replace `_reserve_staging_space`'s native call with unconditional success. Run the
two-stager race test and confirm it fails because both streams are consumed and both destinations
publish. Restore the call and rerun green.

- [ ] **Step 7: Run the full repository gate**

Run `just ci` bare. Expected: all component gates and the non-live suite pass; report every skip.

- [ ] **Step 8: Mark the plan complete and commit**

Check every completed plan box, update the spec's verification paragraph with the focused and full
gate outcomes, then commit only the explicit implementation, test, spec, and plan paths:

```bash
git add src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py \
  tests/providers/local_libvirt/test_rootfs_upload_fetch.py \
  docs/superpowers/specs/2026-08-01-native-rootfs-staging-reservations-design.md \
  docs/superpowers/plans/2026-08-01-native-rootfs-staging-reservations.md
git commit -m "feat(rootfs): reserve staging capacity atomically"
```
