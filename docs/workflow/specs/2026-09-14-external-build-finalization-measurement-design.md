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
server timings, no deployed revision, no effective client request budget, and no attribution
between semaphore wait, object-store I/O, archive scanning, and publication.

The decision this blocks — whether optimized synchronous completion meets the supported request
budget, or durable asynchronous finalization is required — cannot be made from the reports that
exist. #2319's implementation is gated on it.

The finalization path has three phases with distinct costs, and nothing today separates them:

| Phase | Code | Cost driver |
|---|---|---|
| prepare | `CompleteBuildFinalizer._prepare` | two DB reads, upload-window check |
| validate | `_validate_uploads` | wait on `_EXTERNAL_BUILD_VALIDATION_SLOTS`, then object-store range reads + gzip/tar scan + sha256 + ELF parse on a worker thread |
| publish | `_finalize_external_build` | the atomic DB commit |

## Scope

One implementable unit: make the three phases observable, then measure them over the real MCP
path and record the decision.

### What this builds

1. **Finalization measurement instrumentation** (`src/kdive/services/runs/complete_build.py`).
   One structured log record per external-build finalization — success *and* categorized
   failure — carrying `run_id`, the three phase durations, the semaphore wait separated from
   the scan it gates, the object-store request count and byte total the validation performed,
   and the outcome. Phase timing wraps the existing calls; the request/byte counts come from a
   counting decorator over the `ValidatorStore` protocol, composed inside `_VersionPinnedStore`'s
   existing wrapping point so every validation `head`/`get_range` is counted exactly once.

2. **A `live_stack` measurement driver** (`tests/integration/test_finalization_measurement.py`).
   It drives the real MCP HTTP transport: `investigations.open` → `runs.create` (**unbound** — no
   allocation, no System, no VM, so the measurement isolates finalization) →
   `artifacts.create_run_upload` → presigned PUT → `runs.complete_build`. It records the client
   request budget it set, times the `complete_build` call client-side, reads the deployed
   revision from the aux listener's `/readyz` (ADR-0482), and correlates the server's measurement
   record for that `run_id` out of `${KDIVE_STACK_LOG_DIR}/server.log`. It emits one JSON
   measurement row per bundle.

3. **The proof record** — the measured rows for the 103-MB and 2-GB x86_64 bundle classes, with
   their environment provenance and the synthesis-free statement of how each bundle was built.

4. **ADR 0655** — the completion contract, decided *from* those rows. Its decision is
   evidence-selected, so it is written in the build phase after the measurements exist, not here.
   It defines retry/idempotency, cancellation, upload-window fencing, publication ownership, and
   transport-interruption recovery, and it names the outstanding ppc64le confirmation.

### Measurement readout: the server log, not the tool response

The charter forbids public MCP contract implementation, so the phase attribution must not become
a `data` field on `runs.complete_build`. Three readouts were available; the log record is the one
this design takes.

- **Structured log record** (taken). `configure_logging` already installs a JSON formatter with
  the ADR-0014 schema and the mandatory redaction filter, and `scripts/live-stack/lib.sh:253`
  already captures the server's stdout to `${KDIVE_STACK_LOG_DIR}/server.log`. Per-finalization
  granularity, correlatable by `run_id`, no contract change, and useful to an operator
  diagnosing a slow finalization in production — not only to this proof.
- **Response `data` field.** Excluded by the charter; also makes an internal cost breakdown a
  published contract that later optimization would have to keep.
- **OTel metrics via the stack's Prometheus.** Available, but metrics aggregate. The decision
  needs per-bundle attribution, and a histogram cannot say which request the 2-GB bundle was.

### Bundle provenance

Both bundles are cut by the existing `combined_kernel_tar(kernel_src, dest, arch=)`
(`tests/integration/live_stack/spine.py`) from **natively built** x86_64 kernel trees — linux
7.2.6, the host distro config for the 103-MB class and `allmodconfig` + DWARF5 for the 2-GB
class. Nothing is synthesized, so no modeled compression ratio or member-size distribution enters
the record. `SINGLE_PUT_MAX_BYTES` is 5 GiB, so both classes are single-PUT: the measured path
carries no chunk reassembly, which the record states rather than leaves to inference.

