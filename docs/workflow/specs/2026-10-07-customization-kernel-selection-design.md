# Customization-kernel selection for the Fedora regression image

Superseded by [the retained-kernel design](2026-10-08-fedora-retained-kernel-design.md).

## Authority and problem

**Approved (2026-10-07).** The operator approved the interface and value; scope6049320869
authorizes implementation under issue #3065. The reviewed directory fix at `01a5888a` remains authorized and unchanged.
Native POWER qualification belongs to #2818; regression catalog removal remains excluded.
The prior live build reached offline injection, then failed before customization boot because
its untouched virt-builder template contains two non-rescue kernels and the caller passes no hint.
The existing exact selector reproduces that failure and accepts either full filename or version.

## Interface and ownership

Add optional `customization_kernel: str | None = None` to the build-catalog dataclass in
`kdive.images.rootfs.catalog`, parsed from an image row's `customization_kernel` key.
Omission means `None`. A present value must be a nonempty string; wrong types and empty strings
raise the existing catalog `configuration_error` naming that field before acquisition.
Do not normalize, sort versions or interpret the value as a host path or shell command.

Pass the resolved entry's value through `_boot_customize` and `_run_boot` to the existing
`extract_baseline_kernel` hint argument. Reuse its exact candidate match, matching-initrd selection,
rescue exclusion and stale-hint failure. An omitted hint still rejects multiple candidates;
a supplied stale hint fails even for a single-kernel base. No fallback to another kernel.
This field controls only the transient customization boot. No CLI, RootfsBuildSpec, MCP profile,
component-catalog schema or provenance schema field is added. Existing caller defaults remain.

Set only `fedora-kdive-ready-43` to `customization_kernel = "6.18.5-200.fc43.x86_64"`.
Read-only inspection of the retained upstream template found this exact version in grubenv's
saved entry and its referenced BLS linux/initrd lines; both matching files exist in `/boot`.
That evidence justifies the proposed constant; production code will not parse bootloader defaults.
A changed upstream template missing that kernel fails closed and requires an explicit catalog
refresh with fresh build evidence. See [ADR0760](../../adr/0760-customization-kernel-selection.md).

The shared builder remains the sole customization owner; there is no caller migration or alternate
selection pipeline. Keep the finished image's existing kernel-count/config discovery semantics.
The customization hint neither deletes kernels nor supplies a provisioning hint. A multi-kernel
finished image still uses the existing explicit `LibvirtProfile.baseline_kernel` for ready proof,
chosen from the inspected finished image; this proposal does not claim unhinted provisionability.

## Failure model and boundaries

- Actors: operator-maintained catalog and acquired guest template, processed by the existing host
  builder; mutable vendor package repositories can change the finished image.
- Assets/invariants: exact explicit selection, unchanged absent-hint refusal, and preserved original
  cloud-init fix. A selected kernel and optional matching initrd remain existing extractor outputs.
- Accepted classes: stale template values fail closed; successful selection alone proves neither
  package customization nor guest readiness. Multiple final kernels remain supported via the
  existing provisioning hint, without changing discovery or advisory config capture.
- Other owners: native POWER #2818 and excluded catalog removal; new independent live failures
  must be routed through existing owners rather than silently adding a bootloader framework.

Catalog input crosses into guest executable selection: validate type/nonempty at load, then exact
match against existing non-rescue guest candidates. Reuse extractor confinement, error category,
and optional-initrd behavior. No new shell execution, host path dereference or authority grant.
Diagnostics follow the existing selector; no private template identifiers enter committed prose.

## Verification after approval

Tests must distinguish omission, accepted value, empty/wrong type, and forwarding through the
builder. Use the actual selector with a two-kernel fixture to prove the selected kernel/initrd;
stale and rescue-only selections must stop before customization boot. Preserve omitted single-
kernel behavior and the original cloud-init directory regression. No source implementation now.
Rebuild the real Fedora43 catalog row and record the candidate and template/selection identities.
Rerun `tests/integration/test_image_smoke_live.py::test_image_smoke[fedora-kdive-ready-43]`
against the candidate-matched stack. Its existing catalog profile has no provisioning kernel hint;
if the finished image still has multiple kernels, retain that required cell's actual failure.
An unavailable stack remains blocked evidence. Route unresolved prerequisites through existing
ownership; this proposal does not authorize a harness/profile change or shrinking qualification.
A separate explicitly hinted guest can diagnose authenticated readiness and cleanup through the
supported provisioning route, but cannot substitute for the required image-smoke cell.
