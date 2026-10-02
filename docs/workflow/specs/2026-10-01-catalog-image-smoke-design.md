# Smoke every native catalog image — design

Issue: #2808 (epic #2803 entry 5). Decision record:
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md).

## Problem

The coverage contract (ADR-0686) requires one `image-smoke/<image>/<arch>` cell per catalog row.
Each cell asserts `acquire`, `first-boot`, `authenticated-access`, `os-architecture`, `reboot` and
`cleanup`, and a `kind = "build"` row also asserts `build-toolchain`. No live test produces
evidence for these cells. The family reachability proof (`test_live_stack.py`) covers four
hand-staged images and stops at a succeeded `authorize_ssh_key` job. No producer records the
deployed role revisions, so no cell can qualify today.

## Scope

1. **Evidence seam** (`tests/integration/live_stack/evidence.py`), reusable by #2809-#2811.
   - `run_identity(base_url)` returns the candidate, the matrix digest, `host_os`
     (`ID:VERSION_ID` of the host `/etc/os-release`), `host_arch`, whether the checkout is
     clean, and the deployed revisions. ADR-0715 defines how each is read. A role that cannot be
     read is omitted.
   - `identity_problems(identity, roles)` names every missing role, every revision that differs
     from the candidate, and a dirty checkout.
   - `EvidenceWriter(root)` stores content-addressed artifacts and one `Evidence` record per cell.
   - `python -m tests.integration.live_stack.evidence assemble DIR --candidate SHA --out FILE`
     validates the records through `read_results` and writes those for `SHA`. It prints how many
     records it dropped for another candidate. The runbook starts each run with an empty `DIR`.
2. **Owned cleanup check** (`tests/integration/live_stack/cleanup.py`).
   - `release_and_verify` releases the Allocation and waits for the System to reach
     `torn_down`. It reads the Allocation through `allocations.wait(timeout_s=0)` until the
     status is `released`.
   - It requires the domain to be undefined on the worker's libvirt.
   - It requires every disk path from the domain XML to be absent. Only `FileNotFoundError`, or
     `sudo -n test -e` exiting 1 after a `PermissionError`, counts as absent.
   - It requires the summed `in_use` of `resources.availability` to equal the value read before
     the Allocation was requested.
3. **Image smoke**: `tests/integration/test_image_smoke_live.py::test_image_smoke`, marker
   `live_stack`, parametrized over the contract's `image-smoke` cells whose guest architecture is
   the host's. Probes and bindings live in `tests/integration/live_stack/image_smoke.py`.
   The test proves the assertions in order:
   - **acquire**: `images.list` and `images.describe` find a `registered` (bootable) local-libvirt image with the
     cell's name and architecture, staged beforehand by `examples/local-libvirt/build-image.sh`.
   - **first-boot**: provision `{kind: "catalog", name}`, wait for `ready`, and find the
     `kdive-ready` console marker.
   - **authenticated-access**: authorize a fresh ed25519 key with `systems.authorize_ssh_key`.
     SSH as `root` to the `systems.ssh_info` endpoint must report uid 0.
   - **os-architecture**: map the guest `/etc/os-release` `ID` to the catalog distro (`centos`
     maps to `centos-stream`). `VERSION_ID` must equal the catalog version or start with that
     version followed by `.`. `uname -m` must equal the catalog architecture.
   - **build-toolchain** (build rows only): the guest package database has every package in the
     family's `packages("build", …)`. `make` builds a C program with `gcc`, and the program runs.
   - **reboot**: `control.power` `cycle` drains, the System returns to `ready`, and SSH reports
     a different `boot_id`.
   - **cleanup**: `release_and_verify`.
4. **Bindings**: `python -m tests.integration.live_stack.image_smoke bindings --candidate SHA
   --out FILE` writes, for each native cell, the expected full `Context`:
   - the host `host_os` and `host_arch`;
   - the cell's `guest_arch` and `accelerator`;
   - `guest_os` set to the catalog `distro:version`;
   - `image_sha256` set to the SHA-256 of the staged file registered in `systems.toml`, or
     null when nothing is staged.
5. **Record**:
   - `node_id`, `scenario_id` and `cell_id` come from the cell.
   - `context` holds the observed values: the accelerator from the domain XML `type`, and the
     image digest with its `sha256:` prefix stripped. `guest_os` is the catalog identity only
     after `os-architecture` passes.
   - `input_sha256` is `digest(context)`.
   - The outcome:
     - `blocked` (`missing-prerequisite`) when the stack is configured but the issuer, the
       database or the registered image is missing;
     - `failure`, written before any mutation, when the revisions differ or the checkout is
       dirty. A missing role does not stop the scenario: it is recorded, and pytest fails;
     - `failure` with the assertions proven so far when any assertion fails;
     - otherwise `success`.
   - The pytest test fails unless the outcome is `success` and `identity_problems` is empty.
   - `KDIVE_STACK_BASE_URL` unset skips the test, following the tier's clean-skip contract
     (`just test-live-stack`). The qualifier then reports the cell `not-run`, which keeps the
     skipped scenario visible (issue Expected 3).
6. `obligations.toml` maps `image-smoke` to the node. The scenario ID is shared, so the six
   ppc64le cells then report `missing-result` (owner #2818) rather than
   `pending-implementation`. On a POWER host the node selects them, but proving them stays with
   #2818. The contract tests are updated to match.
7. `live-testing.md` documents the run.

## Failure model

1. **Actors and deployments**
   - an operator on one native x86_64/KVM host that runs the stack through `demo-up.sh`;
   - the offline qualifier.
2. **Invariants and assets at stake**
   - A cell qualifies only when its stack, image and assertions match the candidate and binding.
   - No owned domain, disk or capacity is retained after a successful run.
   - Evidence holds no host name, address, key or credential.
3. **Accepted failure classes**
   - A failed scenario may leave residue. The `finally` runs `release_and_verify` and records
     the attempt. The reconciler's orphan teardown (ADR-0021) removes what that attempt cannot.
   - Producer honesty is not attested (ADR-0686).
4. **Covered elsewhere**
   - ppc64le runs: #2818.
   - Scheduling: #2819.
   - The authority role on the demo-up lane: #3066.
   - Unbuildable rows: #3064 and #3065.

## Validation

- Focused unit tests run without a stack. They cover `run_identity` (a missing worker slot,
  mismatched slots, a dirty checkout), the writer and assembly, the disk check with an
  unsearchable parent, the probe parsers, and the bindings. One test passes a bindings file and
  a success record through `qualify`, and the only failure it allows is the missing role.
- The live run covers all native cells. Its sanitized inventory goes in the PR.
