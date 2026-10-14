# Deterministic broken/fixed kernel corpus exercises

## Problem and authority

Issue #2817 (epic #2803 requirement 12, entry 14) asks for two deterministic kernel
exercises that distinguish a vulnerable upstream revision from its fix through the public
HTTP surface. The coverage contract already declares the cells: for each architecture,
`kernel-corpus/local-libvirt/<arch>/case-{1,2}/{broken,fixed}`, owner #2817, with assertions
`broken` or `fixed`, `same-reproducer-config`, `debug-or-crash-evidence` and `cleanup`, and the
six kernel inputs of a native cell. None is bound to a test today.

The frozen charter is the issue's `WORK:SCOPE` (token `q2817-00d4ea39`, campaign
`73d4edacff-8cf13f64-8877-4610-beaf-496193501043`). The 2026-10-07 exclusion set applies:
native ppc64le and any POWER-specific case stay with #2818, note citations with #3156 (closed),
and autonomous A/B evaluation is out of scope. No product source, public contract or ADR changes.

## Case selection

The human notes in `docs/development/kernel-test-cases/` are selection evidence only; nothing
parses them. Two cases were chosen because each needs no extra kernel options beyond the
existing fixture fragment, no guest package, no race and no timing, and each has a single-file
upstream fix:

| Case | Note | Fix (mainline) | Vulnerable revision | Reproducer | Broken outcome | Fixed outcome |
|---|---|---|---|---|---|---|
| `case-1` | 05, dcache `dhash_entries=1` | `f08fe8891c3e` (v7.1-rc1) | parent `970296997869` | boot with `dhash_entries=1` | page fault oops with `RIP: 0010:__d_lookup`, panic before readiness | ready boot with the parameter applied, two dentry hash buckets, no crash signature |
| `case-2` | 16, procfs parent nlink | `16b02eb4b9b2` (v7.2-rc2) | parent `681e452683b6` | read link and entry counts of every directory under `/proc/bus/pci` | link count exceeds `2 + subdirectories` by at least the regular-file count | link count equals `2 + subdirectories` everywhere |

The vulnerable revision is the fix commit's first parent, so the two revisions of a case differ
only in the fix and build from one configuration fragment. Before use, each fix commit was
checked in the mainline repository: it exists, its parent is the named vulnerable commit, its
diff touches only the note's subsystem file (`fs/dcache.c`, `fs/proc/generic.c`), and
`git describe --contains` places it in the release the note names. An offline pre-check on an
x86_64 KVM host built all four revisions with the fixture fragment and booted them directly
under QEMU: both case pairs produced byte-identical effective `.config` files; case-1 broken
oopsed in `__d_lookup` and panicked (`Dentry cache hash table entries: 1`), case-1 fixed
reported two entries and booted past the lookup; case-2 broken reported `/proc/bus/pci/00`
`nlink=8` with six files and no subdirectories, case-2 fixed reported `nlink=2`. The pre-check
is selection evidence, not cell evidence. The four fixtures were then built and verified with the
extended `scripts/kernel_fixtures.py` on the same host; each case's two manifests carry equal
`input.config` and `.config` digests, and the releases (`6.19.0-g970296997869`,
`6.19.0-gf08fe8891c3e`, `7.2.0-rc1-g681e452683b6`, `7.2.0-rc1-g16b02eb4b9b2`) differ per revision.

## Fixture inputs

`fixtures/kernel/corpus.toml` pins the four revisions beside `baselines.toml`:

```toml
repository = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
selected = "2026-10-14"
selection_source = "docs/development/kernel-test-cases/"

[case-1-broken]
note = "05-dcache-dhash-entries-oob-read.md"
commit = "<full sha>"
```

