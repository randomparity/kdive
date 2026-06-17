# ADR 0154 — Verify FETCH_HEAD before checkout in the remote build clone

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

`ShellBuildTransport.clone()` (`providers/shared/build_host/shell_transport.py`) is the
shared clone implementation for both remote build transports — `SshBuildTransport` (over
`ssh`) and `GuestExecBuildTransport` (over the qemu-guest-agent exec channel). It clones a
kernel tree with `git init` → `git fetch --depth 1 <remote> <ref>` → `git checkout FETCH_HEAD`
(the init+fetch+checkout sequence resolves an arbitrary ref/sha, which a plain
`clone --depth 1` cannot).

The original code surfaced the fetch's stderr **only when `fetch.returncode != 0`** and then
ran `git checkout FETCH_HEAD` unconditionally. When a transport masks the remote command's real
exit status to `0` — the companion guest-agent `_exit_status` masking (#517) is exactly such a
seam — a fetch that did not complete reports success, leaves no `FETCH_HEAD`, and the next
`checkout FETCH_HEAD` fails with `error: pathspec 'FETCH_HEAD' did not match any file(s) known
to git`. The actionable network/transport error in the fetch's own stderr is discarded and the
operator is shown the misleading downstream pathspec message.

Live evidence: build run `6b99aa8d` on `ub24-big-build` recorded
`failure_message: "git checkout FETCH_HEAD failed on remote"` /
`failure_detail_stderr: "error: pathspec 'FETCH_HEAD' did not match any file(s) known to git"`.
The git mechanism itself was verified correct; `FETCH_HEAD` was genuinely absent because the
fetch did not complete and its rc was masked to 0.

## Decision

Add a **defense-in-depth** verification step between fetch and checkout that does not depend on
the companion masking fix (#517) landing first:

1. After the fetch — **regardless of its reported exit status** — run
   `git -C <dest> rev-parse --verify --quiet FETCH_HEAD`.
2. If `FETCH_HEAD` does not resolve, raise a `CategorizedError` with category
   `TRANSPORT_FAILURE` whose `details["stderr"]` carries the **fetch's** redacted stderr (the
   true cause), not the checkout's pathspec message. The checkout is never reached on this path.
3. The existing fetch-rc-non-zero path (category `CONFIGURATION_ERROR`, fetch stderr) and the
   checkout-rc-non-zero path (category `CONFIGURATION_ERROR`, checkout stderr — now only
   reachable when `FETCH_HEAD` genuinely resolved) are unchanged.

`--quiet` is used so a normal absent-ref check produces no spurious stderr/stdout. The fetch
stderr is redacted through the existing `redacted_tail(..., self._secret_registry)` helper, the
same path every other surfaced stderr in this module already uses.

### Failure-contract change

This changes the failure contract of `clone()`: a masked-success fetch that produces no
`FETCH_HEAD` now raises `TRANSPORT_FAILURE` (was: `CONFIGURATION_ERROR` from the misleading
checkout pathspec error). `TRANSPORT_FAILURE` is the honest category — the root cause is a
network/transport fault between the build host and the git remote, not operator misconfiguration
of remote/ref.

## Consequences

- A failed remote fetch now fails honestly with its real error even when the underlying
  transport masks exit status, independently of #517.
- One extra cheap `rev-parse` round-trip per clone on the success path.
- Both remote transports inherit the fix through the shared base; no transport-specific code,
  no schema/migration/DB/auth/entrypoint change.

## Alternatives considered

- **Wait for #517 (exit-status masking) and rely on the fetch rc.** Rejected: leaves `clone()`
  brittle to any future status-masking seam and to git itself reporting success while producing
  no usable `FETCH_HEAD`. The explicit `FETCH_HEAD` precondition is correct regardless of who
  reports the fetch's status.
- **Reuse `CONFIGURATION_ERROR` for the no-`FETCH_HEAD` path.** Rejected: a vanished fetch is a
  transport fault, not a config error; mislabeling it sends operators to the wrong remediation.
