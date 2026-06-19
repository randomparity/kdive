# Remote console capture: would-block vs end-of-stream

- **Status:** Draft
- **Issue:** [#594](https://github.com/randomparity/kdive/issues/594)
- **ADR:** [ADR-0182](../adr/0182-remote-console-would-block-vs-eof.md)
- **Refines:** [ADR-0095](../adr/0095-reconciler-remote-console-collector.md)

## Problem

Every remote-libvirt System's `…/console` artifact is 0 bytes and no `console-parts-<n>` are ever
written (#594, verified live across every System over multiple days). Console hosting is active and
opens a collector per running System, but the collector captures nothing — including across the boot
window where #587's emergency-mode boot would print.

Two coupled causes, both established by a **live experiment** against a real booting domain over the
production `qemu+tls` path:

1. **No event loop drives the non-blocking stream.** `open_remote_console` opens a
   `VIR_STREAM_NONBLOCK` stream and the collector polls `stream.recv` from a worker thread. A libvirt
   non-blocking stream's incoming buffer is filled by libvirt's event loop
   (`virEventRegisterDefaultImpl` + a thread running `virEventRunDefaultImpl`); the codebase registers
   none, so `recv` returns `-2` (would-block) forever. Experiment, ~8–10 s of live console each:

   | read mode | event loop | bytes |
   |---|---|---|
   | non-blocking poll | none | **0** |
   | non-blocking poll | registered + run-thread | **~21 KB** |
   | blocking `recv` | none | **~20 KB** |

2. **Overloaded empty-read sentinel.** `_RemoteConsoleStream.recv` mapped `-2` to `b""`, and
   `ConsoleCollector.pump_once` drops the stream on **any** empty read (`if not chunk:
   self._drop_stream()`), reopening next pump with `VIR_DOMAIN_CONSOLE_FORCE`. Even with the event
   loop, the would-block reads between data bursts (experiment: 35 would-block among 15 data reads)
   would thrash open → drop → reopen.

## Approach

Apply both fixes — the experiment showed neither alone captures (sentinel-only with no event loop
stays at 0 bytes).

**A. Register + run the libvirt event loop in the reconciler.** A new idempotent
`ensure_libvirt_event_loop()` helper calls `virEventRegisterDefaultImpl()` once and starts a daemon
thread running `virEventRunDefaultImpl()`. Libvirt's standard whole-process setup; does not change
synchronous API behavior. Two constraints the implementation must honor, because getting either
wrong silently reverts to 0-byte consoles (the exact bug being fixed):

- **Ordering.** `virEventRegisterDefaultImpl()` services only connections opened *after* it
  registers. The helper therefore runs as the **first action** in the reconciler entrypoint, before
  provider composition, discovery, or any libvirt connection is opened. (Console hosting opens its own
  connections lazily in `open_remote_console` during the hosting loop — well after startup — so
  registration at process start is comfortably ahead of them; registering first also covers any
  connection other startup paths open.)
- **Run-thread durability.** The thread wraps `virEventRunDefaultImpl()` so a transient error is
  logged and retried with a short back-off rather than killing the thread or busy-spinning; it logs
  once at startup that the libvirt event loop is running, and logs a warning if the loop exits
  unexpectedly — so a dead loop (which would silently stop all capture) is observable, not invisible.

**B. Split the empty-read sentinel** so the pump backs off on would-block instead of dropping:

| `recv` result | Meaning | Pump action |
|---|---|---|
| `None` | would-block, no data this read | keep the stream, back off, return `False` |
| `b""` | clean end-of-stream (console closed) | drop the stream, reconnect next pump, return `False` |
| non-empty `bytes` | console data | buffer + rotate, return `True` |
| raises | dropped/errored stream | (existing `except`) drop + reconnect |

Changes:

1. `ensure_libvirt_event_loop()` (new) — register default impl + start the run-thread once; called
   from the reconciler entrypoint.
2. `_RemoteConsoleStream.recv` (`wiring.py`) returns `None` for the `-2` would-block instead of `b""`;
   keeps raising for `-1`/`None`; returns the bytes otherwise.
3. `ConsoleStream.recv` protocol (`collector.py`) return type widens to `bytes | None`, `None`
   documented as would-block.
4. `ConsoleCollector.pump_once` (`collector.py`) checks `chunk is None` first (no data, keep stream,
   `return False`) and only treats a genuine `b""` as end-of-stream (`_drop_stream()`).

The stream stays non-blocking; `VIR_DOMAIN_CONSOLE_FORCE` on reconnect is unchanged but now reached
only on a real drop/EOF.

## Test plan (TDD, unit, no libvirt host)

- **Collector:** a fake non-blocking stream whose `recv` interleaves `None` (would-block) with data
  and a trailing `b""`. Assert: a `None` read returns `False` and does **not** close/reopen the
  stream (`opens == 1`, `closed is False`); subsequent data is captured on the **same** stream; the
  finalized artifact equals the concatenated data. This test **fails before** the fix (the `None`
  read drops the stream).
- **Preserve EOF behavior:** existing `test_empty_console_bytes_finalize_to_empty_artifact` and
  `test_reconnect_on_stream_drop` continue to pass — `b""` still drops, an error still reconnects.
- **Stream wrapper:** drive `_RemoteConsoleStream.recv` directly against a fake libvirt stream:
  `-2 → None`, `-1 → raises`, `None → raises`, non-empty `bytes → bytes`, `b"" → b""`.
- **Event-loop helper:** inject fake `register`/`run` seams into `ensure_libvirt_event_loop()`; assert
  the default impl is registered exactly once and the run-thread started once across repeated calls
  (idempotent), and that a second call is a no-op.

These unit tests verify collector/stream/helper **behavior** — not that the real libvirt stream
delivers bytes. That property is proven only by the live experiment above and the live criterion
below; it is not exercised in CI.

## Success criteria

- A would-block read keeps the stream open and captures later data (unit, above).
- `b""` end-of-stream and error-drop reconnect behaviors are unchanged (unit).
- `ensure_libvirt_event_loop()` registers + runs the loop exactly once, idempotently (unit).
- **Required live gate to close #594** (under the `live_vm`/operator gate, not CI): a remote System's
  `…/console` artifact is non-empty after a build→install→boot. (Already demonstrated for the fix
  shape by the experiment in "Problem": the event loop turns 0 bytes into ~21 KB on a real domain.)

## Out of scope

- The teardown-time `Domain not found` pump errors (the domain is destroyed while the System is
  briefly still listed running). Benign noise; not the capture bug.
- Local-libvirt console handling (independent code; not affected).
