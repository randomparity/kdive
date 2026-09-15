# External-build finalization measurement — implementation plan (#2318)

**Goal.** Make `runs.complete_build`'s finalization phases separately observable, measure them
over the real MCP path for a 103-MB-class and a 2-GB-class x86_64 kernel bundle, and record the
completion-contract decision those measurements select against the supported 30-second client
request budget.

**Architecture.** `CompleteBuildFinalizer.complete` runs prepare → (reassemble) → validate →
publish in sequence (`src/kdive/services/runs/complete_build.py:158-169`). Task 1 wraps each
phase with a monotonic clock, counts object-store requests via a decorator composed over the
existing `_VersionPinnedStore` wrapping point, and emits one structured record per attempt whose
payload rides in the log **message** — the only channel both of this repo's serializers carry.
Task 2 adds a `live_stack` driver exercising that path over MCP HTTP with an **unbound** Run — no
allocation, no System, no VM. Tasks 3 and 4 run it and write ADR 0655 from the rows.

**Tech stack.** Python 3.14, `uv`, FastMCP streamable HTTP (fastmcp 3.4.4), Postgres,
S3-compatible object store (SeaweedFS in the live stack), pytest, OpenTelemetry via
`kdive.observability.facade`, stdlib `logging` with `kdive.log`'s JSON formatter.

**Expected implementation size: 470–640 changed lines (L)** — derived from the file map below:
~130 lines of instrumentation in `complete_build.py`, ~230 in the new unit-test module (seven
tests, of which the counting test carries its own non-injected finalizer and bundle fixture), ~80
in the readout module and its three tests, ~150 in the live driver, plus the Ansible and docs
edits. Revised upward from an earlier 380–520 after review established that the counting test
cannot reuse the existing injected-validator fixtures. Design artifacts and the proof record are
excluded.

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
  "comprehensive", "elegant", "significant", "essential", "crucial".
- **ADR numbering**: `docs/adr/<NNNN>-kebab-title.md`, monotonic, never reused. **0655** is taken
  by this change; 0654 was the highest across `main` and every sibling worktree at branch
  creation. ADR-0504 removed the index table, so there is no index row to add.
- **Never pass an issue or PR body as a shell string.** Write it to a file and use
  `--body-file`; scan it with `just check-pr-body FILE`.
- **Error taxonomy**: pick the most specific existing `kdive.domain.errors.ErrorCategory`.
- **Redaction**: every payload value is a UUID string, an integer, a float, a bool, or a member
  of a fixed vocabulary. No store key, object path, version id, or operator-supplied string.
- **The supported client request budget is 300 seconds.** `LiveStackClient.over_http`
  (`src/kdive/mcp/dev_harness.py:203-208`) passes no timeout, so the MCP SDK applies
  `Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)` and no session-level timeout; a long
  finalization is bounded by **read**. This is the decision threshold. It is **not** the timeout
  the driver runs under — a driver bounded by it could not record a finalization that breached it.

  > **Correction (2026-09-15).** This constraint originally read "30 seconds", taking the
  > *connect* default for the request bound. Tasks 3 and 4 were executed against the wrong
  > threshold and the resulting ADR selected the opposite branch; the branch review caught it and
  > the arms were re-run. The rest of this plan is left as written — it is the point-in-time
  > record of what was planned — but every artifact it produced carries the corrected figure.
- **Measured constants already in the tree** (do not re-derive): `_RANGE_CHUNK_BYTES = 4 * 1024 *
  1024` (`build_artifacts/validation.py:57`); `SINGLE_PUT_MAX_BYTES = 5 * 1024 * 1024 * 1024`
  (`artifacts/uploads/uploads.py:9`); `_EXTERNAL_BUILD_VALIDATION_SLOTS = asyncio.Semaphore(1)`
  (`complete_build.py:44`); `CONTEXT_FIELDS = ("request_id", "job_id", "principal", "object_id",
  "transition")` (`log.py:27`) — the closed set `bind_context` accepts.
- **Kernel trees for the measurement** (built outside the repo, not committed):
  `/home/dave/kdive-proof-2318/build-fedora` (103-MB class) and
  `/home/dave/kdive-proof-2318/build-allmod` (2-GB class), both linux 7.2.6. Their config
  derivation is recorded in the proof record so another host can regenerate them.

## File map

| Path | Now | After |
|---|---|---|
| `src/kdive/services/runs/complete_build.py` | prepare/validate/publish, `_VersionPinnedStore` | + `_PhaseTimer`, `_CountingStore`, `_MEASUREMENT_EVENT` record emitted once per attempt |
| `tests/services/runs/test_complete_build_measurement.py` | — | new: the seven instrumentation contracts |
| `tests/integration/live_stack/measurement.py` | — | new: `MeasurementRow`, `read_measurement_record`, `bundle_for_arch`, `ppc64le_bundle_preflight` |
| `tests/integration/live_stack/test_measurement_readout.py` | — | new: parser + preflight unit tests (unmarked; run by `just test`) |
| `tests/integration/test_finalization_measurement.py` | — | new: the `live_stack` driver |
| `deploy/ansible/roles/libvirt_stack/defaults/main.yml` | per-family virtualization package lists | + `make` and `tar` in all three lists |
| `docs/guide/reference/config.md` | env var table | + the driver's env vars |
| `docs/operating/runbooks/live-testing.md` | three live tiers | + how to run the measurement driver |
| `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md` | — | new: the measured rows |
| `docs/adr/0656-external-build-completion-contract.md` | — | new: the decision |

