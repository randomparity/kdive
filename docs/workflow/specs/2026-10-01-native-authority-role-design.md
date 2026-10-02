# Native coverage cells and the authority role — design (#3066)

## Problem

`_native_cell` in `scripts/coverage_campaign/contract.py` gives every native cell the roles
`server`, `worker`, `reconciler` and `authority`. The default local live lane
(`examples/local-libvirt/demo-up.sh`) installs no provider authority, and no native scenario on
that lane routes through it (ADR-0623: with no authority binding, local-libvirt behaves as before).
So every native cell proven there fails `deployed-role-missing` although nothing it exercised
involves the authority.

The evidence seam has the opposite gap. `run_identity` in
`tests/integration/live_stack/evidence.py` drops the `authority` role whenever
`sudo -n cat /opt/kdive-provider-authority/revision` fails. An installed authority whose revision
cannot be read is recorded as "not installed", so a stale authority can hide.

## Decision

Recorded as amendments to
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md).

1. **Required roles follow the scenario.** Native cells require `server`, `worker` and
   `reconciler`. This covers image-smoke, deep-lifecycle, tcg-upload-boot, host-install,
   failure-resource and kernel-corpus. Tool cells keep their declared mechanism unchanged: a group's
   `authority = true` flag and its `role_overrides`. A future native scenario that routes through
   the authority (#2809, #2810) adds the role to its own cells in the change that implements it.
2. **An installed authority is always recorded or the run fails.** `run_identity` first asks whether
   the revision file exists, using `os.lstat`. A missing file or path component means not installed;
   no role is recorded and no `sudo` runs. Any other result counts as present: the file exists, or
   an unreadable directory means its absence cannot be shown. A present file must be read through
   `sudo -n` and resolved to a full SHA. If not, `run_identity` raises `RuntimeError` naming the
   path and the fix. Before this change it recorded nothing.
3. **Recorded roles are still judged.** `results.py` is unchanged. It fails any recorded role whose
   revision differs from the candidate (`deployed-revision-mismatch`). That includes an `authority`
   revision the cell does not require.

The matrix digest changes, so bindings computed before this change are invalid. This is expected
under ADR-0686.

## Failure model

1. **Actors and deployments**
   - A local operator running the live coverage carriers (`test_image_smoke_live.py` today) on a
     native demo-up host, with or without the provider authority installed.
   - `qualify` run offline by the operator or CI over the assembled records.
2. **Invariants and assets at stake**
   - Epic #2803 requirement 3: an involved deployed revision that is unknown or not the candidate
     fails. A cell may not qualify against a stale installed authority.
   - Coverage reports stay honest: a cell fails only for a reason that applies to it. An installed
     authority applies to every native cell on that host: once it is bound, local-libvirt can
     route through it (ADR-0623), so its involvement cannot be ruled out.
3. **Accepted failure classes**
   - An authority installed in a location other than `/opt/kdive-provider-authority` is not
     seen. Accepted: that path is where the `provider_authority_host` role installs it, per
     ADR-0715.
   - When `run_identity` raises, the cell writes no record and `qualify` reports
     `missing-result`. Accepted: no record can carry an identity that was never read, and the
     cell still fails.
   - The file is checked and then read, so it can be removed between the two calls. Accepted: the
     read then fails and the run stops.
4. **Covered elsewhere**
   - Installing the authority on the lane: #2812 and #2807.
   - Authority-routed deep-lifecycle and external-boot native cells: #2809 and #2810.
   - Whether the `authority = true` flags on the 2809 and 2814 tool groups are correct: #2809 and
     #2814.
   - Host-install witness role naming: #2807 (PR #3071).

## Success

- Building the contract yields no native cell with `authority` in `roles`. Tool cells keep it
  exactly where their group sets `authority = true` or a `role_overrides` entry lists it.
- `run_identity` behaves as follows:
  - Absent file: no `authority` role, and `read` is not called.
  - Present and resolvable: the resolved SHA is recorded.
  - Present but unreadable, empty or unresolvable: raises `RuntimeError`.
  - Presence unprovable because of a permission error: treated as present.
- On the native x86_64 demo-up lane at the candidate head, the #2808 image smoke qualifies its
  passing rows with no `deployed-role-missing`. Two controlled faults on that lane:
  - A stale installed revision gives `deployed-revision-mismatch`.
  - An installed revision `sudo -n` cannot read stops the run.
- ADR-0715 is amended without rewriting its lines. The coverage-qualification guide and the
  live-testing runbook describe the new behavior and drop the "until #3066" claim.

## Validation

- `focused-test`, `tests/scripts/test_coverage_contract.py`: native cells omit `authority`, and
  tool `authority` groups keep it. Red before task 1: native cells carry `authority`.
- `focused-test`, `tests/integration/live_stack/test_evidence.py`: the four `run_identity`
  cases, plus `_present` against a real temporary path (absent, present, and a mode-000 parent
  when not root). Red before task 2: an unreadable present file is silently dropped.
- `focused-test`, `tests/scripts/test_results.py`: a stale `authority` revision recorded for a cell
  that does not require the role fails `deployed-revision-mismatch`. This pins `results.py`, which
  this change relies on but does not modify; the test is green from the start.
- `task-test-not-applicable`, the ADR, guide and runbook prose: no executable consumer parses
  these sentences; `just records` and the doc checks gate the record's shape.
- Live proof: the image-smoke rerun and both faults, recorded and redacted in the PR.
