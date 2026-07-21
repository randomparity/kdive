# Async jobs

Some KDIVE operations take 30 minutes or more. Provision, build, install, and
vmcore capture run as durable jobs in a Postgres-backed queue rather than blocking
the tool call. This keeps the MCP transport responsive and makes long ops survive
worker restarts ([ADR-0008](../adr/0008-async-worker-tier-job-queue.md),
[ADR-0018](../adr/0018-job-queue-worker-execution.md)).

## The long-op pattern

A tool that starts a long operation enqueues a job and returns immediately with a
`ToolResponse` (resource://kdive/docs/guide/response-envelope.md) whose `status` is `running` (or `queued`)
and whose `object_id` is the `job_id`. The `suggested_next_actions` field at this
point contains `["jobs.wait", "jobs.cancel"]`.

The agent then polls:

- **`jobs.wait(job_id, timeout_s)`** — blocks up to `timeout_s` seconds (capped at
  300), then returns the current job envelope. Use this in preference to a manual
  poll loop.
- **`jobs.get(job_id)`** — returns the current state immediately. Use when the agent
  has other work to interleave.
- **`jobs.cancel(job_id)`** — requests cancellation. The job's declared cleanup
  contract runs; the outcome is `canceled` or `failed` depending on how far the op
  progressed.
- **`jobs.list`** — lists jobs visible to the caller, useful for triage.

When `jobs.wait` or `jobs.get` returns `status: succeeded`, the `refs` field
contains an object-store reference (e.g. `{"result": "<key>"}`) for any produced
artifact. When it returns `status: failed`, the `error_category` field names the
failure. See the errors guide (resource://kdive/docs/guide/errors.md).

## Which operations are long-running

| Plane | Long-running tools |
|---|---|
| Allocation | `allocations.request` (when admission control defers) |
| Provisioning | `systems.provision`, `systems.reprovision`, `systems.teardown` |
| Install | `runs.install` |
| Boot | `runs.boot` |
| Control | `control.force_crash`, `control.power` |
| Retrieve | `vmcore.fetch` |

Fast operations — `debug.set_breakpoint`, `debug.read_memory`,
`debug.list_breakpoints` — are synchronous and return a `ToolResponse` directly
without a job. Note that `control.power` is **not** fast: every power action
(including `on`) enqueues a `power` job and returns a job handle.

### Typical durations

Rough, host-dependent figures for sizing `timeout_s` and a poll loop — not
guarantees. Figures are for a native-KVM guest; a TCG-emulated (foreign-arch)
guest scales by `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10`), so budget
roughly 10x longer (see [platform support](../operating/platform-support.md)):

- **`runs.boot`** — usually well under a minute once the guest is already
  provisioned and just needs to reach the `kdive-ready` marker. The server's
  ceiling (`KDIVE_LIBVIRT_BOOT_WINDOW_S`, default 900 s / 15 min — see
  [config reference](reference/config.md)) absorbs slow hosts (e.g. POWER9) and
  `kdump.service` arming; it is a ceiling, not a typical wait.
- **`runs.install`** — includes a boot pass under the same
  `KDIVE_LIBVIRT_BOOT_WINDOW_S` ceiling plus kernel/module install work; expect
  low minutes on a warm host.
- **`systems.provision`** (image customization / first boot) — several minutes
  to tens of minutes; the customization boot ceiling
  (`KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S`, default 1800 s / 30 min) absorbs
  first-time mirror/network fetch variance.
- **`vmcore.fetch`** (kdump capture) — the guest reboots out of the capture
  kernel and uploads; budget on the order of 300 s for that readiness window.
- **`control.force_crash` / `control.power`** — seconds, dominated by the
  hypervisor call and a readiness poll.

For anything past the first minute, prefer several short `jobs.wait` calls
(default `timeout_s` 30 s) over one long wait sized to the worst case — see
[transport resets](#transport-resets-and-retries) below.

## Transport resets and retries

A long `jobs.wait` holds one streamable-HTTP request open while it polls (up to the 300 s
cap). An intermediary — a reverse proxy or load balancer in front of the server — may apply
its own idle/read timeout and sever that held stream. When it does, the client sees a raw
`socket connection was closed unexpectedly` transport error rather than a `ToolResponse`
envelope: the connection that would carry the envelope is already gone, so the server cannot
wrap that specific drop ([ADR-0138](../adr/0138-transport-reset-retry-contract.md)).

**The contract:** a transport reset on `jobs.wait` (or any idempotent read such as `jobs.get`,
`jobs.list`, `systems.get`, `runs.get`) is **transient and safe to retry unchanged**. Retry the
same call.

**The token-efficient pattern** is repeated **short** `jobs.wait` calls rather than one long
hold. The default `timeout_s` is 30 s, well under any normal proxy timeout. A non-terminal
`jobs.wait` returns the job's current (`running`/`queued`) envelope with `jobs.wait` in
`suggested_next_actions` — that *is* the "still running, call again" signal; re-issue the wait
while the returned envelope is non-terminal. Requesting a long explicit `timeout_s` (up to 300 s)
holds the stream near the reset window and risks an intermediary cut; that drop is retryable, but
short waits avoid it.

## Retrying the initial enqueue (idempotency)

The read-retry contract above covers `jobs.wait`/`jobs.get`. But a transport reset can also
drop the **response to the enqueuing call itself** — `runs.install`, `vmcore.fetch`,
`control.power`, `systems.provision`, and the rest of the create/enqueue surface. A blind
retry of that call could enqueue a second job. To retry it safely, pass an `idempotency_key`
([ADR-0193](../adr/0193-uniform-mutation-idempotency.md), and see
the envelope guide, resource://kdive/docs/guide/response-envelope.md): a repeated key returns the
**same job envelope** instead of enqueuing again.

**Replay / GC window.** A recorded key replays only within the reconciler's retention window
(default **7 days**, configurable). The reconciler garbage-collects keys past the window on
its periodic pass. After a key is collected, repeating it is treated as a *fresh* enqueue —
still safe at the job layer, because the job-enqueue tools derive their job `dedup_key` from
the target object (e.g. `{run_id}:build`, `{system_id}:capture_vmcore:{method}`), so a
same-target re-enqueue returns the existing job rather than a duplicate. The `idempotency_key`
adds, on top of that, an identical-*envelope* replay for the bounded window.

## Durability and retries

Jobs carry a worker heartbeat/lease. If a worker dies mid-run, the job is
reclaimed by another worker for a remaining attempt. Attempt counts increment at
claim (not at failure), so a worker that dies before recording a result still
spends the attempt; jobs cannot loop forever. A job that exhausts `max_attempts`
is dead-lettered to `failed` and surfaces in `jobs.list` for triage.

Only object-store references and taxonomy categories are stored on the job row —
never raw exception messages or console text, which could carry guest output or
secret material.
