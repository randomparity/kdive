# Same-boot invocation reconciliation — design

Issue #2485. Epic #2484 (R1, R6). Decision record:
[ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md).

## Problem

`_terminal_observation` (`src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`) is
the single place deciding whether a retained slot has terminal evidence. When the retained unit
reports a different `INVOCATION_ID` on the retained boot — what an out-of-band `systemctl restart`,
a `needrestart` sweep, or unattended upgrades produce — it raises `LifecycleConflict("systemd
invocation does not match retained state")`. Every lifecycle caller reaches that function, so `start`,
`status`, and `stop` all fail with `conflict / operator_recovery` and the slot cannot be reconciled
through the shipped contract.

## Scope

One rule inside `_terminal_observation` changes: a same-boot invocation mismatch for the retained
unit returns the `killed` outcome for the *retained* invocation instead of raising. The surrounding
rules keep their behaviour and their order — foreign unit raises `LifecycleConflict`, a bound phase
with no exact invocation raises `LifecycleConflict`, a differing boot ID returns `killed`, a
`BootObservation` on the retained boot raises `SystemdUnavailable`, and the membership rules follow
the changed line. The changed rule stays ahead of those membership rules so the successor's cgroup
membership is never read as the retained invocation's. The function keeps its owner and signature;
no caller changes, and no compatibility path is retained because no contract outside the module
observes the raise. Files changed:

- `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` — `_terminal_observation`,
  plus the ADR-0657 citation `adr-status-check` requires beside a record accepted in this PR.
- `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
- `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md` — new.
- `docs/adr/0574-systemd-supervises-host-worker-incarnations.md` — one appended amendment block
  under `## Decision`, adjacent to the claim it qualifies. Not a supersession banner: this is a
  partial supersession, which `docs/adr/README.md` directs to an amendment, and the
  `> **Superseded by …**` form recognized by `.github/scripts/profiles/adr.sh:47-49` would misstate
  a record whose other decisions stay in force.