No file changes owner. `complete_build.py` already owns finalization sequencing, so the timing
and counting are a clean extension with no caller migration and no obsolete path to remove.

---

## Task 1 — Instrument the finalization phases

**Where this fits.** Everything else reads what this task emits. Nothing downstream works until
the record exists with a stable wire shape.

**Creates:** `tests/services/runs/test_complete_build_measurement.py`
**Modifies:** `src/kdive/services/runs/complete_build.py`

### Interfaces

Provided to later tasks. **The payload rides in the message**, because both serializers in this
repo build a closed payload and merge only the `bind_context` fields: `JsonFormatter.format`
(`log.py:78-90`) merges `_kdive_ctx`, and `format_log_record_json`
(`observability/stdout_exporter.py:37-66`) merges `_domain_context`, which keeps only
`CONTEXT_FIELDS`. A `logging` `extra=` attribute reaches neither, and on the OTel path a nested
dict is not a valid attribute value.

```python
_MEASUREMENT_EVENT = "external_build_finalization_measured"

# emitted as:
#   _log.info("%s %s", _MEASUREMENT_EVENT, json.dumps(payload, sort_keys=True))
# so the serialized line's "msg" is:
#   external_build_finalization_measured {"chunked": false, "outcome": "succeeded", ...}
#
# payload keys:
#   "run_id":          str   (UUID)
#   "prepare_ms":      float (3dp)
#   "reassemble_ms":   float (chunk reassembly; 0.0 on the single-PUT path)
#   "queue_wait_ms":   float (blocked on _EXTERNAL_BUILD_VALIDATION_SLOTS only)
#   "scan_ms":         float (asyncio.to_thread(validate) only, semaphore already held)
#   "publish_ms":      float (_finalize_external_build only)
#   "total_ms":        float
#   "store_requests":  int   (validation head + get_range calls)
#   "store_bytes":     int   (bytes returned by those calls)
#   "chunked":         bool
#   "outcome":         str   (see the closed vocabulary below)
```

`outcome` vocabulary — every exit path `complete` has. The three service exceptions are plain
`Exception` dataclasses, **not** `CategorizedError` (`complete_build.py:108-131`), and
`_validate_uploads` converts every raised `CategorizedError` into `CompleteBuildValidationError`
before it leaves (`complete_build.py:216-222`), so a bare `except CategorizedError` arm would
never fire:

| Exit | `outcome` |
|---|---|
| returned normally | `"succeeded"` |
| `_CompleteBuildAlreadyRecorded` | `"already_recorded"` |
| `CompleteBuildValidationError` | `exc.error.category.value` |
| `CompleteBuildExpiredWindowError` | `"upload_window_expired"` |
| `CompleteBuildConfigurationError` | `str(exc.data.get("reason", "configuration_error"))` |
| anything else, including `CancelledError` | `"unexpected"` |

Consumed from the existing codebase (each confirmed present with this signature):

- `CompleteBuildFinalizer.complete(conn, ctx, run, *, build_id, cmdline, source_provenance)` →
  `BuildStepResult` — `complete_build.py:146`
- `_VersionPinnedStore(store, versions)` with `.head(key)` and
  `.get_range(key, *, start, length, version_id=None)` — `complete_build.py:266`
- `ValidatorStore` protocol: `head(key)`, `get_range(key, *, start, length, version_id=None)` —
  `build_artifacts/validation.py:288`
- `HeadResult` — already imported at `complete_build.py:19`
- `_build_arch(run) -> str` — `complete_build.py:454`
- `CategorizedError` with `.category: ErrorCategory` (a `StrEnum`, so `.value` is the wire
  string) — `domain/errors.py:16,145`
- `_reassemble_chunked_artifacts(...)` — called at `complete_build.py:207`

### Verification

Every entry's green command is
`uv run python -m pytest tests/services/runs/test_complete_build_measurement.py::<name> -q`,
expecting `1 passed`.

- **Contract: a success emits one complete record.** `Mode: focused-test`.
  `::test_success_emits_measurement_record`. Red: no line in `caplog` whose message starts with
  `external_build_finalization_measured`.
- **Contract: a validation failure records its category.** `Mode: focused-test`.
  `::test_validation_failure_records_its_category`. Red: `outcome` is `succeeded`.
- **Contract: a configuration failure records its reason.** `Mode: focused-test`.
  `::test_configuration_failure_records_its_reason`, driven by a missing upload manifest so
  `_prepare` raises `CompleteBuildConfigurationError`. Red: `outcome` is `succeeded`. This arm is
  distinct from the previous one — the two exceptions travel different `except` clauses.
- **Contract: nothing uncategorized is recorded as success.** `Mode: focused-test`.
  `::test_unexpected_exception_records_unexpected`, with the publish step raising `RuntimeError`.
  Red: `outcome` is `succeeded`.
