# Diagnostics names why it withheld, and `operator_recovery` is documented

Issue [#2489](https://github.com/randomparity/kdive/issues/2489) — epic #2484 R5.

## Problem

`operator_recovery` is a `RetryAction` the lifecycle client returns
(`src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py`). It resolves to nothing
outside source, tests, ADR-0657, and two merged workflow records: no file under `docs/` or
`deploy/` says what it means or what to do about it.

`diagnostics` is the designated escape hatch, and each of its four withhold paths in
`src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py` emits a slot-name-only marker or an
empty string, with a `SlotResult.message` carrying the fixed word `withheld` or the slot phase. An
operator cannot tell an unreadable state from an acquisition failure from a redaction refusal.

## Scope

**A closed reason vocabulary.** A `WithholdReason` `StrEnum` in the diagnostics module, one member
per distinct cause. Every value is a literal in that enum; none is derived from an exception, a
captured value, or any part of the withheld report:

| Reason | Cause | Site |
|---|---|---|
| `state_unreadable` | the slot's retained state could not be loaded | `:328-338` |
| `acquisition_failed` | systemd or journal acquisition failed | `_diagnose_slot` → `:381-388` |
| `redaction_refused` | a forbidden value survived this slot's own redaction | `_diagnose_trusted_slot` → `:381-388` |
| `peer_redaction_refused` | the report holds a forbidden value learned from another slot | `:401-403` |
| `internal_error` | an unexpected failure inside the capture loop | `:389-398` |

The two `_UnsafeDiagnosticText` causes share one site today, and they are two of the three causes
the issue says an operator cannot tell apart, so the private exception carries the reason its
raiser knows and the site reads it back.

**Where the reason rides.** On the existing free-form `SlotResult.message`
(`StringConstraints(max_length=1024)`) as `withheld: <reason>`, plus `; phase=<phase>` where a
state was loaded. No field is added to `LifecycleRequest`, `LifecycleResponse`, or `SlotResult`:
those schemas are hashed by `lifecycle_protocol_identity()`, and a moved identity fails
`scripts/live-stack/worker-lifecycle.sh` closed until every provisioned host is reprovisioned.
That constraint is ADR-0574's and #2532's, not a decision taken here, and the message is
operator-facing output with no parser — so no ADR is written for it.

**In the emitted text.** `_WITHHELD_TEMPLATE` becomes
`"[diagnostics withheld for slot {slot}: {reason}]\n"` and all four sites emit it, replacing the
two that emit nothing. One helper appends it, suppressing it to `""` when it contains a known
forbidden value — the same fail-closed rule `_capture_diagnostic_slot` already applies to
`_AGGREGATE_TRUNCATION_MARKER`, applied at every withhold site rather than at one.

**Bounds are unchanged.** The marker goes through `_DiagnosticCapture.append`, which bounds it
against the aggregate emission budget; per-slot emission is bounded where it already is. The
documented 320 KiB / 1.25 MiB acquisition and 256 KiB / 1 MiB emission numbers do not move.

**Documentation.** `deploy/systemd/README.md` gains a section defining all six `RetryAction`
values against the code that emits them. `docs/operating/runbooks/live-stack.md` gains a
wedged-slot recovery procedure built on the shipped `scripts/live-stack/worker-lifecycle.sh
recover`, mapping each reason to an operator action and naming what `recover` refuses.

**Not in scope**: redaction rules and forbidden-value detection (ADR-0574); the emission and
acquisition budgets (ADR-0574); any schema or protocol-identity change (#2532, merged); extending
`recover` to residual slots (#2533); the worker gate's post-restart disposition (#2486 /
ADR-0657); `stack-down.sh --force` and `docs/operating/systemd.md`.

## Success

1. Each of the five causes above yields its own `WithholdReason` member on the `SlotResult`
   message and in the emitted marker.
2. No reason value is derived from an exception string, a captured value, or the withheld report;
   every member is a literal in the enum.
3. The emitted marker is suppressed when it contains a value in the capture's known forbidden set.
4. The documented acquisition and emission numbers are unchanged, and marker bytes are accounted
   through the existing `append` bound.
5. `lifecycle_protocol_identity()` is byte-identical to its value at base `e363c265`.
6. `operator_recovery` and the other five `RetryAction` values are defined in
   `deploy/systemd/README.md`.
7. `docs/operating/runbooks/live-stack.md` carries a wedged-slot procedure an operator can follow
   with only the shipped contract, matching what `recover` does today.

## Failure model

**Actors and deployments.** A local operator on a provisioned live-stack host invoking
`scripts/live-stack/worker-lifecycle.sh diagnostics` over the root-owned peer-authenticated
lifecycle socket, and the socket-activated witness process that renders the response. No anonymous
or network actor reaches this surface: it is a unix socket restricted to the `kdive-live-control`
group. CI never runs it.

**Invariants and assets at stake.**

- No registered secret, and no structurally-detected secret, reaches the emitted diagnostics text
  or the response.
- `lifecycle_protocol_identity()` does not move, or every provisioned host fails closed.
- The per-slot and aggregate emission budgets hold, or a response exceeds `MAX_RESPONSE_BYTES`.

**Accepted failure classes.**

- The response's structural fields (`code`, `message`, `unit`, `phase`) are not passed through the
  redactor. Accepted: every value written there is a literal from a closed enum or a derived unit
  name, and this is unchanged from base — `_result` already writes `state.phase.value` there.
- A reason says a forbidden value was present without saying which. Accepted: that is the
  withholding's purpose, and the host journal remains the operator's source.
- `acquisition_failed` covers both a bounded and an unexpected failure inside `_diagnose_slot`.
  Accepted: the distinction is already only in the log line's `cause=`, and the action is the same.

**Covered elsewhere.**

- Which values count as forbidden, and how they are detected — ADR-0574.
- Slots `recover` cannot reach (absent or malformed state, drifted binding, rejected evidence,
  unreadable boot ID) — #2533; the runbook names them as out of reach rather than covering them.
- The worker gate's post-restart disposition — #2486 / ADR-0657.

## Threat model

**Boundaries and actors.** No boundary is added. One is widened: the emitted diagnostics text and
the `SlotResult.message` now carry a reason token at four sites, two of which emitted nothing
before. The untrusted input is the material acquired from systemd and the journal — text the
worker process, the guest, and anything that logged to that unit produced. The operator reading
the response is trusted; the host's registered redaction sources are trusted as inputs to the
redactor, never as output.

**Control per boundary.**

- Emitted marker: the reason is a literal from `WithholdReason`, so no acquired byte can reach it;
  the whole marker is checked against the capture's known forbidden set and suppressed to `""` on
  a hit — fail closed, no partial emission.
- `SlotResult.message`: the same closed vocabulary plus `SlotPhase`, both enums, bounded by the
  field's existing 1024-byte validator.
- Emission budget: unchanged — `_DiagnosticCapture.append` bounds every appended string against
  the remaining aggregate budget before retaining it.

**Explicitly out of scope.** A forbidden value the redactor does not detect (ADR-0574). An
operator who can read the host journal directly — they already can, by construction. Denial of
service through a slot that always withholds: the request deadline and the fixed eight-slot fleet
bound it, unchanged.

## Validation

Cases live in `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` unless named
otherwise.

- **`WithholdReason` members and their mapping to causes.** Mode: `focused-test` — one case per
  reason, asserting the `SlotResult.message` and the emitted marker.
- **Reason is never report-derived.** Mode: `focused-test` — a case whose withheld report and
  exception both carry a sentinel, asserting the sentinel is absent from the serialized response.
- **Marker suppression on a forbidden hit.** Mode: `focused-test` — a redaction source equal to a
  marker substring, asserting the emitted text is `""` while the `SlotResult` still names the
  reason.
- **Protocol identity unchanged.** Mode: `focused-test` — the existing pin in
  `tests/processes/lifecycle/systemd/test_systemd_worker_contract.py` stays green.
- **The two operator documents.** Mode: `task-test-not-applicable` — prose with no executable
  consumer; its machine-checkable properties (link targets, referenced paths) are covered by the
  `docs-links`, `docs-paths`, `served-doc-links`, and `docs-check` guards in `just ci`, and a test
  over the prose would pin wording rather than behaviour.
