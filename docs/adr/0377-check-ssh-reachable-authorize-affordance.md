# ADR 0377 — `check_ssh_reachable` points to `authorize_ssh_key`

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

`systems.check_ssh_reachable` (ADR-0298) is a deliberately auth-free liveness probe:
it opens a TCP connection and returns `reachable=true` iff the server banners `SSH-`,
sending no handshake and attempting no authentication
(`jobs/handlers/connectivity/ssh_reachable.py`). `reachable=true` therefore says only
that sshd is answering — **not** that the caller's key is authorized.

The handler returns a bare job handle (`ToolResponse.from_job(job)`), whose only
suggested next actions are the generic job-lifecycle set (`jobs.wait`/`jobs.cancel`).
It gives no in-band pointer to `systems.authorize_ssh_key`, so an agent reads
`reachable=true` as "I can log in" and only discovers `Permission denied (publickey)`
on its first real SSH attempt, then has to find `authorize_ssh_key` on its own. The
sibling `systems.ssh_info` already surfaces `authorize_ssh_key` as a next action
(`ssh_access.py`), so the probe is the inconsistent one. The probe being auth-free is
correct by design (#1250); the gap is the missing next-action affordance, not the probe
semantics.

`ToolResponse.from_job` is a shared builder used by every job-returning tool. It
hard-codes `suggested_next_actions = _NEXT_ACTIONS[job.state]` and has no hook for a
tool-specific next action.

## Decision

**Extend `from_job` with an optional, keyword-only `extra_next_actions`.** The generic
lifecycle actions for the job's state come first, then any caller-supplied actions are
appended (order preserved, not deduplicated). The parameter defaults to `None`, so every
existing caller — and `_common.job_envelope`, which wraps `from_job` — is byte-for-byte
unchanged. This keeps the one shared builder that carries the job handle *and* lets a tool
add its own steer, rather than re-composing the whole envelope (refs, `kind`, failure
context, state→actions) by hand in the handler.

**`check_ssh_reachable` passes the RBAC-gated `authorize_ssh_key` pointer.** It builds
`visible_next_actions(["systems.authorize_ssh_key"], ctx, system.project)` — the same
project-scoped RBAC filter `ssh_info` uses (ADR-0261) — and hands the result to
`from_job(job, extra_next_actions=…)`. `authorize_ssh_key` is CONTRIBUTOR-gated while the
probe is VIEWER-gated, so a viewer-only caller sees only the lifecycle actions; a
contributor additionally sees `systems.authorize_ssh_key`.

**Docstring + guide clarification.** The `check_ssh_reachable` wrapper docstring (the
agent-visible surface, serialized into the generated tool reference) states that reachable
confirms sshd is answering, not that the key is authorized, and to call
`systems.authorize_ssh_key` if login is denied. The `systems.md` toolset guide gains the
same transport-vs-auth note.

**No migration.** Pure response-shaping and documentation over existing fields.

## Consequences

- A contributor polling `check_ssh_reachable` is steered to `authorize_ssh_key` in-band,
  matching `ssh_info` — the reachable≠authorized distinction is now an affordance, not a
  lesson learned on the first denied login.
- `from_job` gains one optional keyword; the appended actions are RBAC-filtered by the
  caller before they reach the builder, so `from_job` itself stays authorization-agnostic.
- The generated tool reference documents the reachable≠authorized distinction.

## Alternatives considered

- **Compose the envelope in `check_ssh_reachable` without touching `from_job`.** Rejected:
  duplicates the shared builder's refs/`kind`/failure-context/state→actions logic in one
  handler, which would drift from the canonical builder.
- **Have `from_job` itself append `authorize_ssh_key` for SSH job kinds.** Rejected: bakes
  a tool-specific, RBAC-sensitive steer into the authorization-agnostic shared builder; the
  caller already holds the `ctx`/`project` needed to filter it.
- **Documentation only (docstring + guide, no next action).** Rejected: leaves the
  machine-readable affordance gap the issue names — an agent reading `suggested_next_actions`
  still gets no pointer.