- **Contract: store counts equal the requests validation issued.** `Mode: focused-test`.
  `::test_store_counts_match_issued_requests`. Red: `store_requests` is 0 while the fake's tally
  is non-zero. **This test cannot use the module's usual fixtures** — see step 11.
- **Contract: queue wait is separated from the scan it gates.** `Mode: focused-test`.
  `::test_queue_wait_excludes_scan`. A second coroutine holds the semaphore for a known interval;
  that interval must appear in `queue_wait_ms` and not in `scan_ms`. Red: it appears in `scan_ms`.
- **Contract: the payload is a closed vocabulary.** `Mode: focused-test`.
  `::test_record_fields_are_closed_vocabulary`. Asserts the exact key set and each value's type.
  Red: an added key, or a `str` value outside `{run_id, outcome}`.
- **Contract: result and committed state unchanged.** `Mode: focused-test`. Green:
  `uv run python -m pytest tests/services/runs/test_complete_build.py tests/adversarial/test_complete_build_concurrency.py -q`
  → all pass. Red: n/a — these pass today and must keep passing.

### Steps

1. In `src/kdive/services/runs/complete_build.py`, add beside `_EXTERNAL_BUILD_VALIDATION_SLOTS`:

   ```python
   _MEASUREMENT_EVENT = "external_build_finalization_measured"
   ```

2. Add `import json`, `import time`, `from contextlib import contextmanager`, and
   `from collections.abc import Iterator` to the existing import block if absent, and import
   `ValidatorStore` alongside `validate_external_artifacts` at line 24.

3. Add the counting decorator below `_VersionPinnedStore`. A `head` is one request returning zero
   bytes; a `get_range` is one request returning `len(data)`:

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

5. `CompleteBuildFinalizer` is a frozen dataclass, so thread the timer and a mutable holder
   through rather than mutating `self`. Change `_validate_uploads(self, conn, run_id, prepared,
   *, build_id, arch)` to also accept `timer: _PhaseTimer` and `counts: list[_CountingStore]`,
   and `_validate_complete_build(self, manifest, keys, declared_build_id, arch, exact_versions)`
   to also accept `counts: list[_CountingStore]`.

6. In `_validate_complete_build`, compose the counter over the pinned store and publish it:

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

7. In `_validate_uploads`, time the chunk reassembly branch with
   `with timer.phase("reassemble"):` around the `_reassemble_chunked_artifacts` call, so the
   chunked path's cost lands in a named phase instead of only in `total_ms`.

8. Still in `_validate_uploads`, split the semaphore wait from the scan. `async with` cannot
   separate them, so acquire explicitly and keep the release unconditional:

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

9. In `complete`, create the timer and holder, wrap prepare and publish, and emit the record once
   on every exit path. Use the `outcome` table from Interfaces — one `except` arm per exception
   type, plus a `BaseException` arm so a crashed or cancelled attempt is never a success:

   ```python
   timer = _PhaseTimer()
   counts: list[_CountingStore] = []
   outcome = "succeeded"
   chunked = False
   try:
       with timer.phase("prepare"):
           prepared = await self._prepare(conn, run)
       chunked = prepared.has_chunks
       validated = await self._validate_uploads(
           conn,
           run.id,
           prepared,
           build_id=build_id,
           arch=_build_arch(run),
           timer=timer,
           counts=counts,
       )
       with timer.phase("publish"):
           return await _finalize_external_build(
               conn,
               ctx,
               validated,
               cmdline=cmdline,
               source_provenance=source_provenance,
               object_store_factory=self.object_store_factory,
           )
   except _CompleteBuildAlreadyRecorded as exc:
       outcome = "already_recorded"
       return exc.result
   except CompleteBuildValidationError as exc:
       outcome = exc.error.category.value
       raise
   except CompleteBuildExpiredWindowError:
       outcome = "upload_window_expired"
       raise
   except CompleteBuildConfigurationError as exc:
       outcome = str(exc.data.get("reason", "configuration_error"))
       raise
   except BaseException:
       outcome = "unexpected"
       raise
   finally:
       _log_measurement(run, timer, counts, outcome=outcome, chunked=chunked)
   ```

10. Add `_log_measurement` as a module-level function — it needs no finalizer state — building
    the payload in the Interfaces key order and emitting it into the message:

    ```python
    def _log_measurement(
        run: Run,
        timer: _PhaseTimer,
        counts: list[_CountingStore],
        *,
        outcome: str,
        chunked: bool,
    ) -> None:
        """Emit one finalization measurement record (#2318).

        The payload rides in the message because both serializers merge only the
        ``bind_context`` fields, and ``bind_context`` rejects any name outside
        ``CONTEXT_FIELDS``; a ``logging`` ``extra=`` attribute reaches neither.
        """
        payload = {
            "run_id": str(run.id),
            "prepare_ms": timer.ms("prepare"),
            "reassemble_ms": timer.ms("reassemble"),
            "queue_wait_ms": timer.ms("queue_wait"),
            "scan_ms": timer.ms("scan"),
            "publish_ms": timer.ms("publish"),
            "total_ms": timer.total_ms(),
            "store_requests": sum(c.requests for c in counts),
            "store_bytes": sum(c.bytes_read for c in counts),
            "chunked": chunked,
            "outcome": outcome,
        }
        _log.info("%s %s", _MEASUREMENT_EVENT, json.dumps(payload, sort_keys=True))
    ```

