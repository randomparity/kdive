# External-build finalization measurement — implementation plan (#2318)

**Goal.** Make `runs.complete_build`'s three finalization phases separately observable, measure
them over the real MCP path for a 103-MB-class and a 2-GB-class x86_64 kernel bundle, and record
the completion-contract decision those measurements select.

**Architecture.** `CompleteBuildFinalizer.complete` already runs prepare → validate → publish in
sequence (`src/kdive/services/runs/complete_build.py:158-169`). Task 1 wraps each phase with a
monotonic clock and composes a counting decorator over the `ValidatorStore` protocol at the point
where `_VersionPinnedStore` already wraps it, then emits one structured log record. Task 2 adds a
`live_stack` driver that exercises that path over MCP HTTP with an **unbound** Run — no
allocation, no System, no VM — and correlates the server's record by `run_id`. Task 3 runs it and
writes the proof record. Task 4 writes ADR 0655 from the recorded rows.

**Tech stack.** Python 3.14, `uv`, FastMCP streamable HTTP, Postgres, S3-compatible object store
(SeaweedFS in the live stack), pytest, OpenTelemetry via `kdive.observability.facade`, stdlib
`logging` with `kdive.log`'s JSON formatter.

**Expected implementation size: 380–520 changed lines (L)** — derived from the file map below:
~110 lines of instrumentation in `complete_build.py`, ~150 in the new unit-test module, ~60 in
the readout module and its tests, ~130 in the live driver, plus the Ansible and docs edits. The
design artifacts and the proof record are excluded, as the plan's own header contract requires.

## Global Constraints

Transcribed from the spec and `AGENTS.md`, values included.

- **Ruff**: line length 100; lint set `E,F,I,UP,B,SIM`. **`ty`**: strict defaults, whole tree
  (`src` + `tests`) — never narrowed to `src`.
- **Guardrails**: `just lint`, `just type`, `just test`, `just records`, `just adr-status-check`,
  `just lint-ansible`, `just test-ansible`, and `just ci` for pre-push parity. Run `just ci` as
  `just ci > FILE 2>&1 < /dev/null` (ansible-core aborts on non-blocking stdio). Never pipe a
  gate through `tail`/`head`; never append `; echo $?`.
- **Before `git commit`**: `just format` settles the ruff pair for a Python-only change. For any
  Markdown/YAML/shell in the commit, record `git diff --cached --name-only`, run `prek run`, then
  `git add --` exactly those paths. Never `git add -A` or `git add -u`.
- **Doc style**: use **Milestone**, never "Sprint". Avoid "critical", "robust",
  "comprehensive", "elegant", "significant", "essential", "crucial" in ADRs, specs, commit
  messages, and code comments.
- **ADR numbering**: `docs/adr/NNNN-kebab-title.md`, monotonic, never reused. **0655** is taken
  by this change; 0654 was the highest across `main` and every sibling worktree at branch
  creation. ADR-0504 removed the index table, so there is no index row to add.
- **Never pass an issue or PR body as a shell string.** Write it to a file and use
  `--body-file`; scan it with `just check-pr-body FILE` before publishing.
- **Error taxonomy**: pick the most specific existing `kdive.domain.errors.ErrorCategory`; never
  invent a string.
- **Redaction**: every log field is either a UUID string, an integer, a float, or a member of a
  fixed vocabulary. No store key, object path, version id, or operator-supplied string.
- **Measured constants already in the tree** (do not re-derive): `_RANGE_CHUNK_BYTES = 4 * 1024 *
  1024` (`src/kdive/build_artifacts/validation.py:57`); `SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 *
  1024` (`src/kdive/artifacts/uploads/uploads.py:9`); `_EXTERNAL_BUILD_VALIDATION_SLOTS =
  asyncio.Semaphore(1)` (`src/kdive/services/runs/complete_build.py:44`).
