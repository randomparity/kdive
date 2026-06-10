# Dead-worker gdbstub reconciler reset (issue #216)

- **Status:** Draft
- **Date:** 2026-06-09
- **Issue:** [#216](https://github.com/randomparity/kdive/issues/216)
- **ADR:** [ADR-0086](../../adr/0086-dead-worker-gdbstub-reconciler-reset.md)
- **Milestone:** M2 — Remote libvirt
- **Follow-up to:** #205 (PR #214, ADR-0083); sibling of #215 (ADR-0085)

## Problem

QEMU's gdbstub is **single-client**: it accepts one TCP connection at a time. ADR-0079
(§Consequences, "Single-client gdbstub contention must be reconciled, not just detected")
named the failure mode this spec closes:

> a stale TCP connection from a dead worker can hold it and block re-attach … The
> DebugSession reconciler must **reset the dead-worker transport**, not merely mark the row
> `detached`, or the next attach contends with a ghost — surfacing as `transport_conflict`.

ADR-0083 (§Consequences) recorded the interim limitation #205 shipped with:

> a worker that dies mid-debug can leave its stale TCP connection holding the System's
> single-client gdbstub, and the next attach fails (`transport_conflict` /
> `debug_attach_failure`) until the System is torn down and reprovisioned. `close_transport`
> is a no-op (connectionless RSP) and the holding connection belongs to the dead worker, so
> the provider cannot break it without the core reconciler reset.

Today `reconciler/loop.py::_repair_dead_sessions` flips a stale `live` DebugSession to
`detached` and stops there. The gdbstub port stays held by the dead worker's lingering
connection. The next `debug.start_session` attach to that System fails with
`transport_conflict` until the System is torn down — a developer-visible wedge with no
automated recovery.

## Why this is a core change, not provider work

The reconciler (`src/kdive/reconciler/`) is provider-agnostic core behind the ADR-0076
portability gate. Freeing the gdbstub port is provider-specific (it re-arms QEMU's gdbstub
over the remote host's `qemu+tls` monitor), so the reconciler cannot do it directly without
importing a provider — which would breach the gate's dependency direction.

The codebase already solves this exact shape for leaked-domain repair: the reconciler
consumes a narrow injected **port** (`InfraReaper` Protocol, `providers/reaping.py`) with a
`NullReaper` default, and `providers/composition.py` wires the concrete provider reaper. This
spec adds the second instance of that pattern. Because the reconciler's consumption of the
new port lives in `reconciler/loop.py` (a gated core file), the change carries its own ADR
(ADR-0086) and an explicit `scripts/m2_portability_gate.py` `ALLOWED_FILES` extension in the
same PR — exactly as the issue requires.

## Design

### 1. `TransportResetter` — the new reconciler→provider port

A narrow Protocol under `src/kdive/providers/transport_reset.py` (a `providers/` module, **not**
a gated core prefix), mirroring `providers/reaping.py`:

```python
@runtime_checkable
class TransportResetter(Protocol):
    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None: ...

class NullResetter:
    async def reset(self, *, transport, transport_handle, domain_name) -> None:
        return None
```

The reconciler imports only this Protocol + `NullResetter`. The default is `NullResetter`
(local-libvirt needs no active reset — its gdbstub is co-located, so a dead worker's socket is
torn down by the host OS; the contention ADR-0079 reconciles is the *remote* half-open-TCP
case).

### 2. The remote resetter self-selects; the reconciler routes nothing

`RemoteLibvirtTransportResetter` (under `providers/remote_libvirt/`, not gated) realizes the
port. The reconciler passes only **core-available** data per dead session — `transport`,
`transport_handle`, `domain_name` — and the resetter decides whether the session is its
concern, so the reconciler never learns a provider identity:

- `transport != "gdbstub"` → no-op (`drgn-live` is connectionless, ADR-0083 §4; `ssh` carries
  no gdbstub).
- `transport_handle is None`, or it decodes to a non-`gdbstub` scheme → no-op.
- the decoded handle host **≠** the operator-configured `RemoteLibvirtConfig.gdb_addr` → no-op
  (a local-libvirt loopback gdbstub session is not the remote resetter's to touch).
- `domain_name is None` → no-op (the monitor re-arm needs the domain to look up).
- otherwise: re-arm the gdbstub (below).

A single resetter (not a composite) covers M2: only remote needs an active reset. A second
provider that needs one later adds a composite the way `_CompositeReaper` fans out — deferred
until a second provider exists (no premature abstraction).

### 3. The reset re-arms the gdbstub via the QEMU monitor

`reset` opens a one-shot mutual-TLS connection with the existing `remote_connection`
lifecycle (ADR-0077), looks the domain up by name, and issues the HMP `gdbserver tcp::<port>`
command through libvirt's QEMU-monitor passthrough (`qemuMonitorCommand`, HMP flag). Re-arming
the listener drops the stale single client and re-opens the port, so the next attach connects
to a free stub. `port` comes from decoding the dead session's `transport_handle`
(`TransportHandleData`); `gdb_addr` is operator config.

This mirrors every other remote seam: the slow host interaction (`qemuMonitorCommand`) is an
injected seam that runs only under the `live_vm` gate; orchestration, self-selection, handle
decoding, and the error contract are unit-tested with a fake domain/connection. A libvirt
error maps to `CategorizedError(TRANSPORT_FAILURE)` (an existing category, ADR-0079 — no new
strings).

### 4. The reconciler change (`reconciler/loop.py`, the one gated file)

`_repair_dead_sessions(conn, stale_after, resetter)`:

1. The existing bulk `UPDATE … SET state='detached' … RETURNING` is widened to return
   `id, transport, transport_handle, run_id`. The detach transaction commits first.
2. **After** the transaction commits (never holding a DB transaction open across provider
   network I/O), for each detached row: resolve `domain_name`
   (`SELECT s.domain_name FROM runs r JOIN systems s ON s.id = r.system_id WHERE r.id = %s`),
   then `await resetter.reset(...)` wrapped best-effort — a raise is logged and the sweep
   continues, exactly like `repair_leaked_domains`'s per-domain `destroy`.
3. The return value stays the detached-row count, so `ReconcileReport.dead_sessions` keeps its
   meaning.

`Reconciler.__init__` / `reconcile_once` gain a `resetter: TransportResetter = NullResetter()`
parameter threaded into `_repair_dead_sessions`, mirroring how `reaper` is threaded.
`composition.py` gains `build_reconciler_transport_resetter()` (remote resetter when remote is
enabled via `is_remote_libvirt_configured()`, else `NullResetter`), and `__main__.py` passes it
to `Reconciler`.

### Ordering: detach first, reset best-effort

Detach is the durable repair — a dead session **must** leave `live` whether or not the port
can be freed (its worker is gone). Freeing the port is a best-effort side effect. Because the
detach commits before the reset is attempted, a transient reset failure is **not** retried;
the session is already `detached` and will not re-surface. The fallback on a failed reset is
exactly today's behavior — the next attach contends and surfaces `transport_conflict` — so
this is a strict no-regression improvement over the current always-wedged state. Durable
reset-retry (a `needs_transport_reset` flag swept independently of session state) is out of
scope; revisit only if operational data shows transient reset failures are common.

## Acceptance

1. **After a gdbstub-holding worker dies, the reconciler frees the port so the next attach
   succeeds instead of `transport_conflict`.** Unit-covered at two boundaries: the reconciler
   detaches the stale `live` session **and** invokes `resetter.reset` with its
   `gdbstub`/handle/`domain_name`; the remote resetter composes the `gdbserver tcp::<port>`
   re-arm against the domain. The end-to-end re-attach against a real QEMU stub is `live_vm` /
   operator-runbook territory (same boundary as the rest of the remote debug plane).
2. **A test covers the dead-worker-session → reset → re-attach path** (the reconciler test
   above), plus negative coverage: a NULL-heartbeat or non-stale session is neither detached
   nor reset; a non-`gdbstub` transport and a non-matching handle host are no-ops in the
   resetter.

## Edge cases and failure modes

- **NULL heartbeat** — never swept (a just-attached session that has not beaten yet);
  unchanged from today.
- **Non-stale heartbeat** — not a candidate; no detach, no reset.
- **drgn-live / ssh transport** — detached as before; the resetter no-ops (connectionless /
  no gdbstub).
- **local-libvirt gdbstub session** — detached as before; the remote resetter no-ops (handle
  host is loopback, not `gdb_addr`); the default deployment wires `NullResetter` anyway.
- **Domain already gone (System torn down)** — the monitor look-up fails; caught, logged,
  swept onward — the port is moot once the domain is gone.
- **Host unreachable (network partition)** — `qemu+tls` connect fails; caught, logged; the
  session is still detached. Falls back to today's behavior.
- **Reset raises** — logged at warning with the session id and the `transport_conflict`
  fallback named; never starves the other repairs or the rest of the dead-session sweep.

## Out of scope

- Durable reset-retry / a `needs_transport_reset` column.
- A composite multi-provider resetter (only remote needs a reset in M2).
- Local-libvirt active reset (co-located; OS frees the socket on worker death).
- Bare-metal KGDB-over-SoL reset (a later provider swaps the transport entirely).