11. Write `tests/services/runs/test_complete_build_measurement.py`. Split it deliberately:

    - The six record-shape tests reuse the injected-validator fixtures
      `tests/services/runs/test_complete_build.py` already uses — read that module first.
    - `test_store_counts_match_issued_requests` **cannot**: every finalizer there is built as
      `CompleteBuildFinalizer(validate_complete_build=FakeValidator(...))`, and
      `_validate_complete_build` returns from that injection *before* it constructs the store
      (`complete_build.py:248-257`), so no counter is ever created and `store_requests` would be
      permanently 0 whether or not the instrumentation works. Build that one test on a finalizer
      constructed **without** `validate_complete_build`, over an `object_store_factory` returning
      a recording store that serves `valid_combined_kernel_tar()` — the public helper already in
      `tests/mcp/complete_build_support.py:41`, which returns a structurally valid x86_64 bundle
      (gzip tar with a `HdrS` `boot/vmlinuz` and a `lib/modules/6.9.0/` tree). Use that rather
      than the private `_combined_kernel_tar`/`_boot_elf`/`_FakeStore` in
      `tests/providers/local_libvirt/test_validate_external_artifacts.py`: the support module is
      the shared surface this test tree already imports from, and reaching into another module's
      privates would couple two test modules for no gain. The recording store still has to be
      written here — it wraps the bundle bytes with `head`/`get_range` and counts both.

12. Confirm each test fails for its stated red reason against the unmodified source before
    implementing.

13. Run `just format`, then each Verification command, then
    `uv run python -m pytest tests/services/runs tests/adversarial/test_complete_build_concurrency.py -q`.

14. Run `just lint` and `just type`. Both exit 0.

15. Commit: `feat: attribute external-build finalization phases`.

### Acceptance criteria

- Exactly one record per attempt, on success, `already_recorded`, each of the three service
  exceptions, and anything uncategorized.
- `queue_wait_ms` covers only the semaphore acquire; `scan_ms` only the threaded validation;
  `reassemble_ms` only the chunked branch.
