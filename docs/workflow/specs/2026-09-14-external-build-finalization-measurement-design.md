# External-build finalization measurement and completion contract — design (#2318)

- Issue: [#2318](https://github.com/randomparity/kdive/issues/2318) (parent
  [#2314](https://github.com/randomparity/kdive/issues/2314); siblings: closed
  [#2317](https://github.com/randomparity/kdive/issues/2317), conditional
  [#2319](https://github.com/randomparity/kdive/issues/2319))
- Scope charter: issue #2318 comment `q2318-4672f7d3` (re-frozen 2026-09-14)
- Lane: full-spec · complexity L · design denominator 1000 changed lines

## Problem

`runs.complete_build` validates and publishes an external build inside one synchronous MCP
request. #2314 established the I/O amplification analytically — 117 range calls for a 1 MiB
bundle, 102 of them exactly 10,240 bytes — and #2317 has since landed a 4 MiB read-ahead buffer
(`_RANGE_CHUNK_BYTES`, `build_artifacts/validation.py:57`). What no report establishes is where
the wall-clock actually goes: the observed request timeouts for 103-MB and 2-GB bundles carry no
server timings, no deployed revision, and no attribution between semaphore wait, object-store
I/O, archive scanning, and publication.

The decision this blocks — whether optimized synchronous completion meets the supported request
budget, or durable asynchronous finalization is required — cannot be made from the reports that
exist. #2319's implementation is gated on it.

The finalization path has four phases with distinct costs, and nothing today separates them:

| Phase | Code | Cost driver |
|---|---|---|
| prepare | `CompleteBuildFinalizer._prepare` | two DB reads, upload-window check |
| reassemble | `_reassemble_chunked_artifacts` | multipart copy; **chunked uploads only** |
| validate | `_validate_uploads` | wait on `_EXTERNAL_BUILD_VALIDATION_SLOTS`, then object-store range reads + gzip/tar scan + sha256 + ELF parse on a worker thread |
| publish | `_finalize_external_build` | the atomic DB commit |

### The supported client request budget

`LiveStackClient.over_http` (`src/kdive/mcp/dev_harness.py:203-208`) builds
`StreamableHttpTransport(url=..., headers=...)` and `Client(transport)` with no timeout override.
fastmcp 3.4.4 then preserves MCP's 30-second default for regular operations. **30 s is therefore
the supported client request budget** this design tests against — a repository-observable value,
not one the harness picks. It is also the most likely proximate cause of #2314's reported
timeouts.

A measurement bounded by that budget would truncate itself, so the driver raises *its own*
timeout to complete the work and records the 30 s budget separately as the threshold. The two
numbers are distinct and both appear in every row.

## Scope

One implementable unit: make the phases observable, then measure them over the real MCP path and
record the decision.

### What this builds

1. **Finalization measurement instrumentation** (`src/kdive/services/runs/complete_build.py`).
   One structured log record per finalization attempt — success *and* every failure — carrying
   `run_id`, the four phase durations, the semaphore wait separated from the scan it gates, the
   object-store request count and byte total, and the outcome. Phase timing wraps the existing
   calls; the counts come from a counting decorator over the `ValidatorStore` protocol composed
   inside `_VersionPinnedStore`'s existing wrapping point, so every validation `head`/`get_range`
   is counted exactly once.

2. **A `live_stack` measurement driver** (`tests/integration/test_finalization_measurement.py`).
   It drives the real MCP HTTP transport: `investigations.open` → `runs.create` (**unbound** — no
   allocation, no System, no VM, so the measurement isolates finalization) →
   `artifacts.create_run_upload` → presigned PUT → `runs.complete_build`. It reads the deployed
   revision from the aux listener's `/readyz` (ADR-0482) and correlates the server's record for
   that `run_id` out of `${KDIVE_STACK_LOG_DIR}/server.log`.

3. **The proof record** — the measured rows for the 103-MB and 2-GB x86_64 bundle classes, with
   environment provenance sufficient to re-run them elsewhere.

4. **ADR 0655** — the completion contract, decided *from* those rows against the 30 s budget. Its
   decision is evidence-selected, so it is written in the build phase after the measurements
   exist, not here.

### Measurement readout: the message, not a structured field

The charter forbids public MCP contract implementation, so the attribution must not become a
`data` field on `runs.complete_build`. The readout must also survive the serializer the measured
process actually runs, which is **not** `configure_logging`'s formatter: `__main__.py:445` calls
`init_telemetry` for every command, and `facade.py:207` then calls `remove_stdlib_floor()`, which
detaches the `JsonFormatter` handler. Every line in `server.log` after startup comes from
`kdive.observability.stdout_exporter.format_log_record_json`.

Both serializers build a closed payload and merge only the `bind_context` fields —
`JsonFormatter` via `_kdive_ctx` (`log.py:85`), the OTel exporter via `_domain_context`, which
keeps only `CONTEXT_FIELDS` — and `bind_context` rejects any name outside that audited five. A
`logging` `extra=` attribute therefore reaches neither. On the OTel path a nested dict is not a
valid attribute value either.

So the payload rides in the **message**, which both serializers carry verbatim:

```text
msg = "external_build_finalization_measured {\"run_id\": \"...\", \"scan_ms\": 1234.5, ...}"
```

That is the wire contract both sides read. It needs no change to the ADR-0014 or ADR-0090 log
schema, is identical on the stdlib-floor and OTel paths, and passes redaction intact:
`SecretRedactionFilter` rewrites `msg` as text, and no measurement key matches its
`password|passwd|token|api[_-]?key|secret` pattern.

### Bundle provenance

Both bundles are cut by the existing `combined_kernel_tar(kernel_src, dest, arch=)`
(`tests/integration/live_stack/spine.py:428`) from **natively built** x86_64 kernel trees —
linux 7.2.6, the host distro config for the 103-MB class and `allmodconfig` + DWARF5 for the
2-GB class. Nothing is synthesized, so no modeled compression ratio or member-size distribution
enters the record. The proof record carries each tree's exact config derivation so another host
can regenerate it. `SINGLE_PUT_MAX_BYTES` is 5 GiB, so both classes are single-PUT: the measured
path carries no chunk reassembly, which the record states rather than leaves to inference.

### Architecture coverage

Arch enters on **two** axes, and they must agree:

- client-side, the `arch` passed to `combined_kernel_tar`, selecting `arch/x86/boot/bzImage` or
  the ppc64le ELF `vmlinux`;
- server-side, `run.build_profile["arch"]`, which `_build_arch` (`complete_build.py:454-468`)
  reads and hands to the ADR-0343 per-arch boot-member check. It defaults to `x86_64` when
  absent.

A mismatch is a validation rejection, not a measurable outcome, so the driver sets
`build_profile={"schema_version": 1, "arch": arch}` from the same parameter that cuts the bundle.
The x86_64 arm would otherwise pass only by accident, on that absent-field default.

The x86_64 arm runs from `KDIVE_KERNEL_SRC`; the ppc64le arm runs the same code from
`KDIVE_PPC64LE_BUNDLE`. No ppc64le bundle or cross-toolchain exists on the measurement host, so
this change records x86_64 rows only.

### Not in scope

Durable asynchronous finalization itself (#2319), range-buffering changes (#2317), any weakening
of validation or fencing, universal size thresholds, decompression-only attribution, and
install/boot correctness.

## Failure model

**Actors and deployments.** A local operator running the live stack from a checkout
(`scripts/live-stack/up.sh`, host processes, compose backends); the self-hosted CI runner, where
the `live_stack` suite is a manually dispatched job. The instrumentation additionally runs in
every deployment that runs a `server` process — Compose, systemd, and Kubernetes — because it is
a log line on the ordinary finalization path.

**Invariants and assets at stake.**

- Redaction: the record carries only a `run_id`, integers, floats, and a fixed outcome
  vocabulary — no key, no path, no version id, no operator-supplied string.
- The finalization result: instrumentation must not change what `complete_build` returns, what it
  commits, or the upload-window and object-identity fencing at `complete_build.py:553-586`.
- Measurement honesty: a phase duration that silently includes another phase makes the ADR wrong.
  The semaphore wait is recorded separately from the scan it gates, which is the single
  attribution #2314 could not make. An attempt that failed is never recorded as a success.
- Readout recoverability: the record must be recoverable from `server.log` under the handler
  configuration `python -m kdive server` actually installs — the OTel `StdoutJsonLogExporter`
  after `remove_stdlib_floor`, not `configure_logging`'s `JsonFormatter`. Validation row 8
  exercises that serializer specifically.

**Accepted failure classes.**

- *The measured numbers are specific to one host and one object-store deployment shape.* The live
  stack's object store is a container on the same host (`KDIVE_BACKEND_SERVICES` in
  `scripts/live-stack/lib.sh`), so every validation request crosses loopback and the per-request
  latency term is near zero. Against a network-attached S3 endpoint that term becomes
  `store_requests × RTT`, and phase dominance can invert. Accepted **not** because dominance is
  portable — it is not — but because the decision is explicitly scoped to the measured deployment
  shape: the row records that shape and the observed mean per-request latency, and ADR 0655 names
  a network-attached store whose latency makes scan dominate as a result that reopens it.
- *Each row is a single observation, not a distribution.* Accepted: repetition at the 2-GB size
  costs hours, and the decision rests on whether the total clears a 30 s budget by an order of
  magnitude, not on variance.
- *The ppc64le arm is unmeasured here.* Accepted by the charter's exclusion 6, with the owner
  named. ADR 0655 states what a ppc64le result would have to show to reopen the decision.
- *The driver requires a running live stack and skips cleanly without one.* Accepted: it is
  `live_stack`-marked, exactly as the tier's other drivers are. This accepts an **absent** stack
  only; recoverability against a present stack is an invariant above, not an accepted class.

**Covered elsewhere.**

- Durable finalization behaviour, its job kind, schema, and cancellation semantics — #2319.
- Range-read amplification and its regression guard — closed #2317.
- Archive/decompression bounds, checksums, canonical paths, module obligations — parent #2314,
  preserved unchanged by this change.

## Security

Not security-relevant under `$quest` step 6's trigger: it adds no entry point, touches no
authn/authz or tenancy logic, handles no secret, parses no new untrusted input, builds no
command/query/path/URL from a non-literal, widens no permission grant, and changes no dependency
or security-relevant default. The one boundary it touches is the log sink, covered by the failure
model's redaction invariant.

## Success

1. `runs.complete_build` emits exactly one measurement record per finalization attempt, carrying
   `run_id`, the four phase durations, object-store request count and byte total, and outcome.
2. `outcome` is the attempt's true result on every exit path — the three service exception types
   (`CompleteBuildConfigurationError`, `CompleteBuildExpiredWindowError`,
   `CompleteBuildValidationError`), `already_recorded`, and `unexpected` for anything else. No
   failed or cancelled attempt is ever recorded as `succeeded`.
3. The object-store counts equal the requests validation actually issued, verified against a
   recording fake's own tally rather than a literal.
4. The record changes neither the `ToolResponse` nor the committed state: the existing
   `complete_build` and concurrency suites pass unchanged.
5. The record is recoverable from a line produced by `format_log_record_json`, the serializer the
   `server` process runs.
6. The driver records, for each bundle: queue wait, object-store request count and bytes, scan
   time, publication time, deployed revision, **the 30 s supported client request budget and the
   larger timeout the driver set**, and final outcome.
7. The proof record carries measured rows for the 103-MB and 2-GB x86_64 classes and names, for
   each, the bundle's actual compressed byte size and member count, its kernel tree and config
   derivation, the host CPU count, the object-store deployment shape and observed mean
   per-request latency, and the staging filesystem's type and free space. "103-MB class" and
   "2-GB class" name the #2314 observations the rows correspond to; the recorded size is whatever
   the build produced, never rounded to the class name.
8. The ppc64le arm skips when `KDIVE_PPC64LE_BUNDLE` is unset and raises when it is set but
   lacks `kernel.tar.gz`. This deliberately diverges from `_ppc64le_bundle_preflight`
   (`tests/integration/test_live_stack.py:1023-1035`), which skips in both branches and also
   requires `initrd.img`: a measurement harness must not silently produce no row, and the
   measurement arm boots nothing, so it needs no initrd.
9. ADR 0655 is Accepted and decides synchronous versus durable asynchronous completion by a
   stated numeric rule against the 30 s budget: synchronous only if the 2-GB row's `total_ms`
   clears it with margin. It defines retry/idempotency, cancellation, upload-window fencing,
   publication ownership, and transport-interruption recovery, and names both reopening
   conditions — the outstanding ppc64le confirmation and a network-attached object store.
10. `make` and GNU `tar` — the binaries `combined_kernel_tar` invokes — are declared in the
    Ansible role that owns the build-host layer, unconditionally, not only if the local run
    happened to need them.

## Validation

| # | Contract | Mode | Evidence |
|---|---|---|---|
| 1 | Record emitted on the success path with every field | `focused-test` | `tests/services/runs/test_complete_build_measurement.py::test_success_emits_measurement_record` — red: no matching line in `caplog` |
| 2 | Record emitted for a validation failure, carrying its category | `focused-test` | same file, `::test_validation_failure_records_its_category` — red: `outcome` is `succeeded` |
| 3 | Record emitted for a configuration failure (missing manifest, reaped or replaced window) | `focused-test` | same file, `::test_configuration_failure_records_its_reason` — red: `outcome` is `succeeded`; this arm is distinct from row 2's |
| 4 | Nothing uncategorized is recorded as success | `focused-test` | same file, `::test_unexpected_exception_records_unexpected` — red: `outcome` is `succeeded` when the publish step raises `RuntimeError` |
| 5 | Object-store counts equal what validation issued | `focused-test` | same file, `::test_store_counts_match_issued_requests`, built on the **non-injected** path (no `validate_complete_build`) over `_FakeStore` + `_combined_kernel_tar` reused from `tests/providers/local_libvirt/test_validate_external_artifacts.py`; red: counter reports 0 |
| 6 | Semaphore wait recorded separately from the scan it gates | `focused-test` | same file, `::test_queue_wait_excludes_scan` — red: the held interval appears in `scan_ms` |
| 7 | Record fields are a closed vocabulary | `focused-test` | same file, `::test_record_fields_are_closed_vocabulary` — red: a key carrying a store key or path |
| 8 | Record is recoverable from the serializer the server runs | `focused-test` | `tests/integration/live_stack/test_measurement_readout.py::test_reads_row_from_otel_exporter` — fixture produced by running a record through `format_log_record_json`, not hand-written; red: parser returns `None` |
| 9 | Parser ignores records for other runs | `focused-test` | same file, `::test_ignores_other_run_ids` — red: a row is returned for a non-matching `run_id` |
| 10 | ppc64le preflight skips unset, raises when set-but-unusable | `focused-test` | same file, `::test_ppc64le_arm_preflight` — red: a missing path skips instead of raising |
| 11 | `ToolResponse` and committed state unchanged | `focused-test` | existing `tests/services/runs/test_complete_build.py` and `tests/adversarial/test_complete_build_concurrency.py` pass unchanged |
| 12 | `make` and GNU `tar` declared in the owning role | `focused-test` | `just lint-ansible` and `just test-ansible` — red: the role omits them; green: both exit 0 |
| 13 | ADR 0655 shape and status | `focused-test` | `just records` and `just adr-status-check` — red: a malformed record, a reused number, or a `Proposed` status cited from `src/`/`tests/` |
| 14 | The `live_stack` driver itself | `task-test-not-applicable` | Its observable contract *is* the live measurement, against a stack `just test` excludes by marker. Its two separable pieces — the parser and the preflight — are rows 8–10; the driver body's evidence is the recorded run in the proof record, which is the deliverable. |
| 15 | Proof record contents | `task-test-not-applicable` | A human-readable record of measured values with no executable consumer; a test over it would assert prose wording. |

## References

- ADR-0234 / ADR-0343 — the combined external-build artifact and its per-arch boot member
- ADR-0448 §2 — upload-window identity and remint fencing the measurement must not disturb
- ADR-0482 §1 — `/readyz` deployed-version fact the driver reads
- ADR-0014 / ADR-0090 — the structured log schema and the OTel facade the record joins
- ADR-0042 — the live-stack tier this driver runs in
