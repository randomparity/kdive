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
| `slot_unusable` | preconditions unmet: no exact invocation, no safe budget, or redaction sources that are unreadable or rejected as unsafe | `StateConflict`/`OSError` → `:389-398` |
| `acquisition_failed` | systemd, the journal, or the request deadline did not answer | `_diagnose_slot` → `:381-388` |
| `redaction_refused` | a forbidden value survived this slot's own redaction, or no safe sentinel could render it | `_diagnose_trusted_slot` and `_sanitize_diagnostics` → `:381-388` |
| `peer_redaction_refused` | the report holds a forbidden value learned from another slot | `:401-403` |
| `internal_error` | anything else escaping the capture loop | `:389-398` |

Two of these split a site rather than adding one. The two `_UnsafeDiagnosticText` causes share one
site today and are two of the three the issue says an operator cannot tell apart, so the private
exception carries the reason its raiser knows. And the bare `except Exception` arm is reached by
everything the three calls before `_diagnose_slot`'s own `try` can raise — the deterministic
`StateConflict`s from `_require_diagnostic_budget` and `_validated_redaction_values`, and the
`OSError` from loading the slot's redaction sources. Those are operator-fixable preconditions, not
unexpected failures, so a `(StateConflict, OSError)` arm ahead of the generic one gives them
`slot_unusable`.

One relabelling is needed inside `_diagnose_slot` for the vocabulary to be true. `acquisition_failures`
lists `StateConflict`, so the refusal `_sanitize_diagnostics` raises when no safe visible sentinel
survives would be reported as `acquisition_failed` — sending the operator to `systemctl status` and
a re-run that deterministically fails the same way. A `StateConflict` arm ahead of
`self._acquisition_failures` gives that refusal `redaction_refused`, which is what it is.

**Where the reason rides.** On the existing free-form `SlotResult.message`
(`StringConstraints(max_length=1024)`) as `withheld: <reason>`. A withheld slot's phase rides the
typed `SlotResult.phase` field rather than being string-encoded into the message a second time;
`scripts/live-stack/worker-lifecycle.sh` is the one machine reader of that field. Only withheld
results change: a successful capture's result keeps the shape it has today, because no completion
criterion asks for it. No field is added to `LifecycleRequest`, `LifecycleResponse`, or
`SlotResult`: those schemas are hashed by `lifecycle_protocol_identity()`, and a moved identity
fails the client closed until every provisioned host is reprovisioned. Populating an existing
field does not move that hash. The constraint is ADR-0574's and #2532's, not a decision taken
here, and the message is operator-facing output with no parser — so no ADR is written for it.

**In the emitted text.** `_WITHHELD_TEMPLATE` becomes
`"[diagnostics withheld for slot {slot}: {reason}]\n"` and all four sites emit it, replacing the
two that emit nothing. One helper appends it, suppressing it to `""` when it contains a known
forbidden value — the same fail-closed rule `_capture_diagnostic_slot` already applies to
`_AGGREGATE_TRUNCATION_MARKER`, applied at every withhold site rather than at one.

**Bounds are unchanged.** The marker goes through `_DiagnosticCapture.append`, which bounds it
against the aggregate emission budget; per-slot emission is bounded where it already is. The
documented 320 KiB / 1.25 MiB acquisition and 256 KiB / 1 MiB emission numbers do not move.

**Documentation.** `deploy/systemd/README.md` gains a section defining all six `RetryAction`
values against the code that emits them, across both `systemd_worker_lifecycle.py` and the control
module `systemd_worker_control.py`. `docs/operating/runbooks/live-stack.md` gains a wedged-slot
recovery procedure built on the shipped `scripts/live-stack/worker-lifecycle.sh recover`, mapping
each reason to an operator action and naming what `recover` refuses and what it requires.