- `store_requests`/`store_bytes` match a recording fake's own tally, proven on the non-injected
  path.
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
    reassemble_ms: float
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
def ppc64le_bundle_preflight() -> Path: ...
```

Consumed from the existing codebase (each confirmed present with this signature):

- `combined_kernel_tar(kernel_src: Path, dest_dir: Path, *, arch: str = "x86_64") -> Path` —
  `tests/integration/live_stack/spine.py:428`. It runs `make -C <src> modules_install
  INSTALL_MOD_PATH=<dest_dir>/modstage` and then `tar -czf`, so `dest_dir` holds both the staged
  module tree and the tar.
- `boot_member_source(kernel_src: Path, arch: str) -> Path` — same module, line 392
- `put_presigned(item: ToolResponse, path: Path) -> None` — same module, line 348. **Not**
  `put_upload_item`; that name does not exist.
- `sha256_b64(path: Path) -> str` — same module, line 343; the base64 SHA-256 the upload contract
  declares (S3 `x-amz-checksum-sha256`), not a hex digest.
- `accepted_run_upload_names(contract: dict[str, object]) -> set[str]` — same module, line 358
- `ok(envelope: ToolResponse, phase_name: str) -> ToolResponse` — same module, line 161
- `async scalar(client: LiveStackClient, name: str, **args: object) -> ToolResponse` — line 170
- `phase(name: str)` — an `@asynccontextmanager`, same module, line 147
- `mint_role_token(...)` — same module, line 546
- `LiveStackClient.over_http(base_url, token)` — `kdive/mcp/dev_harness.py:203`. It passes **no**
  timeout, so it runs at MCP's 30 s default. The driver must build its own client with an
  explicit larger timeout (`fastmcp.Client(transport, timeout=...)`) or the 2-GB arm cannot
  complete; the 30 s figure is recorded as the budget, never used to bound the run.
- `format_log_record_json(record: ReadableLogRecord) -> str` —
  `kdive/observability/stdout_exporter.py:37`
- `EXTERNAL_BUILD_CONTRACT_URI` — `kdive.mcp.resources.external_build_contract`
- `KDIVE_HEALTH_BIND_ADDR` — `config/core_settings.py:782`, default `127.0.0.1:9464`; the
  server's aux listener serves `/readyz` there.
- `_MEASUREMENT_EVENT` and the payload shape — Task 1

Env contract the driver reads:

| Var | Default | Meaning |
|---|---|---|
| `KDIVE_KERNEL_SRC` | — | built x86_64 kernel tree (existing var) |
| `KDIVE_PPC64LE_BUNDLE` | — | directory holding `kernel.tar.gz` (existing var, #1146) |
| `KDIVE_STACK_LOG_DIR` | `<repo>/.live-stack-logs` | matches `scripts/live-stack/lib.sh:11` |
| `KDIVE_MEASUREMENT_OUT` | unset → stdout only | path to append JSON rows to |
| `KDIVE_MEASUREMENT_STAGE_DIR` | pytest `tmp_path` | where `combined_kernel_tar` stages; see step 7 |
| `KDIVE_MEASUREMENT_TIMEOUT_S` | `1800` | the timeout the driver **sets**, so the run completes |

The budget the decision is tested against is **not** an environment variable. Define it as a
module constant in `measurement.py`:

```python
SUPPORTED_BUDGET_S = 30.0
"""MCP's default client request timeout, which `LiveStackClient.over_http` does not override.

A constant rather than an env var on purpose: the spec and ADR 0655 both rest on this being a
property of the shipped client, so a knob would let the environment running the proof move the
threshold the accepted decision rests on.
"""
```

Record it in every row alongside the timeout actually used.

### Verification

- **Contract: the parser recovers a row from the serializer the server runs.**
  `Mode: focused-test`. `tests/integration/live_stack/test_measurement_readout.py::test_reads_row_from_otel_exporter`.
  Build the fixture by constructing a `ReadableLogRecord` and running it through
  `format_log_record_json` — the same code the server runs — not by hand-writing an envelope.
  Red: `read_measurement_record` returns `None` for a line that holds the record.
  Green: `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py::test_reads_row_from_otel_exporter -q` → `1 passed`.
- **Contract: the parser ignores records for other runs.** `Mode: focused-test`.
  `::test_ignores_other_run_ids`. Red: a row is returned for a non-matching `run_id`.
- **Contract: the ppc64le arm skips unset and raises when set-but-unusable.**
  `Mode: focused-test`. `::test_ppc64le_arm_preflight`, parameterized over unset, set-to-a-missing
  -path, and set-to-a-directory-without-`kernel.tar.gz`. Red: a missing path skips instead of
  raising. Green: → `3 passed`.
- **Contract: the `live_stack` driver.** `Mode: task-test-not-applicable`. Its observable contract
  is the live measurement against a running stack, which `just test` excludes by marker. Its two
  separable pieces — parser and preflight — are the three entries above; the driver body's
  evidence is the recorded run in Task 3.

### Steps

1. Create `tests/integration/live_stack/measurement.py` with `MeasurementRow` and
   `read_measurement_record`. The reader scans the log file line by line, parses each as JSON,
   skips lines that do not parse, selects those whose `msg` **starts with**
   `external_build_finalization_measured `, `json.loads` the remainder of that message, and
   returns the row whose `run_id` matches — the last such row, so a retried finalization reports
   its final attempt. Key on `msg`, never on `logger`: the OTel exporter sets `logger` to the
   instrumentation-scope name, not `record.name`.

2. Add `bundle_for_arch(arch, dest_dir)`: for `x86_64`, read `KDIVE_KERNEL_SRC` and call
   `combined_kernel_tar(Path(src), dest_dir, arch="x86_64")`; for `ppc64le`, call
   `ppc64le_bundle_preflight()` and return its `kernel.tar.gz` directly. Raise `RuntimeError`
   naming the env var for any other arch.

3. Add `ppc64le_bundle_preflight()`: unset → `pytest.skip` with the exact fix; set but missing, or
   lacking `kernel.tar.gz` → raise, never skip. Document in its docstring that this **diverges**
   from `_ppc64le_bundle_preflight` (`tests/integration/test_live_stack.py:1023-1035`), which
   skips in both branches and also requires `initrd.img`: a measurement harness must not silently
   produce no row, and this arm boots nothing.

4. Write `tests/integration/live_stack/test_measurement_readout.py` with the three tests above.
   Leave it **unmarked** so `just test` runs it. Confirm each red before implementing.

5. Create `tests/integration/test_finalization_measurement.py`, marked
   `@pytest.mark.live_stack`, parameterized over `("x86_64", "ppc64le")`.

6. Each case, in order:
   a. preflight the stack as `tests/integration/test_live_stack.py` does, skipping cleanly when
      it is absent;
   b. `GET http://{KDIVE_HEALTH_BIND_ADDR or 127.0.0.1:9464}/readyz` and record `version`,
      `commit`, `started_at` (ADR-0482 §1);
   c. `bundle_for_arch(arch, stage_dir)`, then record the tar's byte size and its member count
      (`len(tarfile.open(path).getmembers())`), counted once, outside the timed region;
   d. mint an operator token, `investigations.open(project, title)`, then
      `runs.create(investigation_id=..., build_profile={"schema_version": 1, "arch": arch},
      target_kind=...)` with **no** `system_id`. Both extra arguments are required:
      `target_kind` because `runs.create` requires it whenever `system_id` is omitted
      (`registrar.py:265-273`), and the profile `arch` because the server validates the bundle
      against `run.build_profile["arch"]` via `_build_arch`, which defaults to `x86_64` — so the
      ppc64le arm would be rejected rather than measured. Use `target_kind="local-libvirt"` —
      `ResourceKind.LOCAL_LIBVIRT` (`src/kdive/domain/catalog/resources.py:20`), the kind the
      default production resolver registers. Note that no existing `live_stack` case models this
      call: every one of them passes `system_id` and so never supplies `target_kind`. The
      `build_profile` shape is copied from `tests/integration/test_live_stack.py:1121`
      (`{"schema_version": 1, "arch": "ppc64le"}`); `target_kind` has no such precedent, and a
      rejection here echoes the registered kinds in `available_target_kinds` (ADR-0169), which is
      the fallback if `local-libvirt` is not registered in the measurement deployment;
   e. read `EXTERNAL_BUILD_CONTRACT_URI`, assert `accepted_run_upload_names` contains `kernel`,
      then `artifacts.create_run_upload` declaring one `kernel` item with `sha256_b64(tar)` and
      the tar's `size_bytes` and **no** `chunks` (both classes are under the 5 GiB
      `SINGLE_PUT_MAX_BYTES`), then `put_presigned(item, tar)`;
   f. build the MCP client with `timeout=KDIVE_MEASUREMENT_TIMEOUT_S`, record both that value and
      the `SUPPORTED_BUDGET_S` constant, and time `runs.complete_build(run_id=...)` with
      `time.monotonic()` around the call;
   g. read the server record with `read_measurement_record`, asserting it is present and its
      `run_id` matches;
   h. assemble the row — arch, bundle bytes, member count, client elapsed, measurement timeout,
      supported budget, deployed revision, and every server-side field — append it to
      `KDIVE_MEASUREMENT_OUT` as one JSON object per line, and print it.