Out of scope, with owners: gate behaviour under `deploy/systemd/**` (#2486); the `_map_failure`
message split (#2487); a `recover` operation (#2488); operator documentation and the diagnostics
reason vocabulary (#2489). ADR-0657 records the gate-side and `recover`-side decisions those
siblings implement; this change implements neither.

## Success

1. A retained slot in `GATED`, `REGISTERED`, or `STARTED` whose unit reports a successor
   invocation on the retained boot is retired through `stop` and through `start`: terminal evidence
   commits for the retained binding, the fence is released, the slot files are removed, and no
   `LifecycleConflict` is returned. This is retirement, not a return to service — see the residual
   below, which is #2488's.
2. The outcome published for that slot is `killed`, and the successor's `result`,
   `exec_main_status`, and `membership` are not read.
3. The four rules issue #2485 names — foreign unit, differing boot ID, `BootObservation` on the
   retained boot, and the two membership rules — behave exactly as before, each pinned by a test
   observed red under a controlled fault. The fifth rule in the function, a bound phase with no
   exact invocation, gets no test: `SlotState.validate_identity`
   (`src/kdive/processes/lifecycle/systemd/systemd_worker_state.py`) rejects a bound phase without
   both identifiers, so the state that would reach it cannot be constructed and the rule is
   unreachable defence in depth.
4. ADR-0657 is `Accepted` and ADR-0574 carries an amendment naming it.
5. `just ci` and `just records` are green.

## Failure model

**Actors and deployments.** The provisioned operator account, through the root-owned lifecycle
control socket on a live-stack systemd host (`deploy/systemd/install-live-worker-lifecycle.sh`,
the `live_vm_host` Ansible role). No other actor reaches this code: workers never hold the
lifecycle-witness authority (ADR-0536), and the Compose and Kubernetes deployments do not run this
coordinator. Designed for the native and hosted live-VM hosts; not designed for a host running two
concurrent live-stack flows, which ADR-0574's single-flow topology already excludes.

**Invariants and assets at stake.**

- Terminal evidence in `worker_incarnations` is durable and is what releases an artifact fence; a
  wrong `killed` row strands the fence of a live worker.
- Evidence is published only for the exact retained binding (unit, generation, boot, invocation).
- The gate's marker binding, which ADR-0574 requires to equal both the retained generation
  and the gate process's own `INVOCATION_ID`, keeps a replayed marker from releasing an
  unregistered generation.
- The unchanged rules in `_terminal_observation` are the fail-closed edges around the changed one.

**Accepted failure classes.**

- A deliberate operator restart of a busy unit retires that worker's incarnation on the next
  request: the restart already killed the cgroup (`KillMode=control-group`, `ExitType=cgroup`), so
  no live worker is affected.
- A slot whose worker survives its unit's restart: not reachable in the named deployments. A
  successor gate exits before `exec` under the strict binding, and `KillMode=control-group` takes
  the prior cgroup down with the unit.
- `killed` does not distinguish an out-of-band restart from another unobservable termination:
  bounded and, at merge, undistinguished anywhere. The gate emits one string
  (`release marker binding invariant failed`) for a restart and for tampering alike, and the
  coordinator logs nothing on the new path. A distinguishable disposition is #2486's, per epic
  #2484 R2; until it lands the evidence is the journal and the slot's own timeline.
- A successor invocation observed between `signal_terminate` and the terminal poll: bounded, the
  unit is `Restart=no`, and the outcome for the retained binding is `killed` either way.

**Covered elsewhere.** Gate behaviour after a restart — #2486, under ADR-0657. Residual slots this
rule cannot reach (unreadable invocation identity, absent unit) — #2488. Misattributed conflict and
authority messages — #2487. Operator procedure for `operator_recovery` — #2489. And the residual
this rule reaches but cannot finish — #2488: the successor's gate exits non-zero, leaving
`ActiveState=failed` with its `InvocationID` retained; `stop_retained`'s `systemctl stop` is a
no-op on a failed unit (reproduced on systemd 259: only `reset-failed` clears it), nothing in the
coordinator calls `reset-failed`, and `require_inactive` demands an empty `InvocationID`, so the
next `start` returns `conflict / operator_recovery` until an operator runs `systemctl reset-failed`
or #2488's recovery operation clears it. Not introduced here — any worker exiting non-zero already
leaves the same failed unit behind the same no-op — and anticipated by epic #2484, whose first
success criterion is satisfiable under the strict branch only once #2488 merges.

## Threat model

**Boundary inventory.** No boundary is added; one is widened — the observation the coordinator
reads from systemd for the retained unit may now *release* a slot in one more case. Untouched: the
control socket's `SO_PEERCRED` check, the gate's marker comparison, and the authority's
exact-binding check in `terminate_worker_incarnation`.

**Actor model.** The untrusted parties are anything running as a slot worker account or as any
non-operator local account. Trust is placed in the local systemd manager, which already owns unit
identity for every rule in this function, and in PostgreSQL's exact-binding check. A slot worker
cannot forge an observation: the coordinator reads systemd directly as root, not a file the worker
can write.

**Control per boundary.** Three checks stay in front of the widened one — the unit-identity check
rejects a foreign observation, the boot-ID check runs first so a different boot never reaches the
changed rule, and the authority re-verifies the exact retained binding before accepting evidence,
raising `EvidenceRejected` otherwise, which retains every host object and leaks no binding detail.

**Explicitly out of scope.** A compromised root account — it already owns every object this
contract protects. Tampering with retained slot files — root-owned, mode 0600 for state and
environment, 0400 for the credential and 0440 for the release marker, under a 0750
directory, with `SlotStore` re-validating immutable fields. Gate-marker replay — held by the strict
binding ADR-0657 keeps, implemented under #2486.

## Validation

Every entry is `Mode: focused-test` in
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` unless noted. The plan carries
each entry's red observation and green command.

- **A same-boot successor invocation reconciles through `stop`** — replaces
  `test_stale_same_boot_invocation_is_refused_without_signaling_or_cleanup`; red today on
  `response.code == "conflict"`, green on `ok` with `killed` for the retained incarnation and the
  slot files cleaned.
- **The same mismatch reconciles through `start`** — new test; the slot is retired and replaced.
- **The successor's exit facts are not attributed to the retained incarnation** — new test giving
  the successor `result="exit-code", status=2`; the outcome stays `killed`, not `failed`.
- **A foreign unit observation still raises `LifecycleConflict`** — new test; no test holds this
  rule today.
- **A differing boot ID still publishes `killed`** — existing
  `test_reboot_maps_exact_retained_binding_to_killed`, extended with a live-looking cgroup on the
  new boot so deleting the boot rule makes it red.
- **A `BootObservation` on the retained boot still raises `SystemdUnavailable`** — existing
  `test_same_boot_inactive_unit_is_not_terminal_evidence`.
- **`membership == "unknown"` on the retained invocation still raises `SystemdUnavailable`, and
  `populated` is still not terminal** — new test for `unknown`; `populated` is held by
  `test_stop_commits_evidence_before_unit_and_state_cleanup`.
- **ADR-0657 is Accepted and ADR-0574 records the amendment** — `just adr-status-check` and, after
  `git fetch origin main`, `just records`; red if the record is `Proposed` while `src/` cites it or
  if the amendment's shape fails the records gate.
