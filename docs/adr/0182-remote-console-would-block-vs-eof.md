# ADR 0182 — Drive a libvirt event loop (and split would-block from EOF) for remote console capture

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers
- **Refines:** [ADR-0095](0095-reconciler-remote-console-collector.md) (reconciler remote console collector)

## Context

Remote-libvirt console capture (ADR-0095) never persists any bytes: every remote System's assembled
`…/console` artifact is 0 bytes and no `console-parts-<n>` are ever written, across every System in
the live demo deployment over multiple days (#594). Console hosting is active — the reconciler-leader
acquires leadership and opens a `ConsoleCollector` per running System — yet nothing is captured,
including across the boot window where a failure like #587's emergency-mode boot would print.

There are two coupled causes, both established by a live experiment against a real booting domain on
the remote `qemu+tls` host (over the exact production connection path):

**Cause 1 — the non-blocking stream has nothing pumping its buffer.** `open_remote_console` opens a
**non-blocking** (`VIR_STREAM_NONBLOCK`) console stream and the collector reads it by polling
`stream.recv` from a worker thread. For a libvirt non-blocking stream, the incoming buffer is filled
by libvirt's **event loop** (`virEventRegisterDefaultImpl` + a thread running
`virEventRunDefaultImpl`); a bare `recv` poll with no event loop registered returns `-2` (would-block)
forever because nothing drives the connection's incoming RPC I/O. The codebase registers **no**
libvirt event loop anywhere. The live experiment confirmed this directly against a booting domain:

| Read mode | libvirt event loop | bytes captured over ~8–10 s of live console |
|-----------|--------------------|----------------------------------------------|
| non-blocking poll | none registered | **0** (every read would-block) |
| non-blocking poll | registered + run-thread | **~21 KB** |
| blocking `recv` | none registered | **~20 KB** |

So with no event loop the non-blocking poll captures nothing, which is exactly the 0-byte production
behavior.

**Cause 2 — an overloaded empty-read sentinel.** `_RemoteConsoleStream.recv` mapped the `-2`
would-block to `b""`, and `ConsoleCollector.pump_once` treats **any** empty read as a clean
end-of-stream and drops the stream (`if not chunk: self._drop_stream()`), reopening it next pump with
`VIR_DOMAIN_CONSOLE_FORCE`. So even with the event loop pumping, the interspersed would-block reads
(the experiment saw 35 would-block among 15 data reads in one window) would thrash open → drop →
reopen. `b""` was made to mean two things — "no data yet" and "the stream ended" — and only the second
was handled.

The bug is remote-specific because this collector and stream are remote-only
(`providers/remote_libvirt/console/`); the local provider does not share them.

## Decision

Apply both fixes the experiment showed are jointly required.

**1. Register and run the libvirt event loop in the reconciler.** The reconciler process (which hosts
console capture) calls `virEventRegisterDefaultImpl()` once at startup, **before** any libvirt
connection is opened, and runs `virEventRunDefaultImpl()` in a dedicated daemon thread for the
process lifetime. This fills non-blocking stream buffers so `recv` polling actually yields console
data (experiment: 0 → ~21 KB). Registration is idempotent and guarded so a second call is a no-op;
the run-thread is started once. The run-thread wraps `virEventRunDefaultImpl()` so a transient error
is logged and retried with a short back-off rather than killing the thread or busy-spinning, and logs
at startup / on unexpected exit — a dead loop would silently stop all capture, so it must be
observable. Synchronous libvirt calls (the reaper, transport resetter, build-VM reaper) are
unaffected — registering the default event loop is libvirt's standard whole-process setup and does
not change synchronous API behavior.

**2. Split the overloaded empty-read sentinel** across the `ConsoleStream` contract so the pump backs
off on the would-block reads that occur between data bursts instead of dropping the stream:

- **`recv` returns `None`** — would-block, no data this read. Keep the stream open, back off. No drop.
- **`recv` returns `b""`** — clean end-of-stream (console closed, e.g. power-off). Drop and reconnect.
- **`recv` returns non-empty `bytes`** — console data; buffer and rotate as before.
- **`recv` raises** — a dropped/errored stream; the pump's existing `except` drops and reconnects.

Concretely: `_RemoteConsoleStream.recv` returns `None` for the libvirt `-2` would-block (instead of
`b""`), keeps raising for `-1`/`None`, and returns the bytes (which may be `b""` at a genuine close)
otherwise. `ConsoleCollector.pump_once` checks `chunk is None` first (no data, keep the stream, return
`False`) and only treats a real `b""` as end-of-stream (drop, return `False`). The `ConsoleStream`
protocol's `recv` return type widens to `bytes | None`, with `None` documented as would-block.

The stream stays non-blocking (`VIR_STREAM_NONBLOCK`); `VIR_DOMAIN_CONSOLE_FORCE` on reconnect is
unchanged but is now reached only on a genuine drop/EOF, not on every idle read, so it no longer
thrashes.

## Consequences

- Remote console capture works: the event loop fills the non-blocking stream's buffer and the pump
  keeps the stream open across would-block reads, so console bytes — including a
  boot/emergency-mode/panic sequence — accumulate and finalize into a non-empty `…/console` artifact.
  ADR-0095 parity is met for remote-libvirt.
- Boot failures such as #587 become diagnosable from kdive artifacts (the failing journal is
  captured) rather than requiring host-side `virsh console`.
- The reconciler now runs one dedicated daemon thread for `virEventRunDefaultImpl()` for the process
  lifetime, alongside its asyncio loop. The two are independent; the libvirt thread only services
  libvirt's poll-based callbacks.
- `b""` keeps its end-of-stream meaning, so the existing reconnect-on-EOF and
  empty-console-finalizes-empty behaviors are preserved; only the *would-block* case changes.
- The `ConsoleStream.recv` contract now returns `bytes | None`. The collector is the only consumer;
  that change is internal to the remote-libvirt console module. No schema, migration, or tool-surface
  change.
- The teardown-time `Domain not found` pump errors (the domain is destroyed while the System is still
  briefly listed running) are unrelated benign noise and are out of scope here.

## Considered & rejected

- **Only split the sentinel; keep the non-blocking poll without an event loop.** Rejected — proven
  inert by the experiment: with no event loop the non-blocking `recv` returns would-block forever and
  captures 0 bytes, so the sentinel split alone (the original form of this ADR) would stop the thrash
  but still persist empty consoles. The event loop is the load-bearing half.
- **Switch the console stream to blocking `recv` (no event loop).** Rejected even though the
  experiment showed it captures (~20 KB): a blocking `recv` pins its worker thread until data, EOF, or
  `abort()`, so `pump_once` could no longer hold the collector lock across the read (it would deadlock
  `close()`/`finalize()`, which take the same lock) — forcing a lock restructuring — and `close()`
  would have to interrupt a blocked `recv` cross-thread via `stream.abort()`, which the experiment
  showed propagates but left the worker still mid-unwind at the deadline (libvirt streams are not
  documented thread-safe for concurrent `recv`/`abort`). The event-loop + non-blocking path keeps the
  collector's existing prompt-return, lock-during-pump design intact and is libvirt-idiomatic.
- **Treat `b""` as would-block and signal EOF another way.** Rejected: it inverts the established
  `b""`=EOF meaning the collector and its tests rely on; adding `None` for would-block is smaller.
- **Drop on would-block but cache the stream to cheapen reopen.** Rejected: dropping on "no data yet"
  is wrong in principle (the stream is healthy), and caching reintroduces the open/close lifecycle
  questions the explicit signal + event loop avoid.
