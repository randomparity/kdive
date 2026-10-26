# 0806 — The remote console collector seals a part when the console goes idle

## Status

Accepted (2026-10-26)

## Context

[ADR-0095](0095-reconciler-remote-console-collector.md) streams a remote System's console into
numbered S3 parts. [ADR-0235](0235-per-run-console-evidence.md) fixes the part and rotation
model, and [ADR-0433](0433-remote-libvirt-sysrq-and-crash-watch.md) reads those parts back for
`control.diagnostic_sysrq` and `control.watch_for_crash`. A part is sealed on one of two events: the
buffer reaches 64 KiB, or a crash marker appears. Each size rotation also holds back a 4 KiB
overlap so a secret split across the seam is redacted as one run.

A SysRq `show_memory` dump is about 2 KiB and has no crash marker. It stays in the collector's
memory until finalize, so the S3 parts the SysRq handler polls never grow and the job fails with
`no_console_output` (#3295). The local path tails a worker-local file and has no sealing step, which
is why it passes on the same guest.

## Decision

The collector seals whatever it holds once the console has been quiet for one second. The seal
uses the tail-flush path: the carry and the buffer are redacted together and uploaded as one part,
with nothing held back.

- Idle seals are at least five seconds apart. The first seal after a quiet spell is immediate;
  bytes that arrive within five seconds of the last idle seal wait in the buffer for the next.
- A seal happens at most once per idle episode. After it the buffer and carry are empty, so further
  idle pumps upload nothing, and a part is never empty.
- The diagnostic SysRq poll window grows from 10 to 20 reads of 0.5 s each, so it covers the seal
  interval, the idle interval, the pump backoff and the part upload.

## Consequences

- A small console burst becomes readable within about two seconds. This also shortens the delay
  for any consumer of the parts, including crash-watch on output without a crash marker.
- A secret split by a pause longer than one second is no longer held back across that boundary: its
  head is uploaded in one part and its tail in the next, each redacted on its own. The two
  places that join the parts redact the joined bytes again, so the split value is caught there:
  the read seam (ADR-0429), `finalize`, which now re-redacts the assembled console artifact, and
  the per-Run console snapshot, which now receives the secret registry and redacts the joined
  parts.
  The individual S3 parts can still hold the two halves, so ADR-0095's rule that no part carries
  a raw straddling secret holds for size rotation but not for an idle seal. That includes the
  observable per-part copies that agents can list.
- An interactive console produces more, smaller parts. No reader assumes a part's size: the read
  seam lists indices and assembles them, and the rotation threshold is read only by the collector.
- A failed capture takes up to ten seconds to report, not five.
- The seal runs only on a would-block read. Bytes buffered when the stream ends or errors stay
  buffered until the next read or `finalize`.
- A dump with an internal pause longer than about one second is sealed in two parts. The SysRq
  poll settles after two quiet reads (about one second), so it can return the first part only.
  A `show_memory` dump is emitted in milliseconds, so this is not expected in practice.
- A store failure during a seal keeps the buffered bytes and the part index, so the next idle pump
  uploads the same part and the indices stay consecutive. Size rotation clears its buffer before
  the upload and is unchanged.
- The idle clock restarts on every received byte. A console that prints more often than once a
  second never goes idle and seals only at the size threshold or a crash marker. A maximum age for
  buffered bytes would close that gap and is not built here.
- A console that prints a byte every few seconds produces at most one part per five seconds, about
  720 an hour, each with an `artifacts` row.
- While the store is down, a failed tail flush keeps at most one MiB of the newest bytes and drops
  older ones, so guest output cannot grow the buffer without limit.

## Considered & rejected

- **A flush request from the worker to the reconciler.** It seals exactly when needed, but it adds
  a cross-process signal and an interface for one consumer.
- **Adding `sysrq: ` to the crash marker.** It seals only the bytes buffered when the marker
  arrives. The lines that follow, including `Mem-Info:`, stay buffered.
- **Keeping the carry on an idle seal.** A dump under 4 KiB never exceeds the carry, so it would
  stay hidden.
