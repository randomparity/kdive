# Plan — Build-VM egress preflight (#519)

- **Spec:** [../../specs/2026-06-17-build-vm-egress-preflight.md](../../specs/2026-06-17-build-vm-egress-preflight.md)
- **ADR:** [../../adr/0155-build-vm-egress-preflight.md](../../adr/0155-build-vm-egress-preflight.md)
- **Guardrails (run before every commit):** `just lint`, `just type`, `just test`
  (CI gates each individually); doc commits also run `python3 scripts/check_adr_status.py`
  and `./scripts/check-doc-links.sh`.
- **File scope:** `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py` (+ its test),
  with minimal plumbing in `providers/shared/build_host/dispatch.py`,
  `providers/remote_libvirt/composition.py`, and the two ignoring factories
  (`dispatch.ssh_build_transport_factory`, `local` build factory). Do **not** touch
  `guest/agent.py` (#517) or `shared/build_host/shell_transport.py` (#518).

Execution mode: tightly coupled (factory-contract change threaded across layers) → implement
directly in one session, TDD. Each task is independently committable; keep guardrails green.

## Task 1 — Egress preflight in `build_vm.py` (TDD)

Where it fits: the core of the issue — the new source-reachability precondition after the
existing default-route gate, before the transport is yielded.

1. Failing tests first in `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`
   (spec Test plan cases 1–6). Extend the guest-agent fake so it answers the `git ls-remote`
   guest-exec distinctly from the route probe (match on the argv containing `ls-remote`), and
   records the issued argv so a test can assert it targets `HEAD`.
2. Implement on `EphemeralBuildVm`:
   - Add `egress_probe_timeout_s` to `BuildVmTiming` (default constant `_EGRESS_PROBE_CALL_TIMEOUT_S`).
   - Add `source: GitSourceRef | None` to `session()` and `ephemeral_build_session()`
     (keyword-only, default `None`).
   - After `_wait_for_network(...)` and only when `source is not None`, run a `_preflight_egress`
     that calls `transport.run(["git", "ls-remote", "--quiet", "--exit-code",
     source.remote, "HEAD"], cwd="/", timeout_s=...)`. `rc == 0` → return; `rc != 0` →
     raise `CategorizedError("build VM cannot reach build source <redacted>",
     CONFIGURATION_ERROR, details={"remote": redact_url_credentials(source.remote),
     "stderr": redacted_tail(result.stderr, self._secret_registry)})`. A `CategorizedError`
     from `transport.run` propagates (do not catch).
3. Acceptance: cases 1–6 pass; existing `session(...)`-without-`source` tests unchanged; the
   raised error names the redacted remote and the VM is torn down (the `finally` already does).

Rollback: revert the file; `source` defaults to `None` so the path is inert without it.

## Task 2 — Thread the source through the factory contract (TDD)

Where it fits: makes Task 1 real in production — delivers `kernel_source_ref` to the session
(otherwise the preflight is never invoked).

1. Failing test in `dispatch`'s test: `run_build_on_host` passes the resolved `GitSourceRef`
   (from a git `kernel_source_ref`) as the 4th factory argument, and `None` for a warm-tree
   string source.
2. Implement:
   - Extend `BuildHostTransportFactory` type to
     `Callable[[BuildHost, SecretRegistry, UUID, GitSourceRef | None], AbstractContextManager[BuildTransport]]`.
   - `run_build_on_host`: resolve `source = parsed.kernel_source_ref.git if
     isinstance(parsed.kernel_source_ref, GitKernelSource) else None` **before** opening the
     factory; pass it as the new positional arg.
   - Update `ssh_build_transport_factory` and the local factory to accept and ignore the arg.
   - `build_ephemeral_build_transport_factory._factory` accepts the arg and forwards
     `ephemeral_build_session(..., source=source)`.
3. Acceptance: both dispatch tests pass; the existing factory callers compile (ty green);
   full suite green.

Rollback: revert; the contract change is additive (one optional positional), no persisted state.

## Task 3 — Ship

Full suite (`just lint && just type && just test`) before first push; `/challenge --base main`
loop to approve; `security-review`; push; open PR vs `main` closing #519; drive to CI-green +
mergeable; hand off (no self-merge).
