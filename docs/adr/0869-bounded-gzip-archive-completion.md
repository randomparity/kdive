# 0869 — Bounded gzip completion after tar inspection

## Status

Proposed

On implementation, supersedes only the over-prefix-cap trailer-verification
residual in [ADR-0381](0381-reject-truncated-kernel-tar-uploads.md).

## Context

The prefix check stops after 128 MiB by design. The later external-boot scanner
already walks the complete tar under separate member and raw-output limits.
Its guarded reader stops at tar EOF, so its apparent final drain never reaches
the underlying gzip trailer. Actual scanner probes accept a missing trailer and
corrupt CRC; public validation does too when the prefix cap is reached. Small
public uploads still benefit from ADR-0381's prefix integrity check.

## Decision

After successful tar processing, finish the existing gzip decoder to EOF under
the existing aggregate raw archive byte limit. The guarded reader owns framing
accounting and bounded completion. Charge decoded trailing bytes and terminators,
read at most the decoder quantum or remaining allowance plus one detection byte,
and reject over-budget output without an unlimited read. Keep logical manifest
sizes unchanged. Missing or corrupt gzip framing, including late `zlib.error`,
uses the existing build-failure category; earlier tar failures retain precedence.

## Consequences

The bounded whole-archive scan can now reject malformed streams beyond the
prefix window. Valid concatenated gzip streams and padding remain accepted within
the same raw limit. Finalization and materialization share the corrected scanner.
No advisory channel, new format, dependency, schema or persisted counter is added.
Extra work is limited to the remaining decoded tail, with bounded read buffers.

## Considered & rejected

- **Keep the apparent guarded drain or rely on the prefix check.** verified:
  at source19a9f4481 on Linux/Python3.14, the actual scanner accepts both missing
  trailer and CRC corruption; public validation with an8192byte test prefix cap
  accepts both too. Its valid control passes; the default small-upload arm rejects.
- **Drain without a total output budget.** judgment: that would remove the
  existing defense against highly expanding trailing compressed data.
- **Reject concatenated gzip streams or nonzero decoded tails outright.** judgment:
  completion does not require a new archive-content compatibility policy.