- **Kernel trees for the measurement** (built outside the repo, not committed):
  `/home/dave/kdive-proof-2318/build-fedora` (103-MB class) and
  `/home/dave/kdive-proof-2318/build-allmod` (2-GB class), both linux 7.2.6.

## File map

| Path | Now | After |
|---|---|---|
| `src/kdive/services/runs/complete_build.py` | prepare/validate/publish, `_VersionPinnedStore` | + `_PhaseTimer`, `_CountingStore`, `_MEASUREMENT_EVENT` record emitted once per attempt |
| `tests/services/runs/test_complete_build_measurement.py` | — | new: the five instrumentation contracts |
| `tests/integration/live_stack/measurement.py` | — | new: `MeasurementRow`, `read_measurement_record`, `ppc64le_bundle_preflight`, `bundle_for_arch` |
| `tests/integration/live_stack/test_measurement_readout.py` | — | new: parser + preflight unit tests (unmarked; run by `just test`) |
| `tests/integration/test_finalization_measurement.py` | — | new: the `live_stack` driver |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | live_vm debug toolchain + store-script deps | + kernel-build packages the driver's `combined_kernel_tar` needs, if any are undeclared |
| `docs/guide/reference/config.md` | env var table | + the driver's env vars |
| `docs/operating/runbooks/live-testing.md` | three live tiers | + how to run the measurement driver |
| `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md` | — | new: the measured rows |
| `docs/adr/0655-external-build-completion-contract.md` | — | new: the decision |

No file changes owner. `complete_build.py` already owns finalization sequencing; the timing and
counting belong to the code that sequences the phases, so this is a clean extension with no
caller migration and no obsolete path to remove.

---

## Task 1 — Instrument the three finalization phases

**Where this fits.** Everything else reads what this task emits. Nothing downstream works until
the record exists with stable field names.

**Creates:** `tests/services/runs/test_complete_build_measurement.py`
**Modifies:** `src/kdive/services/runs/complete_build.py`

### Interfaces

