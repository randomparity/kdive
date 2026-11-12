# Recovering an allocation stranded by a failed authority-owned provision (#3584)

## Problem

`live-testing.md` ("Live authority lab") says no runbook covers an Allocation held by a failed
authority-owned System provision, and claims teardown is refused while the recorded binding
mismatches the manifest. Neither claim has been checked on a live lab.

## Scope

- New subsection under "Live authority lab" in `docs/operating/runbooks/live-testing.md`:
  one entry per stranded shape (binding matches the manifest; binding mismatches it; worker died
  on the final provision attempt), each with the symptom, the calls in order, and the states
  observed after each call. The "No runbook documents ..." sentence is replaced by a link.
- The worker-died shape links `exhausted-authority-system-provision.md` instead of restating it,
  and adds only what the live run observed beyond it.
- If the mismatch shape has no operator exit, the entry says so and names lab rollback; the
  missing exit is reported as a follow-up candidate, not fixed.
- `live-stack.md` gets one pointer line near the wedged-slot section;
  `stranded-activation-recovery.md` is already linked from the new text and is not edited unless
  its cross-reference becomes wrong.
- Evidence comes from one live run on an Ubuntu 26.04 local-authority lab at the merge base.
  A shape the run cannot reproduce is labelled not reproduced, with the reason.

### Failure model

1. Actors and deployments: an operator with project `admin` (and `platform_admin` where a step
   needs it) on a live authority lab built from the runbooks.
2. Invariants: no step releases an Allocation while a domain of its System may still run; no
   step writes job or System rows by hand.
3. Accepted: commands differ on a build other than the merge base — bounded, the section names
   no version guarantee beyond the tools it cites.
4. Covered elsewhere: stranded `runs.boot` activations (#3552 runbook); held-object removal
   (#3587); code changes to teardown or release (operator exclusion).

## Success

1. Each of the three shapes has an entry whose sequence ran on the lab, or an explicit
   not-reproduced statement with its reason.
2. The binding-mismatch sentence matches what the lab observed and names the real exit.
3. `live-stack.md` links the new subsection.
4. No sequence in the new text is presented as proven unless the lab run executed it.

## Validation

- Contract: runbook sequences (Success 1, 2, 4). Mode: task-test-not-applicable — the changed
  surface is operator prose with no executable consumer; the proof is the private live run whose
  command log and DB/daemon snapshots are recorded in the PR body.
- Contract: links and anchors (Success 3). Mode: focused-test — `just docs-links` (scripts/check-doc-links.sh)
  fails red on a missing target or heading anchor; green after the fix.
