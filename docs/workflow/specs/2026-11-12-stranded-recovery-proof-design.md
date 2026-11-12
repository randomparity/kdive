# Stranded allocation recovery: runbook, real-DB proof and live proof (#3552)

## Problem

ADR-0816 ships `ops.recover_stranded_activation`, but no runbook says when it applies or what
its refusals mean, a `preparing` denial never names it, and nothing has driven the recovery
from the stranded shape to a released allocation.

## Scope

- New runbook `docs/operating/runbooks/stranded-activation-recovery.md`, indexed in
  `docs/operating/index.md`: when it applies, `observe`, `settle`, `ops.force_teardown`,
  `allocations.release`, the checks that the domain and allocation are gone, and each refusal
  (`authority_objects_present`, `authority_domain_present`, `worker_domain_absent`,
  `stranded_observation_unsupported`, `observation_missing`, `*_evidence_missing`).
- `admission.py`: a `preparing` denial adds `ops.recover_stranded_activation` to its next
  actions. ADR-0261 filtering already hides it from callers without `platform_admin`.
- New real-DB test `tests/db/test_stranded_recovery_end_to_end.py`. The fakes it shares with
  `tests/services/external_boot/test_stranded_observation.py` move to
  `tests/db/stranded_activation_support.py`, and that file imports them from there.
- No live carrier: the fixture needs a stack restart on a second inventory midway, which the
  frame harness cannot do. The live proof follows the runbook on the lab lane.

### Failure model

1. Actors and deployments: a `platform_admin` operator on a stack whose servers, workers,
   reconciler and authority all run this build. CI runs the real-DB test.
2. Invariants: no allocation is released and no System is torn down while a domain of the
   System is present on either daemon.
3. Accepted: a remote-libvirt System stays stranded, because that provider refuses with
   `stranded_observation_unsupported` (ADR-0816). The runbook names the refusal.
4. Covered elsewhere: authority-owned provision strands (#3584); held-object removal (#3587).

## Success

1. The runbook covers each item that criterion 1 lists. The live run follows it unchanged.
2. A `preparing` denial lists `ops.recover_stranded_activation`. Other states' lists are
   unchanged.
3. The real-DB test drives the stranded shape through the tool's `observe` (real worker handler,
   real authority service, faked daemons), `settle`, `ops.force_teardown` and the worker
   teardown to a released allocation. It refuses when the domain is on the authority's daemon
   or missing from the worker's, and while the settled System still holds its worker domain.
4. On the lab lane, a System that the fixed worker provisioned before the route was added
   strands on `runs.boot`. Recovery ends with the allocation `released` and no domain on
   either daemon. The PR records which arms ran.

## Validation

- Success 1. Mode: task-test-not-applicable. Surface: runbook prose; no executable consumer
  reads it. The live run is its check.
- Success 2. Mode: focused-test. `test_denial_carries_the_state_s_next_actions_off_details`
  gains `ops.recover_stranded_activation` in the `PREPARING` row. Red: the list lacks it.
  Green: `uv run pytest tests/services/external_boot/test_admission.py -q`.
- Success 3. Mode: focused-test. Cases: settle then release; domain on the authority; domain
  absent from the worker; release refused before teardown. Red: break the authority daemon
  fake so that it reports the domain absent. The authority-present case then fails because it
  settles. Green: `uv run pytest tests/db/test_stranded_recovery_end_to_end.py -q`.
- Success 4. Mode: task-test-not-applicable. Surface: the lab run, which needs the installed
  authority lane. Evidence: both daemons' domain listings, the allocation row and the tool
  responses, recorded in the PR.
