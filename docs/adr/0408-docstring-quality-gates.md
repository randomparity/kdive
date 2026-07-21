# ADR-0408: docstring quality gates on the agent-facing tool surface (#1367)

- Status: Accepted
- Date: 2026-07-21

## Context

The ADR-0047 documentation guard (`tests/mcp/core/test_tool_docs.py`) builds the live
FastMCP registry and asserts structural properties of every tool's agent-rendered
description: non-empty description and parameters, a valid maturity, the destructive hint
matching the reviewed set, and so on. It did not assert anything about the *content* of a
destructive or asynchronous tool's description, so a destructive tool could ship a bare
one-liner that named neither what it destroys nor the role it requires, and a tool whose
only result path is a polled job handle could omit the poll tool entirely. The
`ADR-\d+`-leak rule the parent epic lists is already enforced separately by
`tests/mcp/core/test_no_adr_leak.py` (ADR-0270) and is deliberately not duplicated.

The obvious "job-returning" signal is not mechanically available: every tool returns the
generic `ToolResponse` envelope, and `job_id`/`queued` live in the runtime response value,
not the static output schema. Lifecycle tools that enqueue a job to advance a durable
entity (`images.build`, `systems.provision`/`teardown`, `runs.boot`) are tracked through
that entity's read tool, not `jobs.wait`, so "enqueues a job" is the wrong population.

## Decision

Add three content guards to the ADR-0047 doc guard, each paired with a canary that
exercises its predicate against synthetic input so a neutered rule fails loudly rather than
passing over a clean tree:

1. **Destructive tools name a consequence or role.** Every tool in
   `_docmeta.DESTRUCTIVE_TOOLS` must name a concrete consequence (delete, prune, teardown,
   irreversible, …) or the privileged role/gate it demands (`platform_admin`, admin, RBAC,
   …) in its description.
2. **Job-handle tools reference `jobs.wait`.** "Job-returning" is defined as a reviewed set
   `_JOB_HANDLE_TOOLS` — the out-of-band async tools whose result is reachable only by
   polling an opaque job handle (`vmcore.fetch`, the `systems.snapshot`/`restore` family,
   the SSH-probe and `control.*` diagnostics). Each must name `jobs.wait` (the tool carrying
   the transport-reset retry contract), not merely "poll" or `jobs.get`. An equality pin
   asserts the set equals exactly the registered tools that reference `jobs.wait`, so a
   dropped mention or an unreviewed new claimant fails and forces a deliberate update.
3. **Consequential tools clear a content floor.** A destructive or job-handle tool's
   description must be more than a single sentence, so a one-liner like
   "Capture and persist a vmcore." cannot ship. The floor is scoped to that population;
   genuinely simple reads/mutations ("Open an investigation.") stay exempt.

Enforcing rule 1 and rule 3 revealed one real gap: `images.extend` shipped the bare
"Extend an image catalog entry lease." Its docstring is rewritten to name the
`platform_admin` break-glass role and the retention-deferral consequence; the generated
tool reference (`docs/guide/reference/images.md`) is regenerated to match.

## Consequences

The reviewed `_JOB_HANDLE_TOOLS` set shares the drift profile of `DESTRUCTIVE_TOOLS`: a
future out-of-band job tool that forgets `jobs.wait` is not auto-detected until it is either
reviewed into the set or coincidentally references the tool. This residual under-inclusion
is accepted — there is no static signal to close it — and is bounded by the equality pin,
which catches any tool that *does* reference `jobs.wait` without being reviewed. Guard and
docs only; no schema change, no migration.
