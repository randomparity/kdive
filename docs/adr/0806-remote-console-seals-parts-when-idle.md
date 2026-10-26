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

- A seal happens at most once per idle episode. After it the buffer and carry are empty, so further
  idle pumps upload nothing, and a part is never empty.
- The diagnostic SysRq poll window grows from 10 to 20 reads of 0.5 s each, so it covers the idle
  interval, the pump backoff and the part upload.

## Consequences

- A small console burst becomes readable within about two seconds. This also shortens the delay
  for any consumer of the parts, including crash-watch on output without a crash marker.
- A secret split by a pause longer than one second is no longer held back across that boundary: its
  head is uploaded in one part and its tail in the next, each redacted on its own. A registered
  secret is matched whole, so such a split value can survive. Secrets in console output are
  already treated as guest-controlled and best effort; size rotation keeps its overlap.
- An interactive console produces more, smaller parts. No reader assumes a part's size: the read
  seam lists indices and assembles them, and the rotation threshold is read only by the collector.
- A failed capture takes up to ten seconds to report, not five.

## Considered & rejected

- **A flush request from the worker to the reconciler.** It seals exactly when needed, but it adds
  a cross-process signal and an interface for one consumer.
- **Adding `sysrq: ` to the crash marker.** It seals only the bytes buffered when the marker
  arrives. The lines that follow, including `Mem-Info:`, stay buffered.
- **Keeping the carry on an idle seal.** A dump under 4 KiB never exceeds the carry, so it would
  stay hidden.
