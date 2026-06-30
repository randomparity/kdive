# ADR 0280 — Resolve the interactive-console request by redirect to in-guest command execution

- **Status:** Accepted
- **Date:** 2026-06-30
- **Deciders:** KDIVE maintainers

## Context

A black-box review observed that KDIVE exposes console artifacts read-only but
offers no MCP-native interactive console attach/send/read workflow, so running an
in-guest reproducer ("boot, then run a reproducer script and watch output")
required dropping to host-local `virsh -c qemu:///system console ...`. That host
access leaks provider implementation detail, is non-portable across providers, and
puts the interactive session outside KDIVE's tooling surface so its bytes are not
captured as durable artifacts. The review filed #936 to revisit the trade-off
explicitly and posed a two-way fork: (a) keep the rejection and close the gap via
in-guest command execution, or (b) add a `console.attach/send/read/detach` tool
that persists a transcript artifact.

The same capability was considered and rejected in **ADR-0273** (post-readiness
console observation): a `systems.observe_console` / `runs.tail_console` tool lost
because the platform already carries too many tools, a top-level tool review is
pending, and the existing `artifacts.{list,get,search_text}` surface already serves
windowed reads, paging, download URIs, search, redaction, and project
authorization over console bytes. ADR-0273 also rejected "live streaming with a
bounded window" as the heaviest option — it adds a transport concern MCP's
request/response surface does not have. A stateful interactive console *session*
(attach → send → read → detach) has that same shape.

Two further facts post-date #936's framing and bound the decision:

- The in-guest *write* path the reviewer wants already has a landed foundation and
  a higher-priority backlog dedicated to it. **#782 (ADR-0271, shipped)** gives an
  agent direct SSH to a `ready` System with an agent-supplied public key, over the
  existing managed loopback forward. Built on that access, **#909** (bounded guest
  command execution, `priority:high`, *can start now*) returns an explicit
  command's exit status, duration, and redacted bounded stdout/stderr with
  overflow stored as System artifacts; **#910** (`priority:high`, blocked by #909)
  records run-owned reproducer executions and their output artifacts under the Run;
  **#937** (`priority:medium`) adds a lightweight enable-SSH / one-shot-command
  path. The reproducer workflow #936 calls for is the stated goal of that cluster.

- KDIVE's console story is deliberately read-only: per-Run boot evidence
  (ADR-0235), rotating System-owned parts (ADR-0273), and Run correlation
  (ADR-0279) all expose console *output* through `artifacts.*` with mandatory
  redaction. No console *input* path exists. Adding interactive send would
  introduce the first agent-driven write to the guest console.

## Decision

We will **not** add an interactive console tool (`console.attach/send/read/detach`
or a narrower `console.send`) and we reaffirm ADR-0273's rejection. The reproducer
need #936 raises is **redirected to the in-guest command-execution path** rather
than served by a console tool. That path is partly shipped and partly planned: SSH
access (#782) has landed, while bounded guest command execution (#909), run-owned
reproducer records (#910), and the lightweight one-shot path (#937) are all still
`needs-design` and not yet built (#910 is blocked by #909). Closing #936 therefore
hands its gap to that tracked, higher-priority cluster — it does not assert the
reproducer workflow exists today. Console remains output-only, read through
`artifacts.{list,get,search_text}`. #936 is closed as resolved-by-redirect, not
built.

This decision is directional: it has no implementing code. It governs the
already-landed read-only-console and SSH-access architecture, so it is Accepted on
merge of this ADR.

## Consequences

- **Easier.** The MCP tool count stays contained ahead of the pending top-level
  tool review (the explicit ADR-0273 constraint). Console keeps a single
  invariant — output-only, always redacted, served by one generic
  `artifacts.*` reader — with no new stateful session resource, no console write
  path to authorize and redact, and no transport concern added to MCP's
  request/response surface. The reproducer capability accrues to #909/#910, whose
  designs return structured exit status and Run-correlated artifacts that a raw
  console byte stream does not.

- **Harder / residual risk.** The redirect assumes the SSH/command-execution path
  reaches the guest. It does not cover the cases where guest networking is absent:
  early boot before the network is up, a network-down reproducer, and a
  panicked/halted guest where only the console is live. For those, the host-local
  `virsh console` workaround #936 describes remains the only path until a deliberate
  capability lands. This is the falsifiable cost of the decision.

- **Reopen condition.** #936 should be reopened — superseding this ADR — if a
  concrete, recurring need for *interactive* in-guest I/O with **no guest
  networking** is demonstrated (for example, driving a reproducer or recovery
  sequence at a panic/early-boot prompt that SSH cannot reach). Absent that
  evidence, the no-network interactive case stays out of scope.

- **Obligations.** None on the codebase. The #909/#910/#937 issues carry the
  reproducer-execution work and are tracked independently; this ADR does not
  schedule them. The issue is annotated with this resolution.

## Alternatives considered

- **Add `console.attach/send/read/detach` with a persisted transcript (#936
  option b).** A full interactive console tool family. Rejected: it overturns
  ADR-0273 to add a stateful session resource over MCP's request/response surface
  (the shape ADR-0273 rejected as the heaviest option), introduces the first
  agent-driven console write path to authorize and redact, and grows the tool
  surface against the pending tool review — all to serve a reproducer need that
  #909/#910 already own and serve better with structured, Run-correlated results.

- **A narrower stateless `console.send` plus existing `artifacts.*` reads (#936
  option c).** Inject bytes to the guest console without a session lifecycle.
  Rejected: it still adds the first console write path and a new provider
  console-write seam, still grows the tool surface, and still duplicates the
  reproducer outcome #909 delivers — for the marginal gain of the no-networking
  case, which the reopen condition above covers if it proves real. The cost is not
  justified by present evidence.

- **Build the interactive console as specified now, ahead of the exec path.**
  Treat #936 as the primary fix and let #909/#910 follow. Rejected: #909/#910 are
  `priority:high` and #936 `priority:medium`; the high-priority path subsumes the
  common reproducer case (networked guest) and produces better evidence, so
  leading with the medium-priority console tool would duplicate effort and add
  surface that the exec path makes redundant.
