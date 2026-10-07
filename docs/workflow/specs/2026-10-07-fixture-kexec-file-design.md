# File-based kexec in pinned kernel fixtures

## Problem and scope

Issue #3101 and epic #2803 require the pinned fixtures to support RHEL-family kdump.
Scope comment6048281904 fixes the approved boundaries: upload-policy redesign is excluded;
native POWER qualification remains with #2818. The campaign authorizes x86_64 proofs.
The retained longterm x86 fixture enables KEXEC and CRASH_DUMP but leaves KEXEC_FILE unset.
The shared debug fragment never requests it, so olddefconfig preserves that disabled default.

## Design

Request KEXEC, KEXEC_FILE and CRASH_DUMP explicitly in `fixtures/kernel/debug.config`.
Reuse the builder's existing defconfig, fragment application, olddefconfig and `check_config`
sequence. Kconfig selects dependent capabilities; do not duplicate its dependency rules in
Python. The existing post-resolution check rejects a dropped requested option before compilation.
No ownership transition, new config format or upload-policy change is needed.
The RHEL runtime requirement remains UPLOAD_ADVISORY under ADR-0478 and ADR-0678.

Keep ADR-0693's existing manifest schema and identity calculation. Build fresh longterm and
stable x86 outputs, retaining their exact source, effective config, fragment, compiler/package,
ELF/build and artifact identities. Do not rewrite old manifests or relabel old proof as current.
Existing manifest verification remains an integrity check for the recorded inputs; old fixtures
can still verify but do not establish this new kdump capability. Inspect the effective config
and bind live evidence to the newly built fixtures before using them for this issue's proof.

A separate RHEL-only fragment would duplicate the pinned fixture path. Declaring these supported
x86 cells unsupported would evade the campaign goal. The shared fragment is the smaller extension
of the existing owner; no new architecture decision is required. Optional ADR0729 stays unused.

## Failure model

- Actors/deployments: operators building pinned Linux sources on supported native x86_64;
  the same shared fragment remains available to later native ppc64le builders.
- Invariants/assets: requested kexec/crash flags survive Kconfig; retained outputs and recorded
  config/build identities agree; old proof cannot qualify a rebuilt fixture.
- Accepted classes: mutable toolchain/package inputs can change fixture bytes; provenance records
  those inputs without promising bit reproducibility. A successful build is not crash-capture proof.
- Other owners: upload enforcement remains unchanged; native POWER proof belongs to #2818;
  unavailable installed-stack prerequisites are blocked evidence, never unsupported success.

No new trust boundary or kernel privilege policy is introduced: fixture configuration enables the
existing privileged syscall used by the supported kdump workflow, under existing guest controls.

## Success and validation

A focused fixture-policy regression fails on the old fragment and passes with the three requested
flags. Existing config-dependency and identity tests retain their error behavior.
Run real olddefconfig, full native builds and manifest verification for both pinned x86 baselines;
record effective flags and new identities. Run the supported RHEL-family kdump live workflow with
candidate-matched installed components when scheduled prerequisites are available: verify running
kernel identity, crash-kernel arming and actual capture result. Keep unavailable cells explicit.
Use owned bounded scratch, at most eight build jobs and one build at a time; preserve prior fixtures.
Run focused tests, lint, whole-tree types, relevant docs checks, then mandatory pre-push CI and
remote CI before any merge-ready handoff. Unit/config/build success alone cannot close the issue.