7. Stage into `KDIVE_MEASUREMENT_STAGE_DIR` rather than `tmp_path` when it is set, and remove the
   staged `modstage` tree once the tar is cut. `combined_kernel_tar` writes the full uncompressed
   `modules_install` tree beside the tar, which for the DWARF5 `allmodconfig` tree is tens of GB;
   `tmp_path` is rooted at the system temp directory, which is tmpfs (RAM-backed) on the
   measurement host, and pytest retains the last three runs' roots. Default to `tmp_path` so the
   103-MB arm is unchanged.

8. Assert only what the driver must guarantee: the record is present, `outcome` is `succeeded`,
   and `prepare_ms + reassemble_ms + queue_wait_ms + scan_ms + publish_ms <= total_ms`. Do
   **not** assert a duration threshold — the driver measures, it does not gate. Comparing against
   the supported budget is Task 4's job.

9. Add only the **three new** env vars — `KDIVE_MEASUREMENT_OUT`, `KDIVE_MEASUREMENT_STAGE_DIR`,
   and `KDIVE_MEASUREMENT_TIMEOUT_S` — to the table in `docs/guide/reference/config.md`,
   matching the surrounding rows' wording and column order. Do **not** re-add
   `KDIVE_KERNEL_SRC` (`config.md:19`), `KDIVE_PPC64LE_BUNDLE` (`config.md:312`), or
   `KDIVE_STACK_LOG_DIR` (`config.md:383`): all three are already documented, and this table is
   gated. Then run `just config-docs-check` and `just env-docs-check`; both must exit 0.

10. Add a section to `docs/operating/runbooks/live-testing.md` naming the driver, its env
    contract, the staging-space requirement, and the exact command, beside the existing tier
    sections.

11. Run `just format`, then
    `uv run python -m pytest tests/integration/live_stack/test_measurement_readout.py -q` →
    `5 passed`.

12. Stage the changed paths, record `git diff --cached --name-only`, run `prek run`, re-add
    exactly those paths (Markdown is in this commit), then `just lint` and `just type`.

13. Commit: `test: add the external-build finalization measurement driver`.

### Acceptance criteria

- `read_measurement_record` recovers a row from a line produced by `format_log_record_json` and
  ignores others.
- The ppc64le preflight skips when unset and raises when set-but-unusable, with its divergence
  from #1146 documented in the docstring.
- The driver sets `build_profile.arch` and `target_kind`, and is `live_stack`-marked so
  `just test` does not collect it.
- `docs/guide/reference/config.md` gains exactly the three new env vars and no duplicate rows;
  `just config-docs-check` and `just env-docs-check` exit 0.
- `just lint` and `just type` exit 0.

---

## Task 3 — Declare the host deps, run the measurement, write the proof record

**Where this fits.** This produces the evidence Task 4's decision rests on, and discharges the
provisioning-parity obligation the driver creates.

**Creates:** `docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md`
**Modifies:** `deploy/ansible/roles/libvirt_stack/defaults/main.yml`

### Interfaces

Consumed: everything Task 2 provides, plus the built kernel trees named in Global Constraints.
Provides to Task 4: the measured rows, as the proof record's table.

### Verification

- **Contract: the driver's host binaries are declared in the owning role.** `Mode: focused-test`.
  Green: `just lint-ansible` → exit 0; `just test-ansible` → exit 0. Red: the role's package
  lists omit `make`/`tar`.
- **Contract: the proof record's contents.** `Mode: task-test-not-applicable`. A human-readable
  record of measured values with no executable consumer; a test over it would assert prose.

### Steps

