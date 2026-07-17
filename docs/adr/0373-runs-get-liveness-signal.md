# ADR 0373 — runs.get liveness signal for a wedged-after-ready guest

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

A guest that livelocks *after* a successful boot is invisible to kdive's health model
(#1237). `runs.get` renders `data.boot_outcome=ready` (`READY_BOOT_OUTCOME`, #837/ADR-0254)
once the boot step observes the kdive-ready console marker with no pre-marker crash — but that
is a boot-time verdict, not live responsiveness. A guest wedged in a `VM_FAULT_OOM` printk/retry
storm after that point still reads `status=succeeded, boot_outcome=ready`, so a black-box agent
cannot tell a healthy guest from a dead one. The two existing signals do not close the gap:
`control.watch_for_crash` correctly returns `not_fired` for a pure livelock (no panic/BUG/Oops
signature; ADR-0367), and `systems.check_ssh_reachable` only *enqueues* an async probe job — it
is not folded into any state read. The motivating incident was diagnosed only by manually pulling
raw console bytes.

The design fork (which signal(s)?) was resolved by the maintainer: surface **both** an in-guest
console-storm heuristic and the SSH-reachability probe result, combined into one `liveness` block.
The two signals are independent and complementary — the console heuristic is black-box (no guest
round-trip, works even when SSH is dead), while SSH reachability is a direct probe but needs a
prior `check_ssh_reachable` call to have run.

## Decision

`runs.get` emits a new `data.liveness` block:

```
data.liveness = {
  "state": "healthy" | "degraded" | "unknown",
  "console_storm": <bool>,
  "ssh_reachable": <bool | null>,
  "checked_at": <iso-8601 | null>
}
```

**Scope / gate.** `liveness` is emitted only for a `SUCCEEDED`, `local-libvirt` Run whose boot
step reached `boot_outcome=ready` — the same gate as `data.boot_outcome`. Liveness ("healthy vs
wedged") is only a meaningful question once a guest has booted ready, and both underlying reads
are local-libvirt-host capabilities: the console log and the loopback SSH forward live on the
worker/MCP host only for local-libvirt (remote confirms readiness by boot-id, ADR-0082, exposes
no loopback SSH forward, and keeps its console off-host). On every other Run the key is absent —
never a null claim of health.

**Signal 1 — console_storm (black-box, read-time).** Derived at read time from the *current*
redacted console tail via `redacted_console_tail(system_id, secret_registry, max_chars=4096)` (the
same redacted-read primitive `check_ssh_reachable` uses for its unreachable console tail, ADR-0306
— no second redaction path). It fires when either:

- the printk rate-limit marker `callbacks suppressed` is present — the kernel itself reports it
  dropped a message flood, an unambiguous storm hallmark; or
- the combined count of storm signatures in the bounded tail is `>= 3` — a runaway retry loop
  fills the window with the same line repeatedly.

Storm signatures (case-insensitive substrings): `callbacks suppressed`, `vm_fault_oom`,
`soft lockup`, `hung task`, `hung_task`, `detected stall`, `out of memory`. These are the
livelock/OOM-storm hallmarks named in #1237 plus the RCU-stall and OOM-killer lines that
accompany them. A single benign `Out of memory:` (one app OOM) stays below the threshold and does
not flag a storm; the `callbacks suppressed` short-circuit is reserved for the printk flood the
marker definitionally reports. The 4096-char window and the threshold of 3 are chosen so a
one-off line never trips while a storm — which repeats its line many times — always does.

**Signal 2 — ssh_reachable (probe read-back).** The most recent `succeeded`
`CHECK_SSH_REACHABLE` job for the System, matched on the job payload's `system_id`, with its
`result_ref` verdict parsed for `reachable` and `checked_at` (the inline-verdict pattern,
ADR-0164/ADR-0303). `null` when the guest was never probed, when the probe job did not run, or
when the verdict is unparsable — never a fabricated `false`. `checked_at` reflects the SSH probe
measurement time; it is `null` when `ssh_reachable` is `null`. The console signal is always
current relative to the persisted console, so a `null` `checked_at` alongside a meaningful
`console_storm` is expected.

**state derivation.**

- `degraded` — `console_storm` is true, or `ssh_reachable` is `false` (a ready-booted guest that
  no longer answers SSH is wedged);
- `unknown` — no console tail was readable *and* `ssh_reachable` is `null` (no signal to judge);
- `healthy` — otherwise (at least one signal present, none degraded).

**No migration.** Both signals are derived at read time — the console from the live redacted log,
the SSH verdict from the existing `jobs.result_ref` — so no new column or persistence is added.
The `runs.get` wrapper docstring documents `data.liveness` so agents discover it (the agent-facing
contract is the `@app.tool` wrapper docstring, per project convention).

## Consequences

- A livelocked-after-ready guest reads `data.liveness.state=degraded` with `console_storm=true`,
  so an agent can distinguish it from a healthy guest without scraping raw console bytes.
- `runs.get` on a ready local-libvirt Run now reads the console tail (bounded, best-effort) and
  one indexed `jobs` row per call. The console read is capped at 4096 chars and failure-tolerant
  (`redacted_console_tail` returns `None` on any read error), so the status read stays cheap and
  never fails on an unreadable console.
- The heuristic is intentionally coarse: it flags a *degraded* signal, not a diagnosis. A false
  negative (a novel livelock with none of the signatures) degrades to `healthy`/`unknown` exactly
  as today; a false positive costs the agent one console read to confirm. Thresholds are recorded
  here so they can be tuned against real incidents.

## Alternatives considered

- **Console-storm heuristic only** (#1237 option 1): black-box and cheap, but blind to a clean
  wedge that emits no storm yet drops SSH. Rejected — the SSH probe catches that case.
- **SSH reachability only** (#1237 option 2): direct, but needs a probe to have run and is dead
  when the guest is livelocked hard enough to stall sshd's accept loop while still printing.
  Rejected — the console heuristic works with no guest round-trip.
- **Persisting a liveness snapshot column** (migration): rejected as premature — both signals
  derive cheaply at read time, and a persisted snapshot would be stale the moment it is written.
- **A synchronous SSH probe inside `runs.get`**: rejected — it would turn a read into a
  network-bound op with the probe's multi-second retry deadline; the async
  `check_ssh_reachable` job already owns that cost, and `runs.get` reads its latest verdict.
- **Emitting `liveness` for remote-libvirt**: rejected — no loopback SSH forward and the console
  is off-host, so neither signal is computable there; the key stays absent rather than misleading.
