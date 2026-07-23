# ADR 0433 — SysRq diagnostic capture and crash-watch for remote-libvirt Systems

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers

## Context

`control.diagnostic_sysrq` (ADR-0285) and `control.watch_for_crash` (ADR-0367) were
local-libvirt only. Not for the reason it looked: the SysRq *injection* path
(`domain.sendKey(VIR_KEYCODE_SET_LINUX, …)`, ADR-0285) is a plain libvirt domain call that is
transport-agnostic and already works unchanged over `qemu+tls://` — remote already drives the
sibling `injectNMI` over the same connection. What was local-only was the **capture**: both
tools read the console the guest just printed, and a remote System's console is not a
worker-local file. It is streamed out-of-band by the reconciler-resident collector into rotating
S3 parts (ADR-0235), which the boot worker cannot reach as a file.

Two blockers are now cleared. ADR-0427 (#1426) replaced the control-plane identity gates with
capability gates (`supports_diagnostic_sysrq`, `supports_crash_watch`), both fail-closed and
unset for remote. ADR-0429 (#1431) added the worker-side strict console read seam
`RemoteConsoleReader.read_window(conn, system_id, start_index) -> ConsoleWindowRead(data,
next_index, pumped)` over those S3 parts — deliberately distinct from the best-effort boot-window
`ConsoleSnapshotter`: it reports `pumped` (a `pg_locks` leader-liveness probe) so an
un-pumped/unreachable console is distinguishable from a genuinely silent one, and it propagates a
store read failure rather than swallowing it. This is part of the remote-libvirt parity epic
(#1423).

## Decision

Extend ADR-0285 to remote-libvirt (this ADR supersedes its "remote is a fail-closed stub"
clause; the local decision is otherwise unchanged), and remove `watch_for_crash`'s incidental
local-only status. Both tools become implementations over the existing ports, not architecture
changes.

- **Injection (remote `diagnostic_sysrq`).** Replace the fail-closed
  `RemoteLibvirtControl.diagnostic_sysrq` stub with the same `sendKey` injection local uses —
  `[KEY_LEFTALT, KEY_SYSRQ, KEY_<trigger>]` over the mutual-TLS `remote_connection` — with the
  keycode table duplicated per-provider (no shared layer, ADR-0076), exactly as power/injectNMI
  are. Unknown trigger → `configuration_error`; libvirt error → `control_failure`.
- **Console read-back (both handlers).** Both handlers read the console through a provider-gated
  source: a worker-local serial log for local-libvirt (unchanged), or the ADR-0429 read seam for
  remote-libvirt. The seam is exposed as a lazily-built `ConsoleCapabilities.reader_factory` on
  the provider runtime (built at job time so composition stays buildable without S3 config,
  ADR-0076), keeping the handlers provider-agnostic — no `jobs/handlers` import of a provider
  module.
- **Freshness contract, split by tool.** The two consumers read `pumped` differently, as the
  seam author specified:
  - **SysRq is one-shot.** Its whole output is the dump it just triggered, so a `pumped=False`
    read is fatal — returning the empty window would masquerade as "the kernel printed nothing".
    The remote read raises a `configuration_error` (`reason="console_not_pumped"`), satisfying
    the acceptance criterion that an un-pumped read is distinguishable from an empty console and
    never silently produces an empty artifact.
  - **Crash-watch is a poll loop.** An un-pumped read is not fatal; the loop returns the
    possibly-empty window and retries until the deadline, so a console that only starts being
    pumped mid-watch can still fire.
- **The allowlist and redaction are unchanged and provider-independent.** `SysRqCommand` stays a
  StrEnum with destructive keys structurally unexpressible; the read seam re-redacts at the seam
  (ADR-0429) and the handlers redact the returned/persisted bytes as before.
- **Capabilities.** Set `supports_diagnostic_sysrq=True` and `supports_crash_watch=True` on
  remote-libvirt's `ProviderSupport`, matching the wired ports. The two flagless tool-layer gates
  (ADR-0427) then admit a remote System; local behavior is unchanged.

## Consequences

- A remote System gains a non-destructive live diagnostic dump and a crash-watch verdict, at
  parity with local-libvirt. The agent-facing tool docstrings/`Field` now name both providers.
- The remote SysRq artifact is stored System-owned (`owner_kind='systems'`, `sysrq-diagnostic-*`)
  like local's, reclaimed by the teardown clause (a tenant-agnostic name-suffix match) and served
  by `artifacts.get`; the object store is the shared `object_store_from_env()` for both providers.
- No schema change, no migration (the capability flags and reader factory are composition state;
  the ports already exist). The two flags remain gate-only inputs, not yet surfaced on
  `systems.get` (ADR-0427).
- **Live proof is deferred** to the remote `live_vm` tier (#1424, ADR-0425), like the other
  #1423 provider-parity ADRs. The injection and read paths are unit-proven against injected
  libvirt/console fakes; a live remote SysRq capture and crash-watch fire are the remaining
  end-to-end proof.
