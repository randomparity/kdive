# 0015 — The ppc64le finalization measurement arm has not been run

## Status

> **Resolved by the ppc64le confirmation run recorded in docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md** (2026-09-15)

## Concern

ADR-0655 decides the external-build completion contract from measured rows recorded on
**x86_64 only**. The measurement harness it rests on is arch-parameterized on both axes that
matter — the `arch` passed to `combined_kernel_tar`, which selects the boot member, and the
Run's `build_profile["arch"]`, which is what `_build_arch`
(`src/kdive/services/runs/complete_build.py`, named by symbol because this branch moved
its line) hands to the ADR-0343 per-arch check — and
the ppc64le arm runs the same code under `KDIVE_PPC64LE_BUNDLE`. It has never executed.

ADR-0655 names the outstanding ppc64le confirmation as one of four conditions that reopen its
decision. That makes the decision's own stated validity depend on an arm nobody is booked to
run: issue #2318 closes with the pull request that writes the ADR, so without this record the
reopening condition has no surviving owner and becomes unreachable in practice.

The exposure is not symmetric with the x86_64 rows. Finalization cost is driven by bytes and
member count — gzip/tar traversal, sha256 over the compressed object and the decompressed
members, and the ELF parse — and target architecture affects only the boot-member check and the
banner scan. So the x86_64 attribution is expected to carry, and the two recorded rows are
consistent with that: scan time tracked bundle size near-linearly (22.6 ns and 18.9 ns per
compressed byte across a 12× size range), which is what a bytes-driven cost looks like.
"Expected to carry" is still a hypothesis about a different architecture, and this record exists
to keep it visible until it is measured.

## Why deferred

No ppc64le bundle and no ppc64le cross-toolchain exist on the measurement host, and building
either is outside issue #2318's frozen scope — the operator approved that exclusion explicitly
on 2026-09-14 (exclusion 6 of the `WORK:SCOPE` charter, token `q2318-4672f7d3`).

The two ways to produce the input are both larger than the measurement they feed. Building a
ppc64le kernel under TCG emulation costs days. Cross-building one requires installing a
cross-toolchain and then declaring it in the Ansible role that owns that layer, which is a
provisioning change with its own review surface. Either would have expanded a bounded
measurement-and-decision issue into a toolchain issue.

Deferring costs little because the harness is finished and parameterized: when a bundle exists,
the arm is one `pytest` invocation against the already-merged driver, not new code.

## Non-regression boundary

- `tests/integration/test_finalization_measurement.py` must keep its `ppc64le` parameter. A
  change that drops the arm, or hard-codes `x86_64` into `bundle_for_arch` or the Run's
  `build_profile`, removes the only thing that makes this record cheap to resolve.
- The arm must keep skipping cleanly when `KDIVE_PPC64LE_BUNDLE` is unset and raising when it is
  set but lacks `kernel.tar.gz`, so a future run cannot silently produce no row.
- ADR-0655's `## Consequences` must keep naming the ppc64le confirmation as a reopening
  condition for as long as this record is open.

## What would resolve it

Supply a ppc64le bundle — a directory holding `kernel.tar.gz` cut by `combined_kernel_tar` with
`arch="ppc64le"`, whose `boot/vmlinuz` is the stripped ELF `vmlinux` per ADR-0343 — and run:

```sh
KDIVE_PPC64LE_BUNDLE=<dir> \
uv run python -m pytest tests/integration/test_finalization_measurement.py \
  -m live_stack -k ppc64le -q
```

against a live stack, then append the recorded row to
`docs/design/2026-09-14-external-build-finalization-measurement-2318-proof-record.md`.

Done when that row exists and one of two things is true: its phase attribution agrees with the
x86_64 rows, in which case this record is resolved and ADR-0655 stands with the ppc64le
reopening condition discharged; or it does not, in which case ADR-0655 is reopened by a
superseding ADR and this record is resolved by pointing at it.

## Provenance

target: tests/integration/test_finalization_measurement.py
Deferred while implementing issue #2318 on 2026-09-14, under exclusion 6 of that issue's
approved scope charter. Recorded because ADR-0655 makes the unrun arm a condition on its own
decision, and the issue that deferred it closes with the same pull request.