1. **Declare the host binaries unconditionally**, before running anything.
   `combined_kernel_tar` invokes `make` and a GNU `tar` supporting `--transform`
   (`spine.py:450-470`) — exactly the "subprocess-invoked binary" AGENTS.md names, whose owning
   layer it puts in `libvirt_stack` ("general build-host FS/virt tools"), **not** `live_vm_host`
   ("the `live_vm` debug toolchain and store-script deps"). Add `make` and `tar` to all three
   per-family lists in `deploy/ansible/roles/libvirt_stack/defaults/main.yml`
   (`libvirt_stack_packages_debian`, `_redhat`, `_suse`); the package name is the same on each.

   **State the provenance accurately: this is a pre-existing gap, not one this harness
   introduces.** The existing live-stack spine already calls `combined_kernel_tar` at
   `spine.py:505`, and neither binary appears in any of the three lists today, so the
   undeclared dependency predates this change. Charter criterion 6 conditions on "any **new**
   host prerequisite the harness introduces", which this is not. The edit still belongs here —
   this change depends on both binaries, so it repairs the gap rather than inheriting it — but
   the proof record and this plan say "pre-existing undeclared dependency, repaired here", never
   "prerequisite introduced by this change". The role choice is separately confirmed:
   `deploy/ansible/playbooks/runner.yml` applies `libvirt_stack` and does **not** apply
   `local_worker_host` (the one role that already declares `make`), so no applied role covers it.

   Do this whether or not the local run needs it. A conditional "only if the run failed" edit
   evaluates false on an already-warmed dev box and leaves the next clean reprovision without
   `make`, which is the silent breakage the rule exists to stop. Note also that `live_vm_host`
   could not carry it: that role asserts Ubuntu 26.04 and installs with `apt`
   (`roles/live_vm_host/tasks/main.yml:17-37`), so it cannot run on the Fedora measurement host,
   whereas `libvirt_stack` branches across Debian/RedHat/Suse and names Fedora explicitly.

2. Run `just lint-ansible` and `just test-ansible`. Both exit 0.

3. Wait for both kernel builds to finish. Confirm each tree has its boot member:
   `ls -la /home/dave/kdive-proof-2318/build-fedora/arch/x86/boot/bzImage` and the same under
   `build-allmod`. Expect a regular file in each.

4. Bring the stack up: `just stack-up`, then `scripts/live-stack/up.sh --skip-libvirt`. The
   measurement uses an unbound Run and provisions no VM, so libvirt bring-up buys nothing and
   adds a failure surface; `--skip-libvirt` is an accepted argument (`up.sh:33`). Expect
   `Backends healthy and schema migrated.` and a `/readyz` that answers.

5. Run the 103-MB class:

   ```sh
   KDIVE_KERNEL_SRC=/home/dave/kdive-proof-2318/build-fedora \
   KDIVE_MEASUREMENT_STAGE_DIR=/home/dave/kdive-proof-2318/stage \
   KDIVE_MEASUREMENT_OUT=/home/dave/kdive-proof-2318/rows.jsonl \
   uv run python -m pytest tests/integration/test_finalization_measurement.py -m live_stack \
     -k x86_64 -q
   ```

   Expect `1 passed, 1 deselected` — `-k` **deselects**, it does not skip, so the ppc64le arm
   does not appear as a skip in this run — and one JSON row appended.

6. Record the bundle's actual compressed size. If it is materially off the 103-MB class, adjust
   the `fedora` variant in `/home/dave/kdive-proof-2318/build.sh`, rebuild, and re-run rather
   than reporting a different size under the class name.

7. Repeat step 5 with `KDIVE_KERNEL_SRC=/home/dave/kdive-proof-2318/build-allmod` for the 2-GB
   class. The staging directory must have room for the uncompressed `modules_install` tree —
   tens of GB for this config — so keep it off tmpfs.

8. Derive the object-store per-request latency for each row: `store_bytes` and `store_requests`
   are recorded, and the scan's wall-clock is `scan_ms`; state the mean per-request cost and the
   deployment shape (a SeaweedFS container on the same host, reached over loopback — see
   `KDIVE_BACKEND_SERVICES` in `scripts/live-stack/lib.sh`). This is what lets a reader judge
   whether the row transfers to a network-attached store.

9. Write the proof record. It carries: the exact command for each run; the deployed revision from
   `/readyz`; the host's CPU count, kernel, and object-store deployment shape; the staging
   filesystem's type and free space, and the observed staged size for the 2-GB row; the kernel
   version and exact config derivation for both trees, so another host can regenerate them; and
   per row — arch, bundle compressed bytes, member count, supported budget (30 s), measurement
   timeout, client elapsed, `prepare_ms`, `reassemble_ms`, `queue_wait_ms`, `scan_ms`,
   `publish_ms`, `total_ms`, `store_requests`, `store_bytes`, mean per-request latency,
   `chunked`, and `outcome`. State plainly that both bundles were single-PUT (under the 5 GiB
   `SINGLE_PUT_MAX_BYTES`), so no chunk reassembly is in the measured path, and that the
   kernel-build toolchain that produced the trees is outside the repository and therefore outside
   criterion 6's Ansible declaration — disclosed rather than silent.

