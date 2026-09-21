# 0671 — Guard raw tar framing during external-boot scanning

## Status

Accepted (2026-09-21)

## Context

Issue #2571 follows #2569 and #2570. External-boot validation still decompresses the
archive twice: a raw bounding pass precedes tarfile's semantic scan. The raw pass
protects extension allocations and counts headers and padded bytes that the semantic
scan cannot see. Moving its checks into the tarfile loop would lose that protection.

## Decision

Interpose a bounded reader over one `gzip.GzipFile`, ahead of tarfile's uncompressed
stream mode. Reuse public `TarInfo.frombuf` for raw header parsing. Retain the former
preflight's header count, four extension types, and padded-byte accounting verbatim.
Release a header only after its guards pass. Drain this reader after successful semantic
iteration so no accepted archive can skip the raw walk. Stop at its first zero header,
as the old preflight did. Remove the separate preflight and obsolete discard helper if
it has no remaining callers. Leave logical checks and evidence construction intact.

## Consequences

One archive decompression feeds both checks and semantic inspection. State is bounded
by one header and a payload fragment; tarfile still owns GNU/PAX interpretation.
Raw and logical counters remain separate because extensions count only in the former.
Malformed-input error precedence may change; accepted evidence and limit accounting do
not. Python upgrades still require archive regression tests, but no private hook is used.
Historical POWER measurements are not measurements of this implementation.

## Considered & rejected

- **Bounded interposing raw-header parser (selected).** judgment: the smallest coherent
  ownership change reuses the existing public header parser and raw accounting while
  adding a reader state machine. It requires boundary, ordering, and equivalence tests.
- **CPython tarfile private-interface interposition.** verified: inspecting CPython
  3.14.7 with `inspect.getsource(tarfile.TarInfo._proc_pax)` and `_proc_gnulong` shows
  both allocate extension content via `_safe_read` before yielding a logical member.
  Overriding these private methods duplicates dispatch assumptions and still needs
  independent raw padded-byte/header accounting; judgment: unnecessary coupling.
- **Header-only gzip preflight that skips payload decompression.** verified: CPython
  3.14.7 `GzipFile.seek` delegates to the buffered decompressor; DEFLATE references
  earlier decoded history, so arbitrary member offsets cannot be skipped from an
  ordinary gzip stream without decoding intervening payload. No index exists in the
  uploaded format. judgment: retaining a second decoder cannot achieve the objective.
- **Keep the current preflight.** judgment: a safe fallback if the interposing reader
  cannot preserve the tested guards, but it retains the second decompression cost.
