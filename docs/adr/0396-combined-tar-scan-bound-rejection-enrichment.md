# 0396 — Enrich the combined-tar scan-bound rejection with the bound and an arch-gated strip pointer

Status: Accepted

## Context

The `kernel` combined-tar validator (`_verify_combined_tar_shape`) decompresses at
most `_KERNEL_TAR_SCAN_MAX_BYTES` (128 MiB) before giving up — a gzip-bomb guard
(ADR-0234 §2, #1273). When `boot/vmlinuz` is large enough that the scan hits that
bound before any `lib/modules` member appears, the upload is rejected. The former
message —

> kernel combined tar boot/vmlinuz exceeds the scan bound before any lib/modules
> member; strip the boot image or list lib/modules earlier

— told the agent *that* the boot member overran the scan window but not *which*
bound was hit, and gave only an arch-agnostic "strip the boot image" hint.

In a black-box test an agent hit this on a ppc64le kernel (an unstripped `vmlinux`,
~500 MiB decompressed, carrying full DWARF). Lacking the arch-specific remedy —
which lived only in `docs/operating/external-build-upload.md` — it misdiagnosed the
failure as a member-ordering problem, reordered `lib/modules` to the front, and got
a **false pass**: the reorder moved the `lib/modules` header inside the scan window
without addressing the oversized boot member (#1339).

Two hard constraints bound the fix:

1. **No fabricated size.** `_decompress_bounded` only reports that the total 128 MiB
   cap was reached (`cap_reached`), never the boot member's own decompressed size —
   the scan stops at the cap. Decompressing past the cap to measure the member would
   defeat the gzip-bomb guard. So the message can honestly name the bound that was
   hit, but not a `{N} MiB` member size.
2. **The arch is not threaded to the failure site.** `validate_external_artifacts`
   has `arch` but passed only the derived `boot_format` down to the validator, so an
   arch-gated hint had no arch to gate on.

## Decision

**1. Enrich the message; do not change the pass/fail policy.** Message-enrichment
only. The rejection now states the bound that was hit — read from
`_KERNEL_TAR_SCAN_MAX_BYTES` so it cannot drift from the constant — and explicitly
that the boot member's full decompressed size is *not* measured (the scan stops at
the bound), rather than inventing a size:

> kernel combined tar boot/vmlinuz exceeds the 128 MiB scan bound before any
> lib/modules member (the scan stops at the bound, so the boot member's full
> decompressed size is not measured); strip the boot image or list lib/modules
> earlier

Deliberately out of scope: making a genuinely oversized `boot/vmlinuz` fail even
when `lib/modules` is listed first. That is a behavior-policy change (an agent could
still reorder to pass a truly oversized member); this issue is diagnosis clarity, so
the scan-order semantics are unchanged.

**2. Append an arch-gated ppc64le strip pointer.** For `ppc64le` only, the message
appends the concrete remedy with a doc pointer:

> (ppc64le: strip the build-tree vmlinux before packaging - see
> docs/operating/external-build-upload.md)

powerpc has no bzImage, so its boot member is the ELF `vmlinux`, and an unstripped
`vmlinux` carries full DWARF (hundreds of MB) — the exact shape that overruns the
scan window. x86_64's bzImage is already stripped/compressed, so the generic hint
suffices and the ppc64le pointer would misdirect; the hint is therefore gated on the
declared build arch.

**3. Thread `arch` to the failure site.** `arch` is plumbed from
`validate_external_artifacts` through `_validate_one_artifact` →
`_check_artifact_content` → `_check_kernel_combined_tar` → `_verify_combined_tar_shape`
alongside the existing `boot_format`. `boot_format` becomes keyword-only on the two
generic per-artifact functions so adding `arch` keeps positional parameters ≤5.

## Consequences

- The rejection names the bound that was hit and, for ppc64le, points at the strip
  remedy, so an agent hits the root cause instead of reordering into a false pass.
- No fabricated member size and no weakening of the gzip-bomb guard — the honest
  "size not measured" wording is the deliberate trade for keeping the scan bounded.
- The scan-order pass/fail semantics are unchanged; a truly oversized member listed
  after `lib/modules` still passes (documented residual, same class as ADR-0381's
  over-cap trailer residual).
- Validation-message and docs only; no schema change, no migration, no new tool.

Rejected: measuring the boot member's decompressed size (defeats the gzip-bomb
guard); a static arch-agnostic hint (the false-pass this fixes); an unconditional
ppc64le hint on every arch (misdirects x86_64); and rejecting an oversized boot
member regardless of member order (a behavior-policy change out of scope for a
diagnosis-clarity fix).