### Architecture coverage

The driver is arch-parameterized on the one axis that matters to it — the `arch` passed to
`combined_kernel_tar`, which selects `arch/x86/boot/bzImage` or the ppc64le ELF `vmlinux`. The
x86_64 arm runs from `KDIVE_KERNEL_SRC`; the ppc64le arm runs the same code from
`KDIVE_PPC64LE_BUNDLE` and skips cleanly without it, matching #1146. No ppc64le bundle or
cross-toolchain exists on the measurement host, so this change records x86_64 rows only.

### Not in scope

Durable asynchronous finalization itself (#2319), range-buffering changes (#2317), any weakening
of validation or fencing, universal size thresholds, decompression-only attribution, and
install/boot correctness.

## Failure model

**Actors and deployments.** A local operator running the live stack from a checkout
(`scripts/live-stack/up.sh`, host processes, compose backends); the self-hosted CI runner, where
the `live_stack` suite is a manually dispatched job. The instrumentation itself additionally runs
in every deployment that runs a `server` process — Compose, systemd, and Kubernetes — because it
is a log line on the ordinary finalization path.

**Invariants and assets at stake.**

- Redaction: the measurement record must not defeat the mandatory redactor. It carries only a
  `run_id`, integers, and a fixed outcome vocabulary — no key, no path, no version id, no
  operator-supplied string.
- The finalization result: instrumentation must not change what `complete_build` returns, what it
  commits, or the upload-window and object-identity fencing at `complete_build.py:553-586`.
- Measurement honesty: a phase duration that silently includes another phase makes the ADR wrong.
  The semaphore wait is recorded separately from the scan it gates, which is the single
  attribution #2314 could not make.
- The client request budget is a property of the *client*, so the driver records the timeout it
  set rather than inferring one.

**Accepted failure classes.**

- *The measured numbers are host-specific.* Accepted and stated in the record: one host, named
  CPU count and object-store backend. The decision turns on the shape of the attribution — which
  phase dominates — not on a threshold portable to other hardware. #2314 already excludes
  universal size thresholds.
- *The 2-GB row is a single observation per configuration, not a distribution.* Accepted: the
  cost of repetition at that size is hours, and the decision does not rest on variance.
- *The ppc64le arm is unmeasured here.* Accepted by the charter's exclusion 6, with the owner
  named. The ADR states what a ppc64le result would have to show to reopen the decision.
- *The log-file readout depends on the live-stack process layout.* Accepted: the driver is
  `live_stack`-marked and skips cleanly when the stack is absent, exactly as the tier's other
  drivers do.

**Covered elsewhere.**

- Durable finalization behaviour, its job kind, schema, and cancellation semantics — #2319.
- Range-read amplification and its regression guard — closed #2317.
- Archive/decompression bounds, checksums, canonical paths, module obligations — parent #2314,
  preserved unchanged by this change.

## Security

The change is not security-relevant under `$quest` step 6's trigger: it adds no entry point,
touches no authn/authz or tenancy logic, handles no secret, parses no new untrusted input, builds
no command/query/path/URL from a non-literal, widens no permission grant, and changes no
dependency or security-relevant default. The one boundary it touches is the log sink, which the
failure model's first invariant covers: the record's fields are a `run_id`, integers, and a fixed
outcome vocabulary, passing through the redaction filter `configure_logging` already installs.

## Success

1. `runs.complete_build` emits exactly one measurement record per finalization attempt, on the
   success path and on each categorized failure path, carrying `run_id`, prepare/queue-wait/scan/
   publish durations, object-store request count and byte total, and outcome.
2. The object-store counts equal the requests the validation actually issued — verified against a
   fixture whose expected count is derived from the bundle, not asserted from a prior run.
3. The measurement record changes neither the `ToolResponse` nor the committed state: the
   existing `complete_build` and concurrency suites pass unchanged.
4. The driver records, for each bundle it runs, every field #2318 names: queue wait, object-store
   request count and bytes, scan time, publication time, deployed revision, client request
   budget, and final outcome.
5. The proof record carries measured rows for the 103-MB and 2-GB x86_64 classes and names, for
   each, the bundle's **actual** compressed byte size and member count alongside its kernel tree,
   config, host CPU count, and object-store backend. "103-MB class" and "2-GB class" name the
   #2314 observations the rows correspond to; the recorded size is whatever the build produced,
   never rounded to the class name.
6. The ppc64le arm skips cleanly with `KDIVE_PPC64LE_BUNDLE` unset and fails loudly when it is set
   but unusable, matching #1146's contract.
7. ADR 0655 is Accepted, decides synchronous versus durable asynchronous completion from the
   recorded rows, defines retry/idempotency, cancellation, upload-window fencing, publication
   ownership, and transport-interruption recovery, and names the outstanding ppc64le
   confirmation and the result that reopens the decision.
8. Any host prerequisite the driver introduces is declared in the Ansible role that owns its
   layer, in this change.

## Validation

| # | Contract | Mode | Evidence |
|---|---|---|---|
| 1 | Measurement record emitted on the success path with every field | `focused-test` | `tests/services/runs/test_complete_build_measurement.py::test_success_emits_measurement_record` — red: no record in `caplog`; green: `uv run python -m pytest tests/services/runs/test_complete_build_measurement.py -q` |
| 2 | Measurement record emitted on the validation-failure path | `focused-test` | same file, `::test_validation_failure_emits_measurement_record` — red: record absent when `CompleteBuildValidationError` raises; green: same command |
| 3 | Object-store request count and bytes equal what validation issued | `focused-test` | same file, `::test_store_counts_match_issued_requests` — counts compared against a recording fake's own tally, not a literal; red: counter reports 0 |
| 4 | Semaphore wait recorded separately from the scan it gates | `focused-test` | same file, `::test_queue_wait_excludes_scan` — a held semaphore makes wait large and scan small; red: the two are indistinguishable |
| 5 | Record survives redaction with no operator-supplied string | `focused-test` | same file, `::test_record_fields_are_closed_vocabulary` — asserts the record's keys and value types; red: a key carrying a store key or path |
| 6 | `ToolResponse` and committed state unchanged | `focused-test` | existing `tests/services/runs/test_complete_build.py` and `tests/adversarial/test_complete_build_concurrency.py` pass unchanged; green: `uv run python -m pytest tests/services/runs/test_complete_build.py tests/adversarial/test_complete_build_concurrency.py -q` |
| 7 | Driver parses a measurement record out of a server log | `focused-test` | `tests/integration/live_stack/test_measurement_readout.py::test_reads_measurement_row` — unmarked unit test over a fixture log line, so the parser is proven without the stack; red: parser returns `None` |
| 8 | ppc64le arm skips cleanly unset, fails loudly when set-but-unusable | `focused-test` | same file, `::test_ppc64le_arm_preflight` — mirrors #1146's `_ppc64le_bundle_preflight` contract |
| 9 | The `live_stack` driver itself | `task-test-not-applicable` | Its observable contract *is* the live measurement; it runs against a stack this repo's ordinary gate deliberately excludes (`just test` drops `live_stack`). Its parser and preflight are covered by 7 and 8; the driver body is proven by the recorded run in the proof record, which is the deliverable. |
| 10 | Proof record contents | `task-test-not-applicable` | A human-readable record of measured values; no executable consumer validates it, and a test asserting its prose would assert wording rather than the contract. |
| 11 | ADR 0655 shape and status | `focused-test` | `just records` and `just adr-status-check` — red: a malformed record or a `Proposed` status cited from `src/`/`tests/`; green: both recipes exit 0 |
| 12 | Ansible declaration of any new host prerequisite | `focused-test` | `just lint-ansible` and `just test-ansible` — red: an undeclared package in the role that owns the layer; green: both exit 0 |

## References

- ADR-0234 / ADR-0343 — the combined external-build artifact and its per-arch boot member
- ADR-0448 §2 — upload-window identity and remint fencing the measurement must not disturb
- ADR-0482 §1 — `/readyz` deployed-version fact the driver reads
- ADR-0014 — the structured log schema the measurement record joins
- ADR-0042 — the live-stack tier this driver runs in