**Not in scope**: redaction rules and forbidden-value detection (ADR-0574); the emission and
acquisition budgets (ADR-0574); any schema or protocol-identity change (#2532, merged); extending
`recover` to residual slots (#2533); the worker gate's post-restart disposition (#2486 /
ADR-0657); `stack-down.sh --force` and `docs/operating/systemd.md`.

## Success

1. Each of the six causes above yields its own `WithholdReason` member on the `SlotResult` message
   and in the emitted marker.
2. No reason value is derived from an exception string, a captured value, or the withheld report;
   every member is a literal in the enum.
3. The emitted marker is suppressed when it contains a value in the capture's known forbidden set.
4. The documented acquisition and emission numbers are unchanged, and marker bytes are accounted
   through the existing `append` bound.
5. `lifecycle_protocol_identity()` is byte-identical to its value at base `e363c265`, proved by
   comparing the computed identity at that commit with the identity at HEAD.
6. `operator_recovery` and the other five `RetryAction` values are defined in
   `deploy/systemd/README.md`.
7. `docs/operating/runbooks/live-stack.md` carries a wedged-slot procedure an operator can follow
   with only the shipped contract, matching what `recover` does today.

## Failure model

**Actors and deployments.** Two, both reading the same response:

- A local operator on a provisioned live-stack host, invoking
  `scripts/live-stack/worker-lifecycle.sh diagnostics` over the root-owned peer-authenticated
  lifecycle socket, and the socket-activated witness process that renders the response. No
  anonymous or network actor reaches the socket: it is restricted to the `kdive-live-control`
  group.
- CI. `.github/workflows/live.yml:661` (tcg job, `if: always()`) and `:817` (native job,
  `if: failure() || cancelled()`) run the same command, and
  `tests/scripts/test_live_workflow_shape.py` pins both steps. The client ends with
  `print(response.model_dump_json())`, so the whole response — reasons and marker text included —
  lands in a retained GitHub Actions log readable by anyone with repository read access.

**Invariants and assets at stake.**

- No registered secret, and no structurally-detected secret, reaches the emitted diagnostics text
  or the response — in either deployment.
- `lifecycle_protocol_identity()` does not move, or every provisioned host fails closed.
- The per-slot and aggregate emission budgets hold, or a response exceeds `MAX_RESPONSE_BYTES`.

**Accepted failure classes.**

- The response's structural fields (`code`, `message`, `unit`, `phase`) are not passed through the
  redactor. Accepted: every value written there is a literal from a closed enum or a derived unit
  name, and this is unchanged from base.
- A reason says a forbidden value was present without saying which. Accepted: that is the
  withholding's purpose, and the host journal remains the operator's source.
- `acquisition_failed` covers systemd and journal failures, the request deadline
  (`acquisition_failures` includes `LifecycleDeadlineExceeded` and `CommandDeadlineExceeded`), and
  any unexpected failure inside `_diagnose_trusted_slot`. Accepted: the operator's first action is
  the same for all of them — read the unit's systemd and journal state — and the runbook row adds
  the deadline's extra step, re-running `diagnostics`. The one cause this entry does *not* accept
  is the deterministic redaction refusal, because re-running never clears it; that is why
  `_diagnose_slot` gives it `redaction_refused` instead of letting `acquisition_failures` absorb it.

**Covered elsewhere.**

- Which values count as forbidden, and how they are detected — ADR-0574.
- Slots `recover` cannot reach (absent or malformed state, drifted binding, rejected evidence,
  unreadable boot ID) — #2533; the runbook names them as out of reach rather than covering them.
- The worker gate's post-restart disposition — #2486 / ADR-0657.

## Threat model

**Boundaries and actors.** No boundary is added. One is widened: the emitted diagnostics text and
the `SlotResult.message` now carry a reason token at four sites, two of which emitted nothing
before. The untrusted input is the material acquired from systemd and the journal — text the
worker process, the guest, and anything that logged to that unit produced. Both readers named in
the failure model are trusted with the response's content; the CI reader is the wider audience, so
the emitted text has to be safe for a public log, not only for a terminal. The host's registered
redaction sources are trusted as inputs to the redactor, never as output.

**Control per boundary.**

- Emitted marker: the reason is a literal from `WithholdReason`, so no acquired byte can reach it;
  the whole marker is checked against the forbidden set the capture knows **at the moment it is
  appended** and suppressed to `""` on a hit — fail closed, no partial emission. The check is not
  retroactive: `capture.reports` is append-only and is never re-scanned, so a value a later slot
  contributes does not retire an earlier marker. That bound is the same one the aggregate
  truncation marker has always had, and it is stated here rather than implied, because the marker
  text is fixed-form and carries no acquired material either way.
- Emitted marker in a CI log: the live.yml steps wrap the output in a `::stop-commands::` fence
  because they treat it as untrusted. The marker's fixed form carries no `::`, so it cannot escape
  that fence; the closed vocabulary is what guarantees this rather than an escaping pass.
- `SlotResult.message` and `SlotResult.phase`: the closed vocabulary and `SlotPhase`, both enums,
  bounded by the message field's existing 1024-byte validator.
- Emission budget: unchanged — `_DiagnosticCapture.append` bounds every appended string against
  the remaining aggregate budget before retaining it.

**Explicitly out of scope.** A forbidden value the redactor does not detect (ADR-0574). An
operator who can read the host journal directly — they already can, by construction. Denial of
service through a slot that always withholds: the request deadline and the fixed eight-slot fleet
bound it, unchanged.

## Validation

Cases live in `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

- **`WithholdReason` members and their mapping to causes.** Mode: `focused-test` — the six causes
  covered across new and updated cases, asserting the withheld `SlotResult`'s `message` and
  `phase` and the emitted marker.
- **Reason is never report-derived.** Mode: `focused-test` — a case whose withheld report and
  exception both carry a sentinel, asserting the sentinel is absent from the serialized response.
- **Marker suppression on a forbidden hit.** Mode: `focused-test` — a withholding trigger plus a
  redaction source equal to a marker substring, asserting the emitted text is `""` while the
  `SlotResult` still names the reason.
- **Protocol identity unchanged.** Mode: `task-test-not-applicable` — the identity is a pure
  function of `systemd_worker_contract.py`'s two schemas, and the contract being verified is the
  absence of a change to them. A pinned-hash assertion is the one shape this repository has
  already rejected: `test_systemd_worker_contract.py`'s own docstring records that an inequality
  against a frozen hash passes for any schema change at all. The observation that does bite is
  structural and repeatable — `git diff --exit-code e363c265 -- <that file>` — and the plan runs
  it as a task step.
- **The two operator documents.** Mode: `task-test-not-applicable` — prose with no executable
  consumer. `docs-links` resolves a link's file and discards its `#fragment`, so the plan's tasks
  grep each target heading directly; `docs-paths` covers referenced `docs/<path>` strings.
  `docs-check` and `served-doc-links` touch neither file. A test over the prose would pin wording
  rather than behaviour.
