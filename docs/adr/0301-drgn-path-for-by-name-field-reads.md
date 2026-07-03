# ADR-0301: document the drgn path for by-name struct-field reads (#991)

- Status: Accepted
- Date: 2026-07-02
- Builds on [ADR-0034](0034-debug-plane-gdbmi-tier.md) (the gdb-MI tier and its narrow
  command surface), [ADR-0248](0248-gdbstub-symbol-resolution.md) (`resolve_symbol` as
  `&<identifier>` only), and [ADR-0240](0240-live-drgn-script-introspection.md)
  (`introspect.script`, arbitrary in-guest drgn). Extends the agent-facing doc system of
  [ADR-0284](0284-agent-facing-workflow-docs.md). Respects
  [ADR-0270](0270-no-adr-refs-in-agent-surface.md).

## Context

`debug.resolve_symbol` resolves exactly one expression form — `&<identifier>`, a symbol's
address. The gdbstub `debug.*` family evaluates no member, array, or type-aware expression, so
an agent cannot read `some_struct->field[3].member` by name on that path (#991).

This is deliberate, not an oversight. Every public `debug.*` op passes only a gated bare
identifier or an engine-constructed numeric expression to gdb's `-data-evaluate-expression`;
agent-supplied text never reaches gdb's expression parser. That non-injectability is the
property `resolve_symbol`'s narrowing to `&<identifier>` and the breakpoint-location gate both
rely on. It matters because `gdb` runs on the KDIVE **host** (the worker process), attached to
the guest's RSP over loopback — arbitrary expression text would be attacker-controlled input to
a host-side process in a multi-tenant service, and gdb's evaluator is not a pure reader (it can
assign, attempt inferior calls, and reach convenience functions).

The capability an agent actually needs already exists on the drgn path.
`introspect.script` (ADR-0240) runs an arbitrary drgn program **in-guest** (over the
guest-agent/SSH transport), and drgn reads typed kernel objects by name
(`prog["some_struct"].field[3].member`). Because that script executes inside the guest VM the
caller already controls, its blast radius is that guest, not the host — a categorically
different posture from feeding gdb arbitrary text on the host.

So the gap #991 names is purely routing: neither the `debug` nor the `introspect` agent-facing
guide told an agent that by-name member/array reads go through drgn, not `debug.*`.

## Decision

Document the routing. No new tool, engine op, schema, migration, RBAC, or config change.

1. `docs/guide/toolsets/debug.md` — the "Inspecting state" section states that
   `debug.resolve_symbol` yields an address only and the gdbstub path evaluates no
   member/array/type expressions, and routes a by-name struct-field/array-member read
   (`some_struct->field[3].member`) to the drgn path (`introspect.script`).
2. `docs/guide/toolsets/introspect.md` — `introspect.script` explicitly names the by-name
   field/member read as its supported use, with the concrete `some_struct->field[3].member`
   shape and the drgn `prog[...]` idiom.
3. `debug.resolve_symbol`'s parameter description points member/array reads at the drgn path at
   the point of confusion, matching the confusable-tool precedent (the mis-sequence-prone tools
   name their specific alternative).
4. A content-presence guard ties the routing prose in both served snapshots to CI so a later
   doc edit cannot silently drop the deliverable while the completeness and snapshot guards stay
   green.

## Consequences

An agent has a documented, working way to read `some_struct->field[3].member` by name on a live
guest (the #991 acceptance), and the gdbstub non-injectability invariant is untouched.

**Revisit trigger.** Reconsider a bounded, injection-safe evaluate op on the gdbstub family if a
real functionality gap opens on the drgn path — e.g. drgn lacking support for a target
architecture (non-x86), or a deployment where the in-guest drgn transport is unavailable while a
gdbstub session is not. Until such a gap is demonstrated, drgn is the supported answer.

## Alternatives considered

- **Add a `debug.evaluate` gdbstub op (issue option 1).** Rejected: it reverses the deliberate
  no-arbitrary-expression invariant and feeds agent-controlled text to a host-side gdb parser
  (injection surface, plus redaction of arbitrary reads) to duplicate a capability drgn already
  provides in-guest-sandboxed — a poor trade for a Tier-4 nice-to-have.
- **Document in only one guide.** Rejected: an agent may enter from either the `debug` or the
  `introspect` guide, or from `resolve_symbol` itself; a single pointer leaves the other entry
  points a dead end.
- **No content guard.** Rejected: the routing prose is the entire deliverable, and the existing
  completeness/snapshot guards would stay green if a refactor dropped it, silently reopening
  #991.
