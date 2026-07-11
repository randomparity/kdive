# Plan — Offload the remote-build transport session off the event loop (#583)

Derived from [the spec](../../specs/2026-06-19-offload-remote-build-session.md) and
[ADR-0181](../../adr/0181-offload-remote-build-session-to-thread.md). Single tightly-coupled
source change plus a regression test; implemented directly (no subagent fan-out).

## Context

`run_build_on_host` (`src/kdive/providers/shared/build_host/dispatch.py`) dispatches a Run's build
onto the selected build host. For a non-`LOCAL` host it enters a **synchronous** transport-session
context manager on the event loop and offloads only the inner `builder.build(...)`:

```python
with factory(host, secret_registry, run_id, source) as transport:   # on the loop
    return await _run_over_transport(capable, transport, ...)        # build() offloaded
```

The ephemeral-libvirt factory's `__enter__` blocks the loop for minutes (VM provision + three
readiness waits built on `time.sleep` + synchronous libvirt), freezing the `/livez` heartbeat
ticker and aux uvicorn server, so the kubelet SIGKILLs the worker and it crash-loops (#583). The
fix moves the offload boundary out one level so the whole session runs in a worker thread.

Guardrail commands (this repo; run before every commit):
`just lint`, `just type` (whole tree), and the focused tests via
`uv run python -m pytest <path> -q`. Full gate before push: `just ci`.

## Task 1 — Regression test: the transport session runs off the event-loop thread

**Where it fits:** Establishes the falsifiable check from the spec (criterion 1/2). Must fail on
current code and pass after Task 2.

**File:** `tests/providers/build_host/test_dispatch_admission.py` (extend; it already exercises
`run_build_on_host` directly with fakes and no DB).

**What to write:**

- A transport-capable recording builder (advertises `over_transport` returning self; `build`
  returns a `BuildOutput` and records the running thread via `threading.current_thread()`).
- A fake factory (a `@contextmanager`) for `BuildHostKind.EPHEMERAL_LIBVIRT` (or SSH) that records
  the running thread at `__enter__` and at `__exit__`, and yields a fake transport.
- Drive `asyncio.run(run_build_on_host(builder, ephemeral_host, run_id, parsed,
  secret_registry=SecretRegistry(), kernel_src="", transport_factories={KIND: factory}))`.
- Capture the event-loop thread (the thread `asyncio.run` executes on — i.e.
  `threading.current_thread()` captured inside the awaited coroutine, which is the main thread).
- Assert the recorded `__enter__`, `build`, and `__exit__` threads are all **not** the
  event-loop thread.

**Acceptance:** with the unmodified `dispatch.py` the test fails (enter/exit thread == loop
thread); after Task 2 it passes. Do not weaken any existing gated test.

**Verify it fails first:** run the new test against current `dispatch.py` and confirm the failure
is the thread-identity assertion (not an import/setup error).

## Task 2 — Offload the whole `with factory(...)` block in `run_build_on_host`

**Where it fits:** The fix itself.

**File:** `src/kdive/providers/shared/build_host/dispatch.py`.

**What to change:**

- Add a synchronous helper:
  ```python
  def _build_over_transport_session(
      builder: TransportCapableBuilder,
      factory: BuildHostTransportFactory,
      *,
      host: BuildHost,
      run_id: UUID,
      parsed: ServerBuildProfile,
      source: GitSourceRef | None,
      secret_registry: SecretRegistry,
  ) -> BuildOutput:
      with factory(host, secret_registry, run_id, source) as transport:
          git_remote, git_ref = _git_coords(parsed, run_id)
          bound = bind_over_transport(
              builder, transport,
              host_workspace_root=host.workspace_root,
              git_remote=git_remote, git_ref=git_ref,
              secret_registry=secret_registry,
          )
          return bound.build(run_id, parsed)
  ```
  It calls `bind_over_transport` by its module-global name so the existing
  `monkeypatch.setattr(build_host_dispatch, "bind_over_transport", ...)` seam still applies inside
  the worker thread.
- In `run_build_on_host`, after resolving `factory` (and keeping the unsupported-kind
  `CONFIGURATION_ERROR` *before* any offload), replace the on-loop `with factory(...)` /
  `await _run_over_transport(...)` with:
  ```python
  return await asyncio.to_thread(
      _build_over_transport_session,
      capable, factory,
      host=host, run_id=run_id, parsed=parsed, source=source,
      secret_registry=secret_registry,
  )
  ```
- Remove the now-unused `async def _run_over_transport` (its body folds into the sync helper).
  Keep `bind_over_transport` and `_git_coords` as module-level functions.
- Worker-local lane (`host.kind is BuildHostKind.LOCAL`) is untouched.

**Constraints:** ≤5 positional params (host/run_id/parsed/source/secret_registry are keyword-only;
builder + factory positional = 2). No relative imports. Keep `_require_transport_capable` raising
before the offload.

**Acceptance:** Task 1's test passes; the unsupported-kind error still raises synchronously before
any thread is spawned.

## Task 3 — Guardrails + regression coverage of existing behavior

- Run `just lint`, `just type`, then the focused suites that exercise this seam:
  - `tests/providers/build_host/test_dispatch_admission.py`
  - `tests/jobs/handlers/test_build_handler_transport.py` (asserts ephemeral enter → build → exit
    ordering and lease success-releases / failure-retains — must stay green; the ordering is
    preserved because the helper runs enter → build → exit sequentially in one thread).
- Then the full `just ci` before pushing (architecture/doc-generation tests live outside the
  edited dirs).

**Acceptance:** all green, zero warnings.

## Rollback / cleanup

Single-file source change; revert the `dispatch.py` edit and the test addition to roll back. No
schema, migration, config, or chart change. No generated-doc or snapshot regeneration is implied
by this change.
