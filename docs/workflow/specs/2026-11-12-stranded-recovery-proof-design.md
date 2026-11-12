# Stranded allocation recovery: runbook, real-DB proof and live proof (#3552)

## Problem

ADR-0816 ships `ops.recover_stranded_activation`, but no runbook says when it applies or what
its refusals mean, a `preparing` denial never names it, and nothing has driven the recovery
from the stranded shape to a released allocation.

## Scope

- New runbook `docs/operating/runbooks/stranded-activation-recovery.md`, indexed in
  `docs/operating/index.md`: preconditions (ADR-0816 upgrade order; no `runs.boot` before
  teardown), the calls in order (`observe`, `settle`, `ops.force_teardown`, then
  `allocations.release` or `ops.force_release`), the checks that the domain and allocation are
  gone, and a table of every refusal reason the tool returns: those in `settlement.py`,
  `SettlementStatus` and the observation handler.
- `admission.py`: a `preparing` denial on a System with no authority ownership row adds
  `ops.recover_stranded_activation`, derived from the matrix row as `_teardown_exits` is.
  ADR-0261 filtering hides it without `platform_admin`.
- New real-DB test `tests/db/test_stranded_recovery_end_to_end.py`; the fakes it shares with
  `tests/services/external_boot/test_stranded_observation.py` move to
  `tests/db/stranded_activation_support.py`.
- No live carrier: the fixture needs a stack restart on a second inventory midway, which the
  frame harness cannot do. The live proof follows the runbook.

### Failure model

1. Actors and deployments: a `platform_admin` operator on a stack whose servers, workers,
   reconciler and authority run this build; CI runs the real-DB test.
2. Invariants: no allocation is released while a domain of its System is on either daemon,
   and a settled System's teardown runs only on the worker holding its domain.
3. Accepted: remote-libvirt Systems stay stranded (`stranded_observation_unsupported`).
4. Covered elsewhere: authority-owned provision strands (#3584); held-object removal (#3587).

## Success

1. The runbook covers criterion 1 and the live run follows it unchanged.
2. Only the `preparing`, non-authority-owned denial gains the tool; other lists are unchanged.
3. Through the tool, the real-DB test drives the stranded shape (real worker handler and
   authority service, faked daemons) to a released allocation, asserting the worker domain
   was destroyed. It refuses settlement when the domain is on the authority's daemon or absent
   from the worker's, and refuses release before teardown.
4. Lab lane: the stack first runs an inventory whose `[[local_libvirt]]` entry has no
   authority field; the worker provisions a System; the stack restarts with
   `authority_instance` added and `runs.boot` strands it. Arms: A1 stranded and release
   refused; A2 observe and settle; A3 force teardown to `torn_down`; A4 release to `released`;
   A5 no domain on either daemon. The PR records each arm's result.

## Validation

- Success 1. Mode: task-test-not-applicable. Surface: runbook prose; no executable consumer
  reads it. The live run is its check.
- Success 2. Mode: focused-test. `tests/services/external_boot/test_admission.py` cases for
  the owned and unowned `preparing` denial. Red: the list lacks the tool. Green:
  `uv run pytest tests/services/external_boot/test_admission.py -q`.
- Success 3. Mode: focused-test. Red: skip the settlement read in
  `route_external_boot_teardown`, so teardown refuses. Green:
  `uv run pytest tests/db/test_stranded_recovery_end_to_end.py -q`.
- Success 4. Mode: task-test-not-applicable. Surface: the lab run on the installed authority
  lane. Evidence: daemon listings, the allocation row and tool responses, in the PR.
