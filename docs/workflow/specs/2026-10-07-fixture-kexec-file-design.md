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

## Observed capture prerequisite amendment

The first candidate-matched Rocky capture attempt failed at the existing upload-build
preflight: both pinned x86 configurations lack `CONFIG_FW_CFG_SYSFS`. Provisioning
completed, but install, kdump arming and capture were not reached. This is an existing
x86 `CRASH_CAPTURE` requirement, not a KEXEC_FILE Kconfig dependency. Issue #3101 retains
ownership of making its fixture usable by that required proof; upload policy is unchanged.

In `scripts/kernel_fixtures.py::build`, extend the default fragment with
`CONFIG_FW_CFG_SYSFS=y` only when the selected architecture is x86_64 and the selected
config is the repository default. Reuse the existing architecture argument and fragment
application/check sequence. Explicit custom config files retain their exact contents.
Do not add this option to the shared fragment: it is unavailable on supported little-endian
POWER. The existing manifest records the fully assembled `input.config` and its digest;
its schema and verification rules do not change. No new option, public interface or
architecture configuration framework is needed.

Test default x86 and ppc64le selection through the real builder logic with only external
commands stubbed, asserting the resulting input and effective config against the existing
architecture-aware CRASH_CAPTURE clauses. Verify custom config remains unchanged and a
Kconfig-dropped requested option is still rejected. Resolve actual pinned x86 configs and
check those same clauses before compilation; preserve the observed red and fresh green.
Rebuild both native baselines into fresh output directories and verify new identities,
then rerun the same strict-revision Rocky capture carrier. Reuse clean source trees;
the existing builder requires fresh outputs, so do not invent an incremental bypass.
Retain old outputs and their actual builder identity. Native POWER remains unproven.
The subsequent native RHEL capture reached installation but kdump arming failed.
The guest service journal identified missing `erofs` and `overlay` modules while
building its dracut `squash-erofs` initramfs. This is now observed prerequisite
failure, not an inferred KEXEC_FILE dependency. The operator approved the bounded
correction and one additional final review (three total, two already used).
Append `CONFIG_EROFS_FS=y` and `CONFIG_OVERLAY_FS=y` alongside fw_cfg only in the
same default-x86 branch. Preserve custom fragments and POWER defaults. Extend the
existing selection test and dropped-option cases; rebuild both fresh outputs and
rerun actual capture. Do not add unobserved SQUASHFS/XFS requirements or alter
upload policy. Preserve all earlier build identities and failed capture evidence.

The next rerun armed and booted the capture kernel, but its EROFS initramfs mount
failed with `algorithm 1 isn't enabled on this kernel`. The pinned longterm source
identifies algorithm 1 as LZMA and guards that decoder with `EROFS_FS_ZIP_LZMA`;
the observed dracut command uses `--squash-compressor lzma`. Include
`CONFIG_EROFS_FS_ZIP_LZMA=y` in the same default-x86 assembly to make the approved
EROFS support usable for that actual initramfs. Kconfig selects its XZ decoder
dependencies. Preserve the failed capture-kernel boot evidence and repeat both
fresh builds and capture before consuming the one remaining final review.