`scripts/kernel_fixtures.py` resolves a selection name from either file: `longterm` and
`stable` from `baselines.toml`, `case-<n>-<revision>` from `corpus.toml`. `build` and `verify`
are otherwise unchanged: the same fragment, the same fresh-output rule, the same manifest with
its `source` equal to the selected entry, and the same source recheck through
`fetch-kernel-tree.sh` (which already fetches a full commit from any repository). The CLI's
`--baseline` accepts every selection name; an unknown name fails with the list of valid ones.
ADR-0693's decision (explicit source commits, verified manifest, reuse only after verify)
covers these selections unchanged.

## Contract

`contract.py` is unchanged: the eight `kernel-corpus` cells and their assertions already exist.
Four `[implementations]` rows in `obligations.toml` bind the arch-free scenario ids, so the
ppc64le cells share the node; `tests/scripts/test_coverage_contract.py` counts them as bound.
The contract names #2817 as the owner of the ppc64le corpus cells too, while #2818 owns every
other ppc64le cell; that routing is reported as a follow-up rather than changed here.

## Carrier

`tests/integration/live_stack/kernel_corpus.py` holds the case table, the outcome checks, and
a `bindings` command. `tests/integration/test_kernel_corpus_live.py::test_kernel_corpus` has
one parameter per `kernel-corpus` cell whose guest architecture is the host's; the four
`[implementations]` rows bind `kernel-corpus/case-{1,2}/{broken,fixed}` to it. A ppc64le host
would run the four ppc64le cells through the same node; they stay `missing-result` (#2818)
until a native POWER run, and a ppc64le fixture build is #2818's work.

Each parameter:

1. Loads and verifies both fixtures of its case (`$KDIVE_FIXTURE_ROOT/case-<n>-broken` and
   `-fixed`). A missing or rejected fixture stops the cell as `blocked`
   (`missing-prerequisite`). It proves `same-reproducer-config`: the two manifests carry the
   same `input.config` and `.config` digests, and the reproducer is the case table's one
   constant for both revisions.
2. Runs on the existing `on_catalog_system` frame with the Fedora 44 representative
   (`fedora-kdive-ready-44`): acquire, allocate, provision to `ready`, body, then the frame's
   `cleanup` proof (allocation released, System torn down, domain and owned disks absent,
   capacity restored).
3. The body authorizes an SSH key and probes the old boot ID, opens an investigation, creates a
   Run, uploads the revision's fixture with `vmlinux` (as the deep lifecycle does), installs
   with the case's `cmdline` (case-1: `dhash_entries=1`; case-2: none) and boots. A case-1 Run
   declares `expected_boot_failure = {kind: console_crash, pattern: "RIP: 0010:__d_lookup"}`
   on both revisions, so the declaration is part of the shared reproducer. The domain's direct
   kernel must be the uploaded boot image (digest equality), as in the deep lifecycle.
4. Checks the revision outcome below, proves `broken` or `fixed` and
   `debug-or-crash-evidence`, closes the investigation.

### Outcomes

`case-1/broken`: the boot job drains `succeeded`; `runs.get` reports
`boot_outcome = expected_crash_observed` and a matched line containing `RIP: 0010:__d_lookup`.
The Run's console artifact (`refs.console`, read in full through `artifacts.get`) must contain
`Dentry cache hash table entries: 1`, `BUG: unable to handle page fault`, the RIP line, and a
`Linux version <release>` banner equal to the fixture release. `debug-or-crash-evidence`
resolves the RIP frame and every reliable call-trace frame `sym+0xOFF/0xSIZE` between the first
`Call Trace:` and its `</TASK>` against the fixture's `System.map`: the symbol exists, its distance
to the next `System.map` address (the kallsyms size) equals `SIZE`, and `OFF < SIZE`. Frames
marked `? ` are stack guesses and are skipped. At least three frames must resolve, the first in
`__d_lookup` or `__d_lookup_rcu`; a frame naming a symbol or size absent from the map fails the
cell. On the pre-check console all 14 reliable frames resolved against the matching build, and
another build's map rejected them.

`case-1/fixed`: the boot drains `succeeded` with `boot_outcome` ready and no matched line. Over
SSH, a new boot ID, `/proc/cmdline` containing `dhash_entries=1`, the running release and the
GNU build ID from `/sys/kernel/notes` equal the fixture's. The console must contain
`Dentry cache hash table entries: 2` and no crash signature (`first_crash_signature`).
`debug-or-crash-evidence` records the build ID, release, cmdline and the dentry-hash line.

`case-2/{broken,fixed}`: the boot reaches readiness; the SSH probe reports a new boot ID, the
release and build ID, and for every directory under `/proc/bus/pci` its link count and its
subdirectory and non-directory entry counts. At least one directory must hold a regular file.
Broken: every directory's link count is at least `2 + subdirectories + files`, so each excess
is at least its file count. Fixed: every link count equals `2 + subdirectories`.
`debug-or-crash-evidence` records the per-directory inventory with the running build ID.

Any other observation fails the cell: a timeout, a boot failure, a crash on the fixed revision,
a clean boot on case-1 broken, or a mixed or empty inventory on case-2 is a `failure`, never
`success` and never `unsupported`.

### Identity

The record's observed context carries the guest OS and architecture, the accelerator, the
image digest, the installed kernel digest, and the fixture's source commit, compiler identity
and effective-config digest (of the uploaded `effective_config`). The build ID is the running
kernel's (`/sys/kernel/notes`) where the guest reaches userspace; on case-1 broken, which
never does, it is the uploaded `vmlinux` build ID, which `runs.complete_build` accepted and
whose boot image the domain is proven to run. `bindings --candidate SHA --out FILE` writes the
expected context of each native `kernel-corpus` cell from the staged image and the verified
fixture of that cell's revision; an absent fixture leaves the kernel fields null, which
`qualify` reports as a missing input.

## Failure model

- Missing `KDIVE_FIXTURE_ROOT`, a fixture `verify` rejects, or either revision of the case
  absent: `blocked` (`missing-prerequisite`), no System created.
- Unregistered image: the frame's existing `blocked` stop.
- Stack not at the candidate: the frame's existing `failure` before any mutation.
- Boot outcome or observation outside the case's expected outcome: `failure`; the cleanup
  attempt is recorded as an artifact.
- A crashed guest is released by the same `allocations.release` path as a ready one; the
  frame's cleanup proof applies unchanged, so a leaked domain or disk fails `cleanup`.
- A fixture that changes during the run fails the cell (re-verify after the body).

## Verification

Unit tests (no stack): selection lookup across both files, unknown selection, and a corpus
selection recorded in and verified against a manifest; the oops parser and `System.map`
resolution (matching size, wrong size, unknown symbol, no trace); the nlink classifier
(broken, fixed, mixed, empty, no files); the case table's single reproducer per case; the
bindings writer with and without fixtures; and that each native `kernel-corpus` cell has
exactly one implementation bound to the new node. Each new check is shown to bite with a
controlled fault.

Live: the four x86_64 cells on a local Fedora 44 KVM host on the `demo-up` lane, fixtures built
there with `scripts/kernel_fixtures.py build`, bindings, assembly and `qualify`, deployed
server, worker and reconciler at the committed candidate. The runbook records the last run.

## Alternatives

- Pick boot-only cases for both (05 and 11). Rejected: case 11 crashes before the console is
  fully up and depends on an early-boot debug attach; case 16 adds a userspace observation of a
  non-crash defect.
- Use a released stable tag as the vulnerable revision. Rejected: two releases differ in many
  commits and usually in configuration; the fix's parent isolates the fix.
- Declare the expected crash only on the broken Run. Rejected: the declaration is part of the
  reproducer, and its non-match on the fixed revision is the specific "failure absent" signal.
- Attach a gdbstub session to the panicked case-1 guest. Deferred: the console oops already
  resolves against the build's symbol map, and live debug coverage is #2813's.