Provided to later tasks (field names are the wire contract Task 2's parser reads):

```python
_MEASUREMENT_EVENT = "external_build_finalization_measured"

# logged via: _log.info(_MEASUREMENT_EVENT, extra={"kdive_measurement": {...}})
# the dict's exact keys:
#   "run_id":          str   (UUID)
#   "prepare_ms":      float (monotonic, 3dp)
#   "queue_wait_ms":   float (blocked on _EXTERNAL_BUILD_VALIDATION_SLOTS only)
#   "scan_ms":         float (asyncio.to_thread(validate) only, semaphore already held)
#   "publish_ms":      float (_finalize_external_build only)
#   "total_ms":        float
#   "store_requests":  int   (validation head + get_range calls)
#   "store_bytes":     int   (bytes returned by those calls)
#   "chunked":         bool
#   "outcome":         str   ("succeeded" | "already_recorded" | an ErrorCategory value)
```

Consumed from the existing codebase (each confirmed present with this signature):

- `CompleteBuildFinalizer.complete(conn, ctx, run, *, build_id, cmdline, source_provenance)` →
  `BuildStepResult` — `complete_build.py:146`
- `_VersionPinnedStore(store, versions)` with `.head(key)` and
  `.get_range(key, *, start, length, version_id=None)` — `complete_build.py:266`
- `ValidatorStore` protocol: `head(key)` and `get_range(key, *, start, length, version_id=None)`
  — `build_artifacts/validation.py:288`
- `HeadResult` — imported in `complete_build.py` already
- `CategorizedError.category` → `ErrorCategory` — `domain/errors.py`

### Verification

- **Contract: a success emits one complete record.** `Mode: focused-test`.
  Test `tests/services/runs/test_complete_build_measurement.py::test_success_emits_measurement_record`.
  Red: `caplog` holds no record whose message is `external_build_finalization_measured`.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::test_success_emits_measurement_record -q` → `1 passed`.
- **Contract: a categorized failure emits one record carrying its category.** `Mode: focused-test`.
  Test `::test_validation_failure_emits_measurement_record`.
  Red: no record is emitted when `_validate_complete_build` raises `CategorizedError`.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::test_validation_failure_emits_measurement_record -q` → `1 passed`.
- **Contract: store counts equal the requests validation issued.** `Mode: focused-test`.
  Test `::test_store_counts_match_issued_requests`. The fake store keeps its own tally; the test
  compares the record against that tally, never against a literal.
  Red: `store_requests` is `0` while the fake's tally is non-zero.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::test_store_counts_match_issued_requests -q` → `1 passed`.
- **Contract: queue wait is separated from the scan it gates.** `Mode: focused-test`.
  Test `::test_queue_wait_excludes_scan`. A second coroutine holds
  `_EXTERNAL_BUILD_VALIDATION_SLOTS` for a known interval; the record must show that interval in
  `queue_wait_ms` and *not* in `scan_ms`.
  Red: the held interval appears in `scan_ms`, or the two are indistinguishable.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::test_queue_wait_excludes_scan -q` → `1 passed`.
- **Contract: the record's fields are a closed vocabulary.** `Mode: focused-test`.
  Test `::test_record_fields_are_closed_vocabulary`. Asserts the exact key set and each value's
  type, so a later field carrying a store key or path fails here.
  Red: an added key, or a `str` value outside `{run_id, outcome}`.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::test_record_fields_are_closed_vocabulary -q` → `1 passed`.
- **Contract: the result and committed state are unchanged.** `Mode: focused-test`.
  The existing suites are the evidence.
  Red: n/a — these pass today and must keep passing.
  Green: `uv run python -m pytest tests/services/runs/test_complete_build.py tests/adversarial/test_complete_build_concurrency.py -q` → all pass.

### Steps

1. In `src/kdive/services/runs/complete_build.py`, add the module-level event name next to
   `_EXTERNAL_BUILD_VALIDATION_SLOTS`:

   ```python
   _MEASUREMENT_EVENT = "external_build_finalization_measured"
   ```

2. Add the counting decorator below `_VersionPinnedStore`. It counts a `head` as one request
   returning zero bytes (a HEAD carries no body) and a `get_range` as one request returning
   `len(data)` bytes, so the byte total is what the object store actually transferred for
   validation:

   ```python
   @dataclass(slots=True)
   class _CountingStore:
       """Count the object-store requests and bytes one validation pass issues (#2318).

       Wraps the version-pinned store rather than replacing it: pinning decides *which* bytes
       are read and this decides nothing, so composing them keeps each to one job.
       """

       store: ValidatorStore
       requests: int = 0
       bytes_read: int = 0

       def head(self, key: str) -> HeadResult | None:
           self.requests += 1
           return self.store.head(key)

       def get_range(
           self, key: str, *, start: int, length: int, version_id: str | None = None
       ) -> bytes:
           data = self.store.get_range(key, start=start, length=length, version_id=version_id)
           self.requests += 1
           self.bytes_read += len(data)
           return data
   ```

3. Import `ValidatorStore` from `kdive.build_artifacts.validation` in the same import block that
   already pulls `validate_external_artifacts`, so the annotation resolves under `ty`.

4. Add a monotonic phase timer above `CompleteBuildFinalizer`:

   ```python
   class _PhaseTimer:
       """Accumulate per-phase monotonic durations for one finalization attempt."""

       def __init__(self) -> None:
           self.started = time.monotonic()
           self.phases: dict[str, float] = {}

       @contextmanager
       def phase(self, name: str) -> Iterator[None]:
           start = time.monotonic()
           try:
               yield
           finally:
               self.phases[name] = self.phases.get(name, 0.0) + (time.monotonic() - start)

       def ms(self, name: str) -> float:
           return round(self.phases.get(name, 0.0) * 1000.0, 3)

       def total_ms(self) -> float:
           return round((time.monotonic() - self.started) * 1000.0, 3)
   ```

   Add `import time` and `from contextlib import contextmanager` and
   `from collections.abc import Iterator` to the existing import block if absent.

5. Store the counting store on the finalizer call so `complete` can read its tallies after
   `_validate_uploads` returns. Because `CompleteBuildFinalizer` is a frozen dataclass, pass the
   timer and a mutable holder down rather than mutating `self`: change
   `_validate_uploads(self, conn, run_id, prepared, *, build_id, arch)` to accept
   `timer: _PhaseTimer` and `counts: list[_CountingStore]`, and
   `_validate_complete_build(self, manifest, keys, declared_build_id, arch, exact_versions)` to
   accept `counts: list[_CountingStore]`.

6. In `_validate_complete_build`, compose the counter over the pinned store and append it to
   `counts` so the caller can read it:

   ```python
   store = self.object_store_factory()
   counting = _CountingStore(_VersionPinnedStore(store, exact_versions))
   counts.append(counting)
   return validate_external_artifacts(
       counting,
       manifest=manifest,
       keys=keys,
       declared_build_id=declared_build_id,
       arch=arch,
   )
   ```

7. In `_validate_uploads`, split the semaphore wait from the scan:

   ```python
   with timer.phase("queue_wait"):
       await _EXTERNAL_BUILD_VALIDATION_SLOTS.acquire()
   try:
       with timer.phase("scan"):
           validated = await asyncio.to_thread(
               self._validate_complete_build,
               list(prepared.manifest_row.entries),
               prepared.keys,
               build_id,
               arch,
               final_versions,
               counts,
           )
   except CategorizedError as exc:
       raise CompleteBuildValidationError(exc) from exc
   finally:
       _EXTERNAL_BUILD_VALIDATION_SLOTS.release()
   ```

   The `async with` is replaced by an explicit acquire/release because the wait and the held
   region are two phases and `async with` cannot separate them. The `finally` keeps the release
   unconditional, which the `async with` previously guaranteed.

8. In `complete`, create the timer and holder, wrap each phase, and emit the record exactly once
   on every exit path:

   ```python
   timer = _PhaseTimer()
   counts: list[_CountingStore] = []
   outcome = "succeeded"
   try:
       with timer.phase("prepare"):
           prepared = await self._prepare(conn, run)
       validated = await self._validate_uploads(
           conn, run.id, prepared, build_id=build_id, arch=_build_arch(run),
           timer=timer, counts=counts,
       )
       with timer.phase("publish"):
           return await _finalize_external_build(
               conn, ctx, validated, cmdline=cmdline,
               source_provenance=source_provenance,
               object_store_factory=self.object_store_factory,
           )
   except _CompleteBuildAlreadyRecorded as exc:
       outcome = "already_recorded"
       return exc.result
   except CategorizedError as exc:
       outcome = exc.category.value
       raise
   finally:
       self._log_measurement(run, timer, counts, outcome=outcome, chunked=...)
   ```

   Resolve `chunked` from the `prepared` value when `_prepare` completed and `False` otherwise —
   bind it to a local initialised before the `try`.

9. Add `_log_measurement` as a module-level function (not a method — it needs no finalizer
   state), building the dict in the Interfaces block's exact key order and calling
   `_log.info(_MEASUREMENT_EVENT, extra={"kdive_measurement": payload})`.

10. Write `tests/services/runs/test_complete_build_measurement.py` with the five tests named in
    Verification. Build them on the fakes `tests/services/runs/test_complete_build.py` already
    uses — read that module first and reuse its fixtures rather than inventing a second set of
    doubles. Confirm each test fails for its stated red reason before implementing, by running
    the focused command against the unmodified source.

11. Run `just format`, then the focused command for each Verification entry, then
    `uv run python -m pytest tests/services/runs tests/adversarial/test_complete_build_concurrency.py -q`.

12. Run `just lint` and `just type`. Both exit 0.

13. Commit: `feat: attribute external-build finalization phases`.

### Acceptance criteria

- `runs.complete_build` emits exactly one `external_build_finalization_measured` record per
  attempt, on success, on `already_recorded`, and on each `CategorizedError`.
- `queue_wait_ms` covers only the semaphore acquire; `scan_ms` only the threaded validation.
- `store_requests` and `store_bytes` match a recording fake's own tally.
- `tests/services/runs/test_complete_build.py` and
  `tests/adversarial/test_complete_build_concurrency.py` pass unchanged.
- `just lint` and `just type` exit 0.

---

## Task 2 — The measurement readout and the live driver

**Where this fits.** Task 1 emits the record inside the server process. This task reads it back
out over the live-stack layout and drives the real MCP path that produces it.

**Creates:** `tests/integration/live_stack/measurement.py`,
`tests/integration/live_stack/test_measurement_readout.py`,
`tests/integration/test_finalization_measurement.py`
**Modifies:** `docs/guide/reference/config.md`, `docs/operating/runbooks/live-testing.md`

### Interfaces

Provided:

```python
# tests/integration/live_stack/measurement.py
@dataclass(frozen=True, slots=True)
class MeasurementRow:
    run_id: str
    prepare_ms: float
    queue_wait_ms: float
    scan_ms: float
    publish_ms: float
    total_ms: float
    store_requests: int
    store_bytes: int
    chunked: bool
    outcome: str

def read_measurement_record(log_path: Path, run_id: str) -> MeasurementRow | None: ...
def bundle_for_arch(arch: str, dest_dir: Path) -> Path: ...
def ppc64le_bundle_preflight() -> Path: ...          # raises pytest.skip.Exception when unset
```

Consumed from the existing codebase (each confirmed present with this signature):

- `combined_kernel_tar(kernel_src: Path, dest_dir: Path, *, arch: str = "x86_64") -> Path` —
  `tests/integration/live_stack/spine.py:428`
- `boot_member_source(kernel_src: Path, arch: str) -> Path` — same module, line 392
- `put_presigned(item: ToolResponse, path: Path) -> None` — same module, line 348. **Not**
  `put_upload_item`; that name does not exist.
- `sha256_b64(path: Path) -> str` — same module, line 343. This is the base64 SHA-256 the upload
  contract declares (S3 `x-amz-checksum-sha256`), not a hex digest.
- `accepted_run_upload_names(contract: dict[str, object]) -> set[str]` — same module, line 358
- `ok(envelope: ToolResponse, phase_name: str) -> ToolResponse` — same module, line 161
- `async scalar(client: LiveStackClient, name: str, **args: object) -> ToolResponse` — line 170
- `phase(name: str)` — an `@asynccontextmanager`, same module, line 147; used as
  `async with phase("...")`
- `mint_role_token(...)` — same module, line 546
- `LiveStackClient`, `OidcIssuer`, `mint_token` — `kdive.mcp.dev_harness`
- `EXTERNAL_BUILD_CONTRACT_URI` — `kdive.mcp.resources.external_build_contract`
- `KDIVE_HEALTH_BIND_ADDR` — `src/kdive/config/core_settings.py:782`, default
  `127.0.0.1:9464`; the server's aux listener serves `/readyz` there. The driver reads the env
  var and falls back to that default rather than hard-coding a port.
- `_MEASUREMENT_EVENT = "external_build_finalization_measured"` — Task 1

Env contract the driver reads:

- `KDIVE_KERNEL_SRC` — built x86_64 kernel tree (existing var)
- `KDIVE_PPC64LE_BUNDLE` — directory holding `kernel.tar.gz` (existing var, #1146)
- `KDIVE_STACK_LOG_DIR` — defaults to `<repo>/.live-stack-logs`, matching
  `scripts/live-stack/lib.sh:11`
- `KDIVE_MEASUREMENT_OUT` — path to write the JSON rows; unset → rows go to stdout only
- `KDIVE_MEASUREMENT_BUDGET_S` — the client request budget the driver sets and records;
  default `600`

### Verification

- **Contract: the parser recovers a row from a server log line.** `Mode: focused-test`.
  Test `tests/integration/live_stack/test_measurement_readout.py::test_reads_measurement_row`,
  over a fixture line written in the JSON shape `kdive.log.JsonFormatter` produces — read that
  formatter and build the fixture from it rather than guessing the envelope.
  Red: `read_measurement_record` returns `None` for a line that holds the record.
  Green: `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py::test_reads_measurement_row -q` → `1 passed`.
- **Contract: the parser ignores records for other runs.** `Mode: focused-test`.
  Test `::test_ignores_other_run_ids`.
  Red: a row is returned for a non-matching `run_id`.
  Green: `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py::test_ignores_other_run_ids -q` → `1 passed`.
- **Contract: the ppc64le arm skips unset and fails loudly when set-but-unusable.**
  `Mode: focused-test`. Test `::test_ppc64le_arm_preflight`, parameterized over unset, set-to-a-
  missing-path, and set-to-a-directory-without-`kernel.tar.gz`.
  Red: a missing path skips instead of raising.
  Green: `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py::test_ppc64le_arm_preflight -q` → `3 passed`.
- **Contract: the `live_stack` driver.** `Mode: task-test-not-applicable`. Its observable
  contract is the live measurement against a running stack, which `just test` deliberately
  excludes by marker. Its two separable pieces — the parser and the preflight — are covered by
  the three entries above; the driver body's evidence is the recorded run in Task 3.

### Steps

1. Read `src/kdive/log.py`'s `JsonFormatter` and record the exact envelope it emits for a record
   carrying an `extra` dict. The parser must match that shape, not an assumed one.

2. Create `tests/integration/live_stack/measurement.py` with `MeasurementRow` and
   `read_measurement_record`. The reader scans the log file line by line, parses each as JSON,
   skips lines that do not parse, selects those whose message equals
   `external_build_finalization_measured`, and returns the row whose `run_id` matches — the last
   such row, so a retried finalization reports its final attempt.

3. Add `bundle_for_arch(arch, dest_dir)`: for `x86_64`, read `KDIVE_KERNEL_SRC` and call
   `combined_kernel_tar(Path(src), dest_dir, arch="x86_64")`; for `ppc64le`, call
   `ppc64le_bundle_preflight()` and return its `kernel.tar.gz` directly. Raise `RuntimeError`
   naming the env var for any other arch.

4. Add `ppc64le_bundle_preflight()` mirroring `_ppc64le_bundle_preflight` in
   `tests/integration/test_live_stack.py`: unset → `pytest.skip` with the exact fix; set but
   missing or lacking `kernel.tar.gz` → raise, never skip.

5. Write `tests/integration/live_stack/test_measurement_readout.py` with the three tests above.
   Leave it **unmarked** so `just test` runs it. Confirm each red before implementing.

6. Create `tests/integration/test_finalization_measurement.py`, marked
   `@pytest.mark.live_stack`, parameterized over `("x86_64", "ppc64le")`. Each case:
   a. preflight the stack the way `tests/integration/test_live_stack.py` does, skipping cleanly
      when it is absent;
   b. `GET http://{KDIVE_HEALTH_BIND_ADDR or 127.0.0.1:9464}/readyz` and record `version`,
      `commit`, `started_at` from its deployed-version fact (ADR-0482 §1);
   c. `bundle_for_arch(arch, tmp_path)`, then record the tar's byte size and its member count
      (`tarfile.open(path).getmembers()` length, counted once, outside the timed region);
   d. mint an operator token, `investigations.open(project, title)`, then
      `runs.create(investigation_id=..., build_profile=...)` with **no** `system_id`;
   e. read `EXTERNAL_BUILD_CONTRACT_URI` and assert `accepted_run_upload_names` contains
      `kernel`, then `artifacts.create_run_upload` declaring one `kernel` item with
      `sha256_b64(tar)` and the tar's `size_bytes` and **no** `chunks` (both classes are under
      the 5 GiB `SINGLE_PUT_MAX_BYTES`, so single-PUT is the supported shape), then
      `put_presigned(item, tar)`;
   f. set the client request budget from `KDIVE_MEASUREMENT_BUDGET_S`, record it, and time
      `runs.complete_build(run_id=...)` with `time.monotonic()` around the call;
   g. read the server record with `read_measurement_record`, asserting it is present and its
      `run_id` matches;
   h. assemble the row — arch, bundle bytes, member count, client elapsed, client budget,
      deployed revision, and every server-side field — and append it to `KDIVE_MEASUREMENT_OUT`
      as one JSON object per line, also printing it.

7. Assert only what the driver must guarantee: the record is present, `outcome` is `succeeded`,
   and `queue_wait_ms + scan_ms + publish_ms <= total_ms`. Do **not** assert a duration
   threshold — the driver measures, it does not gate.

8. Add the four env vars to the table in `docs/guide/reference/config.md`, matching the
   surrounding rows' wording and column order.

9. Add a section to `docs/operating/runbooks/live-testing.md` naming the driver, its env
   contract, and the exact command to run it, beside the existing tier sections.

10. Run `just format`, then `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py -q` → `5 passed`.

11. Stage the changed paths, record `git diff --cached --name-only`, run `prek run`, re-add
    exactly those paths (Markdown is in this commit), then `just lint` and `just type`.

12. Commit: `test: add the external-build finalization measurement driver`.

### Acceptance criteria

- `read_measurement_record` recovers a row from a real `JsonFormatter` line and ignores others.
- The ppc64le preflight skips when unset and raises when set-but-unusable.
- The driver is `live_stack`-marked, so `just test` does not collect it.
- `docs/guide/reference/config.md` documents all four env vars.
- `just lint` and `just type` exit 0.

---

## Task 3 — Run the measurement and write the proof record

**Where this fits.** This produces the evidence Task 4's decision rests on.

**Creates:** `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md`
**Modifies:** `deploy/ansible/roles/live_vm_host/tasks/main.yml` (only if the run needs a host
package the role does not declare)

### Interfaces

Consumed: everything Task 2 provides, plus the built kernel trees named in Global Constraints.
Provides to Task 4: the measured rows, as the proof record's table.

### Verification

- **Contract: any new host prerequisite is declared in the role that owns its layer.**
  `Mode: focused-test`. Run `just lint-ansible` and `just test-ansible` after the edit.
  Red: the role fails its own assertions with the package absent.
  Green: `just lint-ansible` → exit 0; `just test-ansible` → exit 0.
- **Contract: the proof record's contents.** `Mode: task-test-not-applicable`. It is a
  human-readable record of measured values with no executable consumer; a test over it would
  assert prose wording, which this plan forbids.

### Steps

1. Wait for both kernel builds to finish. Confirm each tree has its boot member:
   `ls -la /home/dave/kdive-proof-2318/build-fedora/arch/x86/boot/bzImage` and the same under
   `build-allmod`. Expect a regular file in each.

2. Bring the stack up: `just stack-up`, then `scripts/live-stack/up.sh`. Expect
   `Backends healthy and schema migrated.` and a `/readyz` that answers.

3. Run the driver for the 103-MB class:
   `KDIVE_KERNEL_SRC=/home/dave/kdive-proof-2318/build-fedora KDIVE_MEASUREMENT_OUT=<path> uv run python -m pytest tests/integration/test_finalization_measurement.py -m live_stack -k x86_64 -q`.
   Expect `1 passed`, `1 skipped` (the ppc64le arm) and one JSON row.

4. Record the bundle's actual compressed size. If it is materially off the 103-MB class, adjust
   the config (the `fedora` variant in `/home/dave/kdive-proof-2318/build.sh`) and rebuild rather
   than reporting a different size under the class name.

5. Repeat step 3 with `KDIVE_KERNEL_SRC=/home/dave/kdive-proof-2318/build-allmod` for the 2-GB
   class. Expect the same shape, over a longer wall-clock.

6. If either run needed a host package that `deploy/ansible/roles/live_vm_host/tasks/main.yml`
   does not declare, add it to that role in this change and run the two Ansible gates. If no new
   package was needed, record that explicitly in the proof record and make no role edit.

7. Write the proof record. It carries: the exact command for each run; the deployed revision from
   `/readyz`; the host's CPU count, kernel, and object-store backend; per row — arch, kernel tree
   and config, bundle compressed bytes, member count, client request budget, client elapsed,
   `prepare_ms`, `queue_wait_ms`, `scan_ms`, `publish_ms`, `total_ms`, `store_requests`,
   `store_bytes`, `chunked`, and `outcome`; and a plain statement that both bundles were
   single-PUT (under the 5 GiB `SINGLE_PUT_MAX_BYTES`), so no chunk reassembly is in the measured
   path. State the ppc64le arm as not run, with the reason and the owner.

8. Do not attribute elapsed time to decompression, and do not state a size threshold. Report
   which phase dominates each row and nothing beyond what the rows show.

9. Stage, run `prek run`, re-add exactly the staged paths, commit:
   `docs: record external-build finalization measurements`.

### Acceptance criteria

- Two measured rows exist, one per bundle class, each carrying every field #2318 names.
- The proof record names the deployed revision, host facts, and both bundles' actual sizes.
- The ppc64le arm is recorded as not run, with its reason and owner.
- `just lint-ansible` and `just test-ansible` exit 0 if the role was touched.

---

## Task 4 — ADR 0655: the completion contract

**Where this fits.** The decision #2318 exists to make, and #2319's gate.

**Creates:** `docs/adr/0655-external-build-completion-contract.md`

### Interfaces

Consumed: the proof record's rows from Task 3. Provides: the accepted decision #2319 reads.

### Verification

- **Contract: the record's shape and status pass the repo's record gates.** `Mode: focused-test`.
  Red: a malformed record, a reused number, or a `Proposed` status cited from `src/`/`tests/`.
  Green: `just records` → exit 0; `just adr-status-check` → exit 0.

### Steps

1. Read the two rows. Identify which phase dominates each and whether the total fits inside the
   recorded client request budget with margin.

2. Write `docs/adr/0655-external-build-completion-contract.md` with exactly the five sections
   `## Status`, `## Context`, `## Decision`, `## Consequences`, `## Considered & rejected`.
   Set `- **Status:** Accepted` — the decision ships with this PR.

3. `## Context` cites the proof record by repo-relative path and states the measured attribution
   in two or three sentences. No re-derivation of #2314's analysis.

4. `## Decision` picks synchronous or durable asynchronous completion from the rows, then defines
   all five contract elements the issue requires: retry and idempotency, cancellation,
   upload-window fencing (preserving `complete_build.py:553-586`), publication ownership, and
   recovery after transport interruption.

5. `## Consequences` states what #2319 must do — implement or close with this evidence — and names
   the outstanding ppc64le confirmation plus the specific result that would reopen the decision.

6. `## Considered & rejected` carries one bullet per alternative, each tagged `verified:` with the
   command and its result plus the environment it ran in, or `judgment:`. The alternative not
   taken in step 4 is one bullet; "do nothing — keep the contract undocumented" is another.

7. Size the record to the decision. Do not argue for it at greater length than it governs.

8. Run `just records` and `just adr-status-check`. Both exit 0.

9. Stage, run `prek run`, re-add exactly the staged paths, commit:
   `docs: decide the external-build completion contract`.

### Acceptance criteria

- ADR 0655 exists, is `Accepted`, and carries the five sections.
- Its decision is traceable to the proof record's rows.
- Every `Considered & rejected` bullet carries a `verified:` or `judgment:` tag.
- `just records` and `just adr-status-check` exit 0.

---

## Deferrals

None carried into this plan yet. A `$trial-loop` deferral on this branch is appended here with
its owning record path or tracker issue.
