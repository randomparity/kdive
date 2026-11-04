# Remote-authority ppc64le vmlinux proof (#3512)

## Problem

The remote staging implementation already exists (#3131), but its live carrier hardcodes
x86_64. The remote authority frame already validates the selected guest architecture against
the provider host and selects the matching registered root. No staging code needs changing.

## Scope

Authority: issue #3512 and its `WORK:SCOPE` token `q3512-c782dd94`, approved 2026-11-04.
Retain x86_64 and add a repeatable ppc64le remote-authority arm. Local staging remains #3130;
legacy remote delivery remains #3124. Other parent criteria remain unchanged.

Read the configured authority host architecture with the existing `select_remote_lane` and
`remote_authority_inventory` boundaries; accept x86_64 and ppc64le, feeding both Cell fields.
The existing frame rechecks host architecture before allocation. Architecture-qualified cell
IDs keep evidence distinct. The unfiltered x86 invocation stays compatible. Reuse `_prove`
unchanged: upload, activate, guest SHA-256, real default-search drgn, release, baseline boot,
restored absence, and frame cleanup. Observed architecture is smaller than a second carrier or a configuration key, and avoids
failing the unselected architecture in an unfiltered run. A duplicate lifecycle would drift.

### Failure model

- Deployment: configured remote authority on native x86_64 or ppc64le; native POWER/KVM-HV
  is required for the new proof. TCG results cannot complete #3512.
- Missing lane remains dormant in ordinary collection. An unsupported observed architecture
  fails before entering the frame; architecture drift fails its existing precondition before
  allocation. No inventory mutation precedes those checks.
- Failed activation retains existing release/frame cleanup. Product failures are evidence to
  investigate and report, never grounds to loosen assertions or count skips as success.
- Missing matching filesystem, modules, guest agent, or drgn prevents proof. The uploaded
  kernel must support the actual root filesystem, including CONFIG_BTRFS_FS for a btrfs root.
- Existing deployment revision checks and root provenance checks remain authoritative.

## Success

1. Both supported authority architectures route to the matching existing frame Cell.
2. A native ppc64le remote run proves the uploaded SHA-256, drgn init_uts_ns release, restore
   removal, and unchanged authority domains/volumes/capacity.
3. The runbook gives an exact arm selector and prerequisites; a dated proof record identifies
   tested revision, native accelerator, kernel/config provenance, observations and cleanup.
   Hardware unavailable or a failed run is recorded as incomplete; the issue stays open.

## Validation

- Focused boundary test supplies observed x86_64/ppc64le through the inventory boundary,
  intercepts its frame, and verifies matching native Cells; unsupported architecture must fail
  before entering the frame. Red: current carrier still routes ppc64le as x86_64.
  Green: focused Linux pytest file.
- Native live execution is the functional proof,
  with deployed revision equal to the test candidate and a matching complete kernel tree.
- Runbook/spec/proof prose: task-test-not-applicable; no executable consumer validates prose.
  Run the repository doc guards. Never add tests that snapshot wording.
