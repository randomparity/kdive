# INITRD component-source design

## Goal

Issue #1436 adds INITRD as the first caller-supplied non-ROOTFS provisioning component while
recording remote-libvirt's in-guest initramfs generation as a deliberate rejection. The governing
decision is [ADR-0583](../../adr/0583-provider-aware-initrd-component-input.md).

## Scope and constraints

The input is an optional `ProvisioningProfile.initrd: ComponentRef`. Its Pydantic `Field`
description is the agent-facing contract: it states the discriminated reference shape, that
local-libvirt and fault-inject accept only a worker-host `local` path, and that remote-libvirt
rejects every supplied INITRD because it generates one in-guest. Existing profiles remain valid.
`component-upload`, `artifact`, and `catalog` remain outside this change. Target architectures
remain x86_64 and ppc64le on Python 3.14.

The entry point, admission enforcement call, provider declarations, and local consumption land
together. Remote rejection happens before job creation and uses `ErrorCategory.CONFIGURATION_ERROR`
with `provider`, `component_kind`, `source_kind`, and `accepted_source_kinds` details.

## Data flow

Pydantic parses the discriminated component reference and retains it in the serialized profile.
`validate_profile_for_provider` validates ROOTFS as today, then validates a present INITRD against
the bound runtime's component-source capabilities. Remote therefore rejects immediately.

For local-libvirt, provisioning resolves the `local` path through the existing allowed-root and
checksum rules used for local component material. It copies the file into the temporary baseline
directory before that directory's atomic rename. The supplied file replaces an extracted rootfs
initramfs; domain XML then points at the stable baseline `initrd`. An already materialized baseline
is reused unchanged on retry.

Fault-inject accepts the reference to exercise the provider-neutral contract but intentionally has
no materialization side effect. Remote provisioning never sees an accepted reference and keeps its
in-guest `dracut` plus `grubby --copy-default` path unchanged.

The remote provider's component-source comment records that rationale beside its empty INITRD
declaration. Epic #1423's Non-goals records the same rejected outcome. The provider parity table
gains the issue-required provider-difference waiver for `support.component_sources.initrd:local`,
with the permanent in-guest dracut rationale. The separate AST guard receives no enforcement
waiver: it proves every declared INITRD map is backed by the shared enforcement call.

## Error handling and safety

Structural reference errors remain scrubbed provisioning-profile errors. Unsupported sources use
the existing component-validation envelope. Local path resolution must reject paths outside the
configured roots, missing or non-regular files, and checksum mismatches before domain definition;
failure cleanup follows the baseline directory's existing atomic/reclaim contract.

The new trust boundary is an authenticated tenant selecting a worker-host path. It widens the
existing local-component boundary only to INITRD. Existing absolute-path parsing, provider
allowlisted roots, regular-file checks, and optional SHA-256 verification control it. The failure
response exposes the source kind and accepted kinds, never file contents. Operator control of the
allowlisted roots is trusted. Symlink/path-race hardening beyond the existing local component
contract and all upload source kinds are out of scope.

## Verification

Tests prove schema round-trip; generated-schema text naming the reference and provider matrix;
remote admission rejection with the exact error details; local and fault-inject acceptance; the
specific cross-provider INITRD waiver with no AST-enforcement waiver; local replacement of the extracted initrd;
reuse stability; path/checksum failures; and unchanged behavior when the field is absent. A source
assertion checks the remote component-source comment for the in-guest dracut reason, and the
quest verifies epic #1423's edited Non-goal by GitHub readback. Focused tests run first; `just
lint`, `just type`, and `just ci` are the branch guardrails. Live remote proof is unnecessary
because the remote path is rejected before provider I/O; the local materialization behavior is
covered at its injected filesystem boundary.

## Durable workflow context

Branch: `feat/initrd-component-source-1436`. Base branch: `main`. Scope token:
`q1436-956f81c1`. Host architecture: x86_64. Targets: x86_64 and ppc64le. Architecture
relationship: included.
