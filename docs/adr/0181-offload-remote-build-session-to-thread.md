# ADR 0181 — Offload the whole remote-build transport session off the event loop

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers

## Context

Each worker process runs an affirmative `/livez` aux HTTP server on its asyncio event loop
(ADR-0090 §5). Liveness tracks the *loop*, not the work unit: a background ticker bumps the
heartbeat at a 1 s cadence, so a long-running job (a kernel build runs for minutes) never makes
`/livez` read stale. The contract that keeps this honest is that long, blocking provider work
runs in a worker thread (`asyncio.to_thread`), leaving the loop free to schedule the ticker and
answer the aux server. A blocking *synchronous* call left on the loop freezes the ticker and the
aux server together, so the loop appears dead even though the process is healthy.

`run_build_on_host` (the build-host dispatch seam) honored that contract only partially. The
actual `builder.build(...)` was offloaded via `asyncio.to_thread`, but the synchronous transport
*session* that wraps it was entered on the event loop:

```python
with factory(host, secret_registry, run_id, source) as transport:   # __enter__ on the loop
    return await _run_over_transport(...)                            # build() offloaded
```

For the ephemeral-libvirt build host, `factory(...)` is `ephemeral_build_session`, whose
`__enter__` provisions a throwaway VM and then runs three multi-minute, blocking readiness waits
(`wait_for_agent` 180 s, `wait_for_agent_responsive` 120 s, `_wait_for_network` 120 s) built from
`time.sleep` and synchronous libvirt calls; its `__exit__` tears the domain and overlay down.
The SSH factory's enter/exit materialize and remove an identity file. All of that ran on the
event loop.

On Kubernetes the kubelet liveness probe (chart default ~30 s grace) therefore SIGKILLed the
worker (`exit=137`) while `__enter__` blocked the loop during provisioning/readiness. The worker
restarted, re-claimed the in-flight build, and — because the SIGKILL pre-empted the session's
`finally` teardown — found the leftover `kdive-build-<run_id>` VM, logging
`domain is already running`, and crash-looped. The build never completed on k8s (#583). The
worker-local lane already offloaded its whole build via `asyncio.to_thread`, so only the
transport lanes were affected.

## Decision

`run_build_on_host` offloads the **entire** transport-session lifecycle — `factory(...)`'s
`__enter__` (provision + readiness waits / identity materialization), `bind_over_transport`, the
synchronous `builder.build(...)`, and `__exit__` (teardown) — to a single `asyncio.to_thread`
call, so none of it executes on the event-loop thread:

```python
return await asyncio.to_thread(
    _build_over_transport_session,
    builder, factory, host=..., run_id=..., parsed=..., source=..., secret_registry=...,
)
```

`_build_over_transport_session` is a plain synchronous function that opens the factory context
manager, binds the transport, runs the build, and lets the context manager tear down on exit. The
worker-local lane is unchanged (already offloaded). `bind_over_transport` stays a module-level
function so existing tests keep substituting it.

This restores ADR-0090 §5's invariant for the transport build lanes: the loop only schedules, the
ticker keeps ticking, and `/livez` answers within the probe deadline throughout a multi-minute
build. No new threads, processes, or chart knobs are introduced; the offload boundary is simply
moved out one level to wrap the whole synchronous session.

## Consequences

- A remote-libvirt or SSH build no longer blocks the worker event loop during VM
  provisioning, guest-agent / network readiness, or teardown; `/livez` stays responsive and the
  kubelet no longer SIGKILLs the worker mid-build.
- Because the session is no longer pre-empted by SIGKILL, its `finally` teardown runs normally, so
  the `kdive-build-<run_id>` VM is reaped on the same run rather than being orphaned. (Re-claim was
  already idempotent — `ensure_named_overlay` reuses an existing overlay and `_define_and_start`
  treats an already-running domain as the achieved post-state — and the reconciler's build-VM
  reaper still backstops a genuine worker crash.)
- Session enter, build, and teardown now run on the same worker thread sequentially, preserving
  the existing enter → build → exit ordering and lease semantics (success releases, failure
  retains for the reconciler). Existing tests that assert that ordering are unaffected.
- The whole session holds one thread-pool worker for the build's duration. The worker dispatches
  one job at a time, so this does not change concurrency; the default thread pool is sized well
  above the one in-flight build.

## Considered & rejected

- **Run the aux `/livez` server in its own thread/process, independent of the build loop.**
  Rejected: ADR-0090 §5 deliberately couples `/livez` to the event loop so that a *genuinely*
  wedged loop reads not-live. A liveness server that answers regardless of loop health would mask
  exactly the class of event-loop-starvation bug this issue is — it would have hidden #583 rather
  than surfaced it. The correct fix is to stop blocking the loop, not to stop measuring it.
- **Parameterize the chart's `kdive.auxProbes` so an operator can raise `failureThreshold` /
  `timeoutSeconds` for long builds.** Rejected as the fix (the issue itself frames it as a
  stopgap): it does not stop the loop from blocking — `/readyz`, `/metrics`, and any other
  loop-driven work still stall during provisioning, and the operator must hand-tune a probe to a
  build phase's duration. It only widens the window the bug hides in. (The live workaround of
  `kubectl patch`-ing `failureThreshold` high during the #572 campaign is retired by this fix.)
- **Offload only the readiness waits inside `EphemeralBuildVm.session`.** Rejected: it leaves the
  provisioning libvirt calls and the SSH identity materialization on the loop, and it would push
  thread-offload knowledge into every transport factory instead of owning it once at the dispatch
  seam that already offloads `build()`. Wrapping the whole session is simpler and uniform across
  transport kinds.
