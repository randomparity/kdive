# Build-VM egress preflight to the configured source

- **Issue:** #519
- **ADR:** [0155](../adr/0155-build-vm-egress-preflight.md)
- **Status:** Accepted
- **Date:** 2026-06-17

## Problem

The ephemeral remote-libvirt build VM (ADR-0100) gates readiness on an in-guest default-route
probe (ADR-0144) and then yields the transport; the caller's first operation is the source clone
(`git fetch`). A default route is installed by DHCP, but it does **not** guarantee the guest can
reach the configured build source: DNS may be broken, the guest-subnet→internet hop may be
policy-dropped while the route still exists, or the remote may simply be unreachable from the
guest's vantage. A live campaign on real hardware (`ub24-big`, FORWARD ACCEPT, NAT mode) showed a
build VM **with** a default route whose clone still failed for no egress to `github.com`,
surfacing as a confusing clone/fetch error (campaign failures `6b99aa8d`, `d39a408e`).

This is false-confidence in the readiness signal — not a host misconfiguration. The fix adds a
source-specific reachability precondition after the route gate, so an unreachable source fails
the gate naming the source before the clone runs.

## Scope

In scope (build_vm.py + its tests, with minimal dispatch plumbing — see Design):

- A bounded in-guest `git ls-remote` egress preflight in `EphemeralBuildVm.session`, after the
  default-route gate, before the transport is yielded.
- Threading the configured source (`kernel_source_ref` → `GitSourceRef`) into the session via the
  build-host transport-factory contract.
- A failure mapped to `CONFIGURATION_ERROR` naming the **redacted** remote, with the probe's
  redacted stderr in details.

Explicitly out of scope (with rationale, settled in ADR-0155):

- The default-route gate itself stays (ADR-0144) — it runs first and is unchanged.
- `ShellBuildTransport.clone` / `git fetch` stderr surfacing — owned by #518 (companion bug).
- Guest-agent `_exit_status` masking — owned by #517 (companion bug).
- No DNS-only or host:port-parse probe (under-checks / needs URL parsing — ADR-0155 rejected).
- No retry/poll of the preflight — the route gate already absorbed DHCP-slowness.

## Design

### Where the source is known vs. where the gate runs

`run_build_on_host` (`providers/shared/build_host/dispatch.py`) has the parsed profile (hence
`kernel_source_ref`) in scope, but it resolves the git remote (`_git_coords`) only **after** the
transport factory has opened — and the build-VM readiness gate runs at factory/context-manager
*entry*. So the source must be passed **into** the factory.

Change the `BuildHostTransportFactory` contract to accept an optional source:

```python
type BuildHostTransportFactory = Callable[
    [BuildHost, SecretRegistry, UUID, GitSourceRef | None],
    AbstractContextManager[BuildTransport],
]
```

`run_build_on_host` resolves `source: GitSourceRef | None` from `parsed.kernel_source_ref`
(non-git warm-tree → `None`) **before** opening the factory and passes it through. The SSH and
local factories accept and ignore it; the ephemeral factory forwards it to
`ephemeral_build_session(..., source=source)` → `EphemeralBuildVm.session(..., source=source)`.

### The preflight (in `build_vm.py`)

After `wait_for_agent` + the existing `_wait_for_network` route gate, and only when
`source is not None`:

- Run `git ls-remote --quiet --exit-code <remote> <ref>` once via the bound
  `GuestExecBuildTransport.run` (allowlist `{'/bin/sh'}`, unchanged), bounded by a per-call
  timeout (`_EGRESS_PROBE_CALL_TIMEOUT_S`, a constructor-default on `BuildVmTiming`).
- `rc == 0` → proceed (yield the transport).
- `rc != 0` → raise `CategorizedError("build VM cannot reach source <redacted-remote>",
  category=CONFIGURATION_ERROR, details={"remote": redact_url_credentials(remote),
  "stderr": redacted_tail(result.stderr, secret_registry)})`. The `finally` tears the VM down
  exactly as the route-gate failure path already does.
- A `CategorizedError` raised by `transport.run` (agent dropped) propagates unchanged.

The remote argument is the same value the immediately-following clone passes to git; the build VM
runs fixed `shlex`-joined argv (no new injection surface beyond the existing clone).

### Redaction

The named remote can carry `user:pass@` userinfo. The error message and the `remote` detail use
`kdive.security.secrets.redaction.redact_url_credentials`; the probe stderr uses the existing
`redacted_tail(..., secret_registry)`. No live credential reaches an error detail or a log.

## Test plan (TDD, `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`)

Drive `EphemeralBuildVm.session` over the existing fake provision-connection + guest-agent fake
(no libvirt host), extending the agent fake to answer the `git ls-remote` probe:

1. **Default route present, source unreachable → gate fails before clone, names source.** Agent
   answers the route probe `rc 0` and the `ls-remote` probe `rc != 0`. Assert the session raises
   `CategorizedError` with `category == CONFIGURATION_ERROR`, the message/details name the
   (redacted) remote, the VM is torn down (domain gone, overlay reclaimed), and **no clone-shaped
   command ran** (the probe is the last guest-exec issued).
2. **Working egress proceeds unchanged.** Route `rc 0`, `ls-remote` `rc 0` → session yields a
   `GuestExecBuildTransport`; teardown runs on exit.
3. **No source supplied (`source=None`) → preflight skipped.** Existing route-only behavior; no
   `ls-remote` guest-exec is issued (asserts the warm-tree lane is unregressed).
4. **Credentialed remote is redacted.** A remote whose URL carries `user:secret@` userinfo that
   fails the probe → the raised error's message/details contain the host but **not** the userinfo.
5. **Agent drop during the preflight propagates.** The `ls-remote` probe raises a
   `CategorizedError` (transport_failure) → it propagates unchanged (not swallowed as
   not-ready), VM torn down.

Existing tests that call `vm.session(_BASE_VOLUME, run_id=RUN_ID)` (no `source`) must keep passing
unchanged — the preflight defaults to skipped.

## Rollout / rollback

Pure provider-side behavior change; no migration, no schema, no env var. Rollback is reverting the
PR. The default-route gate (ADR-0144) is untouched, so reverting this change restores the prior
route-only behavior with no data or contract residue.