10. State the ppc64le arm as not run, with the reason and the owner. Do not attribute elapsed
    time to decompression, and do not state a size threshold. Report which phase dominates each
    row and nothing beyond what the rows show.

11. Stage, run `prek run`, re-add exactly the staged paths, commit:
    `docs: record external-build finalization measurements`.

### Acceptance criteria

- `make` and `tar` are declared in all three `libvirt_stack` package lists, unconditionally.
- Two measured rows exist, one per bundle class, each carrying every field #2318 names plus the
  supported budget and the measurement timeout.
- The proof record names the deployed revision, host facts, staging facts, object-store shape and
  per-request latency, and both bundles' actual sizes and config derivations.
- The ppc64le arm is recorded as not run, with its reason and owner.
- `just lint-ansible` and `just test-ansible` exit 0.

---

## Task 4 — ADR 0655: the completion contract

**Where this fits.** The decision #2318 exists to make, and #2319's gate.

**Creates:** `docs/adr/0656-external-build-completion-contract.md`

### Interfaces

Consumed: the proof record's rows from Task 3, and the 30 s supported budget from Global
Constraints. Provides: the accepted decision #2319 reads.

### Verification

- **Contract: the record's shape and status pass the repo's record gates.** `Mode: focused-test`.
  Green: `just records` → exit 0; `just adr-status-check` → exit 0. Red: a malformed record, a
  reused number, or a `Proposed` status cited from `src/`/`tests/`.

### Steps

1. Read the two rows. Identify which phase dominates each.

2. Apply the charter's own rule, verbatim: **synchronous completion is retained only if the
   larger bundle's `total_ms` meets the 30 000 ms supported budget.** Otherwise durable
   asynchronous finalization is required. The threshold is the MCP client default this
   repository's own harness runs at, not a number the measurement chose; state that provenance
   in `## Context`.

   Do **not** impose a margin factor as the rule. An earlier draft required clearing 50% of the
   budget, which has no charter provenance and would decide the in-between case — a `total_ms`
   between 15 s and 30 s — against the charter's plain reading, activating #2319 on evidence the
   charter resolves the other way. If the measured total lands close enough to 30 s that
   headroom matters, say so as a labelled engineering judgement in `## Considered & rejected`,
   citing the design's own accepted failure class that each row is a single observation rather
   than a distribution. A judgement recorded as a judgement is fine; one disguised as a
   threshold is not.

   **This task's ADR body gets its review at step 6**, the branch review over the whole diff —
   the design review saw only the `Proposed` shell, because an evidence-selected decision cannot
   be written before the evidence exists. Do not treat the earlier design review as covering the
   five contract definitions written here.

3. Write `docs/adr/0656-external-build-completion-contract.md` with exactly the five sections
   `## Status`, `## Context`, `## Decision`, `## Consequences`, `## Considered & rejected`.
   Set `- **Status:** Accepted` — the decision ships with this PR.

4. `## Context` cites the proof record by repo-relative path, states the measured attribution in
   two or three sentences, and names the 30 s budget with its source. No re-derivation of
   #2314's analysis.

5. `## Decision` states the outcome of the rule in step 2, then defines all five contract
   elements: retry and idempotency, cancellation, upload-window fencing (preserving
   `complete_build.py:553-586`), publication ownership, and recovery after transport
   interruption.

6. `## Consequences` states what #2319 must do — implement or close with this evidence — and
   names **both** reopening conditions: the outstanding ppc64le confirmation, and a
   network-attached object store whose per-request latency makes scan dominate. The measured rows
   come from a loopback store, so the second is not hypothetical; bound the decision to the
   measured deployment shape explicitly.

7. `## Considered & rejected` carries one bullet per alternative, each tagged `verified:` with
   the command, its result, and the environment it ran in, or `judgment:`. The alternative not
   taken in step 2 is one bullet; "do nothing — keep the contract undocumented" is another.

8. Size the record to the decision. Do not argue for it at greater length than it governs.

9. Run `just records` and `just adr-status-check`. Both exit 0.

10. Stage, run `prek run`, re-add exactly the staged paths, commit:
    `docs: decide the external-build completion contract`.

### Acceptance criteria

- ADR 0655 exists, is `Accepted`, and carries the five sections.
- Its decision follows the stated numeric rule against the 30 s budget and is traceable to the
  proof record's rows.
- Both reopening conditions are named.
- Every `Considered & rejected` bullet carries a `verified:` or `judgment:` tag.
- `just records` and `just adr-status-check` exit 0.

---

## Deferrals

One, owned by a record in this change:

- **The ppc64le measurement arm has not been run** — charter exclusion 6. Owner:
  `docs/debt/0015-ppc64le-finalization-measurement-unrun.md` (Open, review-by 2026-12-14). The
  harness is parameterized and merged; resolving it is one `pytest` invocation once a bundle
  exists. ADR 0655 names it as a reopening condition, so the deferral must outlive issue #2318,
  which closes with this pull request — that is why it is a record rather than a line in the
  charter.

Every finding from both design-review passes was accepted and fixed in the artifacts; none of
those became a deferral.
