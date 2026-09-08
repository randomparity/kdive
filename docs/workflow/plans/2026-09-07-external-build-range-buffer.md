# External-build validation range buffering — implementation plan

**Goal.** Replace parser-sized object-store reads during external-build archive validation with a
bounded seekable read-ahead window while preserving validation and immutable-version identity.

**Architecture.** `_RangedReader` remains the only adapter between `gzip`/`tarfile` and the
version-fenced store. It gains a 4 MiB contiguous buffer and binary-file cursor methods; callers and
the `_ObservedVersionStore` identity fence stay unchanged. Direct reader tests prove byte/cursor and
failure semantics, while the existing end-to-end validator fixture proves request reduction and
version pinning.

**Tech stack.** Python 3.14 managed with `uv`; stdlib `io`, `gzip`, and `tarfile`; pytest.

Spec: `docs/workflow/specs/2026-09-07-external-build-range-buffer-design.md`.

Expected implementation size: 270–310 changed lines (M) — corrected after design review required
two cross-window cursor-atomicity failure tests in the roughly 195-line reader test module,
alongside the roughly 60-line reader change and roughly 30-line validator regression.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` checks the whole source and test tree.
- Python 3.14 and stdlib only; add no dependency.
- Host architecture is x86_64. Declared targets are x86_64 and ppc64le; host inclusion does not
  remove the obligation to keep the architecture-neutral reader valid for both targets.
- Retain `_ObservedVersionStore` as the sole immutable-version fence. Do not weaken archive,
  checksum, canonical-path, member-identity, byte/member-count, or build-identity validation.
- Retained read-ahead is at most `_RANGE_CHUNK_BYTES` (4 MiB). Every request is bounded by recorded
  object size. A caller read larger than 4 MiB may be served directly without retaining more than
  4 MiB.
- `read()` retains the existing short-read behavior; `read()` with its default size requests and
  returns all remaining recorded bytes, even beyond 4 MiB, without retaining more than 4 MiB. EOF
  returns `b""`. Seek supports `SEEK_SET`, `SEEK_CUR`, and `SEEK_END`, rejects unknown modes and
  negative results, and permits beyond-EOF.
- A cross-window read commits its cursor only after the follow-on fetch succeeds and its response
  bound is validated. A propagated store exception or oversized response leaves the cursor at the
  read's call-entry position so retry cannot skip buffered bytes.
- A cross-window empty response returns the staged cached suffix, advances only by that suffix, and
  leaves the boundary for the next read to retry. `read(0)` returns `b""`, leaves `tell()` unchanged,
  and performs no store request.
- Scope is `src/kdive/build_artifacts/validation.py`, matching build-artifact tests, and these
  design artifacts. Durable completion and MCP behavior are excluded.
- Guardrails: focused pytest commands below; `just lint`; `just type`; `just test-changed`; and
  exact pre-ship `just ci > FILE 2>&1 < /dev/null`.
- Before committing Python, run `just format`. For Markdown, stage the intended files, run
  `prek run`, then re-add exactly those paths.
- Base branch is `main`; branch is `feat/external-build-range-buffer-2317`.

## File map

| Path | Disposition | Answerable for |
|---|---|---|
| `src/kdive/build_artifacts/validation.py` | modify | bounded buffered reads, cursor/seek behavior, response validation |
| `tests/build_artifacts/test_validation_reader.py` | create | isolated reader byte, cursor, bound, request-count, and failure contracts |
| `tests/providers/local_libvirt/test_validate_external_artifacts.py` | modify | full validator request reduction and immutable-version proof |

## Task 1 — Specify the reader contract with focused tests

**Where this fits.** Direct tests establish the adapter contract before implementation, including
behavior that `gzip` may not exercise deterministically across Python versions.

### Interfaces

- Existing `validation._RangedReader(store: ValidatorStore, key: str, size: int)` from
  `src/kdive/build_artifacts/validation.py`.
- Existing `ValidatorStore.get_range(key, *, start, length, version_id=None) -> bytes` from the
  same module.
- Task 2 relies on tests naming `read`, `tell`, `seek`, the 4 MiB window, and oversized-response
  failure behavior exactly.

### Verification

- **Contract: sequential reads and in-window seeks reuse a bounded byte window.** Mode:
  `focused-test`. Tests `test_ranged_reader_buffers_sequential_reads` and
  `test_ranged_reader_reuses_buffer_after_in_window_seek` in
  `tests/build_artifacts/test_validation_reader.py`. Expected red: multiple store calls and missing
  `seek`. Green command: `uv run python -m pytest tests/build_artifacts/test_validation_reader.py -q`.
- **Contract: cursor, boundary, EOF, and invalid seek semantics match the spec.** Mode:
  `focused-test`. Tests in the same module cover `tell`, reads spanning the 4 MiB boundary,
  a caller read larger than 4 MiB without retaining more than 4 MiB, default `read()` through EOF,
  `read(0)` without a cursor or request change, supported whence values, beyond EOF, negative
  results, and invalid whence. Expected red: missing cursor methods or retained state exceeding the
  cap. Same green command.
- **Contract: short, empty, and oversized range responses are not hidden.** Mode: `focused-test`.
  A configurable fake store returns each response shape or raises a selected categorized exception.
  One test primes the buffer, proves an in-window hit does not touch the failing store, then moves
  outside the window and proves the exact exception propagates unchanged. Two cross-window tests
  consume a cached suffix before an exception or oversized response and assert that `tell()` stays
  at the call-entry position and retry returns the complete bytes. A third cross-window test makes
  the follow-on response empty, asserts the cached suffix is returned and advances the cursor, then
  proves the next read retries at the boundary. Expected red: the existing reader has no
  buffer-aware boundary behavior; oversized response must retain `BUILD_FAILURE`. Same green
  command.

### Steps

1. Create `tests/build_artifacts/test_validation_reader.py` with a byte-backed fake store that
   records `(start, length, version_id)` and can truncate, empty, enlarge, or fail a selected call.
2. Add the named tests with deterministic byte sequences around `_RANGE_CHUNK_BYTES`. Assert exact
   bytes, cursor positions, request arguments, and exception category/message where applicable.
3. Run `uv run python -m pytest tests/build_artifacts/test_validation_reader.py -q`. Require a red
   failure showing unbuffered request count or absent seek behavior before production edits.

### Acceptance criteria

- Each material reader contract has a test that fails on the pre-change implementation.
- Test memory stays bounded: use deterministic repeated byte patterns, not a multi-gigabyte blob.

## Task 2 — Implement bounded read-ahead and seek

**Where this fits.** This is the complete production change at the version-fenced archive-reader
boundary.

### Interfaces

- Preserve `_RangedReader.__init__(store: ValidatorStore, key: str, size: int) -> None` and
  `read(size: int = -1) -> bytes`.
- Add `tell() -> int`, `seekable() -> bool`, and `seek(offset: int, whence: int = io.SEEK_SET) -> int`.
- Task 3 consumes the unchanged validator entry point
  `validate_external_artifacts(store, *, manifest, keys, declared_build_id, arch="x86_64")`.

### Verification

- **Contract: bounded read-ahead reduces store requests without changing returned bytes.** Mode:
  `focused-test`. Task 1's complete test module is the observable contract. Expected red was
  captured before editing; green command:
  `uv run python -m pytest tests/build_artifacts/test_validation_reader.py -q`.

### Steps

1. Add buffer start/data fields to `_RangedReader`, with retained length capped at
   `_RANGE_CHUNK_BYTES`.
2. Implement `read` so a cache hit is staged locally; a miss fetches at least the requested length,
   reads at most recorded EOF, and validates the response bound before committing the staged cursor
   and bytes. A fetch exception or oversized response leaves the entry cursor unchanged. Retain no
   more than 4 MiB.
3. Implement `tell`, `seekable`, and `seek`; preserve the buffer for positions it contains and
   invalidate it for positions outside it.
4. Run the Task 1 focused command and require all tests to pass.
5. Run `just format`, `just lint`, and `just type`; require exit 0.

### Acceptance criteria

- Retained bytes never exceed 4 MiB and no fetch crosses recorded EOF.
- No caller or public contract changes; the existing version fence remains in the call chain.

## Task 3 — Prove the full validation path

**Where this fits.** The direct reader contract is necessary but not sufficient: this task proves
the real parser path materially reduces requests while all validation still succeeds.

### Interfaces

- Existing `_combined_kernel_tar` and `_FakeStore` fixtures in
  `tests/providers/local_libvirt/test_validate_external_artifacts.py`.
- Existing `_ObservedVersionStore` ensures each underlying fake-store call carries
  `test-version` after the first `HEAD`.

### Verification

- **Contract: real external-build validation materially reduces parser-driven requests and keeps
  version identity.** Mode: `focused-test`. Add
  `test_external_boot_archive_validation_buffers_range_reads`. Expected red: the pre-change reader
  makes 117 kernel range calls for the 1,049,287-byte fixture and exceeds the fixed ceiling of
  eight. The count includes content checking, both independent archive readers, and checksumming.
  Green command: `uv run python -m pytest
  tests/providers/local_libvirt/test_validate_external_artifacts.py::test_external_boot_archive_validation_buffers_range_reads -q`.
- **Contract: existing archive validation and failure behavior remain unchanged.** Mode:
  `focused-test`. Green command: `uv run python -m pytest
  tests/providers/local_libvirt/test_validate_external_artifacts.py -q`.

### Steps

1. Add deterministic incompressible module payload to the existing combined-archive fixture and
   assert its compressed size is from 1 MiB through 2 MiB, so it stays within one 4 MiB buffer while
   parser reads cross many smaller boundaries.
2. Add the named regression. Assert validation succeeds, every request carries `test-version`, and
   total kernel range-call count is at most eight across content checking, both independent archive
   passes, and the checksum pass.
3. Run both focused commands and require pass.
4. Run `just test-changed`; require pass.
5. Run pre-ship `just ci > FILE 2>&1 < /dev/null`; require exit 0 and report its test totals.

### Acceptance criteria

- The regression would fail on the unbuffered implementation due to request count.
- Every x86_64 focused and repository guardrail arm passes; ppc64le remains architecture-neutral
  and no target-specific path changed.
- No live tier is required because this change has no live VM or transport dependency; #2318 owns
  representative live MCP-path timing.
