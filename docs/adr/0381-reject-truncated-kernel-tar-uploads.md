# ADR-0381: Reject truncated / trailer-corrupt kernel-tar uploads at upload time (#1273)

- Status: Accepted
- Date: 2026-07-17

## Context

The external-build `kernel` artifact is a gzip tar of `boot/vmlinuz` plus a
`lib/modules/<release>/` tree (ADR-0234 §2). Its validator
(`_check_kernel_combined_tar` in `src/kdive/build_artifacts/validation.py`)
decompresses a bounded 128 MiB prefix — the gzip-bomb guard,
`_KERNEL_TAR_SCAN_MAX_BYTES` — and scans the tar for the two required members.
The rest of the validator catches the common malformations (wrong compression,
not-a-tar, missing/wrong-arch `boot/vmlinuz`, missing modules) with a categorized
`BUILD_FAILURE` at upload time. A silently truncated or trailer-corrupt archive,
however, slipped through with **no upload-time signal**, deferring the failure to
a far more expensive place — a boot that comes up missing modules (#1273).

Three blind spots let that happen:

1. **Sub-cap truncation was invisible.** `_decompress_bounded` returned the
   decompressed prefix and a `cap_reached` flag but did not distinguish a clean
   gzip EOF from a stream that simply ran out below the cap. A truncated `.tar.gz`
   (upload cut short, gzip trailer missing) produced a prefix with
   `decompressor.eof == False` and no error, and was accepted.
2. **A corrupt gzip trailer raised an uncaught error.** A gzip whose prefix
   inflates cleanly but whose CRC32/ISIZE trailer is wrong makes `zlib` raise
   `zlib.error` when it consumes the trailer. `_decompress_bounded` did not catch
   it, so it propagated as an uncategorized exception rather than a clean
   `BUILD_FAILURE`.
3. **The modules match was shallow.** The requirement was
   `path.startswith("lib/modules/")`, satisfied by a bare directory member or a
   `modules.dep` with zero `.ko` files — so a modules tree truncated after
   `modules.dep` but before any real module still passed.

## Decision

Close all three sub-cap blind spots and make an explicit, documented call on the
over-cap case.

1. **Distinguish clean EOF from a short stream, and categorize `zlib` failures.**
   `_decompress_bounded` returns `(data, cap_reached, gzip_complete)` where
   `gzip_complete = decompressor.eof`, and wraps any `zlib.error` as a
   `BUILD_FAILURE` ("kernel artifact gzip stream is corrupt"). The caller
   hard-fails when the stream ended below the cap without reaching the trailer
   (`not cap_reached and not gzip_complete`) — the truncated-tail case (#1273
   option 1, bounded by the existing guard). A complete (sub-cap) gzip has its
   CRC32/ISIZE trailer verified by `zlib`, so a corrupt trailer or any mid-stream
   bit-flip surfaces as a categorized `BUILD_FAILURE` rather than an
   uncategorized exception. This is the whole-stream integrity check #1273 asks
   for, bounded by the cap.

2. **Require a real kernel module.** A `lib/modules/` member counts only when it
   is a *file* under `lib/modules/<release>/` whose name ends in `.ko`, `.ko.xz`,
   `.ko.gz`, or `.ko.zst`. A bare directory or `modules.dep` no longer satisfies
   the requirement (#1273 option 2).

3. **Over-cap trailer verification is deliberately out of scope.** For an archive
   whose decompressed size exceeds the 128 MiB scan bound, the gzip trailer sits
   past the cap; reaching it means decompressing unbounded, which reintroduces the
   gzip-bomb DoS the cap exists to stop. When `cap_reached`, the validator does
   not attempt trailer verification and accepts the archive on the members already
   seen within the bound — issue #1273 option 3 (surface the cap, don't hard-fail
   on a tail you were never allowed to read). We do **not** add advisory plumbing
   for this case: there is no advisory return channel through `ValidatedUpload`,
   the case is narrow (required members within 128 MiB but a corrupt tail beyond
   it), and a one-off channel with no consumer violates the no-speculative-features
   rule. The residual gap is accepted and documented.

## Consequences

- Sub-cap archives — the overwhelming majority; a real kernel + modules tar is far
  under 128 MiB decompressed for most kernels — now get full gzip-integrity
  coverage at upload time. Truncation, a missing trailer, and a bad CRC/ISIZE all
  surface as a categorized `BUILD_FAILURE` with the same "catch it early" contract
  the rest of the validator already provides.
- A modules tree truncated after `modules.dep` but before any real `.ko` is now
  rejected, where before it passed the shallow prefix match.
- No schema change and no migration: the change is internal to validation. The
  `kernel` contract note (`EXTERNAL_BUILD_CONTRACTS`) and the external-build-upload
  resource doc are updated to state the real-module requirement so the advertised
  contract matches the enforced one.
- The residual over-cap gap (a >128 MiB archive that is truncated or trailer-corrupt
  only *beyond* the cap still passes) is an accepted, bounded limitation; the
  alternative is unbounded decompression.

## Considered & rejected

- **Verify the gzip trailer to EOF for every archive.** Rejected: it requires
  decompressing past the 128 MiB cap, reintroducing the gzip-bomb DoS the cap was
  added to stop. The cap and full-trailer verification are mutually exclusive for
  over-cap archives; the cap wins.
- **Add an advisory/warning return path for the over-cap case now.** Rejected:
  there is no advisory channel through `ValidatedUpload` and no consumer for one;
  building it for a narrow residual is speculative.
- **Keep the shallow `lib/modules/` prefix match, fix only the gzip side.**
  Rejected: it leaves the "directory member with no `.ko`" blind spot the issue
  explicitly calls out.
- **Lean on the manifest SHA-256 for integrity instead.** Rejected: the manifest
  check confirms the uploaded object matches the *declared* checksum. A client that
  truncated the archive at source and declared the matching (truncated) checksum
  passes it. This issue is structural completeness of the archive contents, which a
  whole-object checksum of a cleanly-truncated file cannot detect.
