# 0704 — Bound guest regular-file transfer while copying

## Status

Accepted (2026-09-27)

## Context

Issue #2853 requires a host storage ceiling during guest regular-file reads.
The previous session downloaded the complete file before checking its size.
The concrete seam serves module capture and guest SELinux policy reads.

## Decision

Read through libguestfs `pread` in chunks of at most 1 MiB, writing no more than
`expected_size` bytes. Accept short nonempty reads; reject premature EOF or an
oversized response. A one-byte EOF probe detects excess content without storing it.
Keep the final flushed-size equality check and context-managed temporary-file cleanup.
Use the same ValueError size-change failure for overrun and truncation.

## Consequences

Host storage is bounded by the declared size. Each requested data payload is at most 1 MiB;
transfer memory is independent of total file size, including binding-owned copies.
The smaller chunk stays below libguestfs's documented 2–4 MiB protocol message limit.
Large files require more RPC calls. This does not promise a coherent snapshot of a file
concurrently modified without changing its length. No caller or archive format changes.

## Considered & rejected

- **Unbounded download then compare.** verified: the controlled Python fixture at base
  449eaf33c stored 1 MiB against a declared three bytes before ValueError.
- **download_offset plus a separate probe.** judgment: offset-limited bulk download is
  viable, but chunked reads keep the stored-byte ceiling directly enforced by this owner
  and make short-read/error behavior explicit without relying on host output semantics.
- **Pipe download with cancellation.** judgment: adds threads and cancellation ownership
  when the existing bounded read API meets the criterion.
