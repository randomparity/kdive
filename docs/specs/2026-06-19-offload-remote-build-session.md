# Offload the remote-build transport session off the worker event loop (#583)

- **Issue:** #583 — Worker liveness probe times out during remote build → SIGKILL crash-loop
- **ADR:** [ADR-0181](../adr/0181-offload-remote-build-session-to-thread.md)
- **Status:** Accepted

## Problem

On Kubernetes the worker's `/livez` aux endpoint stops answering during a remote-libvirt kernel
build, the kubelet liveness probe fails (chart default ~30 s grace), and the worker is SIGKILLed
(`exit=137`). On restart it re-claims the in-flight build, finds the leftover
`kdive-build-<run_id>` VM, and crash-loops; the build never completes on k8s.

The root cause is event-loop starvation, not memory pressure. The worker keeps `/livez` honest
via a background heartbeat ticker that runs on the asyncio loop (ADR-0090 §5); a long job is fine
*as long as it does not block the loop*. `run_build_on_host` offloads the inner
`builder.build(...)` via `asyncio.to_thread`, but enters the synchronous transport-session context
manager on the event loop:

- `src/kdive/providers/shared/build_host/dispatch.py:75-85` — `with factory(...) as transport:`
  runs `__enter__`/`__exit__` on the loop; only `_run_over_transport`'s `build()` is offloaded.
- For ephemeral-libvirt, `factory(...)` is
  `providers/remote_libvirt/lifecycle/build_vm.py::ephemeral_build_session`, whose `__enter__`
  blocks for minutes in `wait_for_agent` (180 s), `wait_for_agent_responsive` (120 s), and
  `_wait_for_network` (120 s) — all `time.sleep` + synchronous libvirt — and whose `__exit__`
  tears the VM down. All of this blocks the loop, freezing the heartbeat ticker and the aux
  uvicorn server together.

## Goal / success criteria

1. The entire transport-session lifecycle (factory `__enter__`, bind, `builder.build`, `__exit__`)
   runs off the event-loop thread for every non-`LOCAL` build host.
2. While a synchronous factory `__enter__` is blocking, the worker event loop remains free to run
   other tasks (the `/livez` ticker / aux server) — verified by a test that the session does not
   execute on the loop thread.
3. Enter → build → exit ordering and build-host lease semantics (success releases the lease,
   failure retains it for the reconciler) are unchanged.
4. The worker-local build lane is unchanged (it already offloads its whole build).

Falsifiable check: a test driving `run_build_on_host` with a factory that records the running
thread at `__enter__`, inside `builder.build`, **and** at `__exit__` (teardown — itself blocking
synchronous libvirt) asserts none of those threads is the event-loop thread. It fails on the
current code (which enters/exits the session on the loop) and passes after the fix. Pinning all
three points keeps the guard as wide as criterion 1's claim, so a later regression that moved any
phase — including teardown — back onto the loop is caught.

## Non-goals

- No change to the readiness-gate timeouts, the guest-agent classification (ADR-0168/0178), or the
  reconciler build-VM reaper.
- No new aux-server thread/process and no new chart probe knobs (see ADR-0181 rejected
  alternatives).
- No change to the residual-VM re-claim path: it is already idempotent and is exercised less often
  once SIGKILL-mid-build stops.
- No change to build cancellation: `asyncio.to_thread` is not cancellable, so a worker stop /
  shutdown does not interrupt an in-flight session. This is unchanged from today's `build()`
  offload — the session simply spans a wider sync region now — and the lease fence + reconciler
  reaper remain the backstop for a worker that dies mid-build.

## Approach

Move the offload boundary out one level in `run_build_on_host`:

- Extract a synchronous `_build_over_transport_session(builder, factory, *, host, run_id, parsed,
  source, secret_registry) -> BuildOutput` that opens `factory(...)`, calls
  `bind_over_transport(...)`, and runs `builder.build(run_id, parsed)` inside the `with` block.
- Replace the on-loop `with factory(...)` / `await _run_over_transport(...)` pair with a single
  `await asyncio.to_thread(_build_over_transport_session, ...)`.
- Keep `bind_over_transport` a module-level function (tests substitute it via
  `monkeypatch.setattr(build_host_dispatch, "bind_over_transport", ...)`), and have the synchronous
  helper call it by its module-global name so the patch still applies inside the worker thread.
- Keep the unsupported-host-kind `CONFIGURATION_ERROR` raised before any offload.

## Risks

- A blocking call still left on the loop *before* the offload would reintroduce the bug; the test
  in success-criterion 2 guards the session enter, which is where the multi-minute waits live.
- `bind_over_transport` must remain looked up at call time so the test seam keeps working; covered
  by the existing transport/ephemeral handler tests.
