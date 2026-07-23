# ADR 0422 — `force` tool parameters keep their name under generated verbs

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-22
- **Deciders:** kdive maintainers
- **Amends (does not supersede):** [ADR-0421](0421-schema-generated-kdivectl-verbs.md) — completes
  its decision 8, which retired the break-glass `--force` **CLI flag** on the two curated
  destructive verbs but did not decide what happens to the two tool **parameters** also named
  `force` that epic #1442 R2/Q2 named. ADR-0421's decisions 4 and 5 (mutation/destructive
  ceremony and live-annotation tier) and decision 8 (flag retirement) stand unchanged.
- **Issue:** #1445 (epic #1442)

## Context

Epic #1442 R2 requires that no generated CLI verb expose a flag colliding with a reserved CLI
flag name, and its Q2 asked specifically what becomes of the two tool parameters named `force`:

- `resources.deregister` — `force: bool = False` (`src/kdive/mcp/tools/ops/resources/deregister.py:63`);
  the tool is **destructive**, and `force=True` means "deregister a resource *despite* its live
  allocations" — a server-side domain precondition the handler checks
  (`deregister.py:129 if live and not force: refuse`).
- `runs.boot` — `force: bool = False` (`src/kdive/mcp/tools/lifecycle/runs/steps.py:210`); the
  tool is **mutating**, and `force=True` recycles a settled `boot` step so a fresh boot of the
  same installed variant runs without a re-stage (#1063). Absent it, a repeat call replays the
  prior job (`data.replayed=true`).

ADR-0421 decision 8 is headed "the two `force` parameters," but its body addresses a *different*
`force`: the boolean `--force` **CLI flag** carried by the curated break-glass verbs `teardown
system` (→ `ops.force_teardown`) and `allocations force-release` (→ `ops.force_release`). It
retired that flag, because the `force-` in the canonical verb name plus the destructive typed-`yes`
confirm already carry the break-glass ceremony. That reasoning is sound and unchanged. But it left
the two *tool parameters* above — the actual subject of R2/Q2 — undecided.

Two facts settle them:

1. **The collision the epic named is gone.** Epic #1442 R2 framed the concern as a collision *in
   meaning* — a parameter named `force` reading like `teardown --force`'s break-glass
   acknowledgement rather than a value sent to the server. ADR-0421 decision 8 retired exactly that
   break-glass `--force` flag, so neither the in-meaning collision nor a literal name collision
   remains: the flag names a generated verb may not reuse are now just `--json`, `--help`, and
   `--yes` (the destructive confirm, ADR-0421 decision 4). The `tool call` passthrough tier flags
   `--allow-mutating` / `--allow-destructive` (ADR-0107) live on that passthrough only, not on
   generated verbs. `force` collides with none of these.
2. **The two params carry distinct, tool-owned meaning.** On the destructive `resources.deregister`,
   `--yes` is the CLI destructive confirmation ("I mean to run this destructive verb") while
   `--force` is the *domain* precondition the server enforces ("proceed even though allocations are
   live"). They answer different questions and are not a redundant double-ceremony. On the mutating
   `runs.boot`, `--force` is a plain behavior toggle with no ceremony flag at all.

## Decision

**We will not rename either `force` parameter.** Both derive by ADR-0421's canonical rule to a
`--force` flag on their generated verbs — `kdivectl resources deregister <id> --force` and
`kdivectl runs boot <id> --force` — and keep the tools' own parameter name and documented
vocabulary.

Epic #1442 R2 therefore reduces, for these two, to the **collision guard alone**: a build-time
assertion that no generated verb's derived flags collide with the reserved set `{--json, --help,
--yes}` (with the passthrough tier flags reserved defensively), failing the build with a message
naming the offending tool and parameter if a future tool introduces such a parameter. The guard is
still required — a future parameter named `json`, `help`, or `yes` would genuinely break — but the
two existing `force` parameters need no action.

## Consequences

- **#1445's scope shrinks to a guard.** Combined with ADR-0421 decision 7 (which keeps the
  `tools.*` gateway rather than renaming it to `gateway.*`), #1445 no longer performs any tool or
  parameter rename. It defines the reserved-flag set and adds the R2 collision guard, nothing more.
  The reference sweep the issue anticipated for the `force` params and the gateway namespace does
  not happen.
- **`--force` is a stable, documented flag** on `resources deregister` and `runs boot`; scripts and
  runbooks can rely on it, and it matches each tool's parameter name — preserving the
  name-derivability ADR-0421 is built on.
- **New obligation:** the R2 guard must enumerate the reserved set explicitly and run in `just ci`,
  so the "no shadowing" property is enforced rather than assumed.
- **ADR-0421 decision 8 is clarified, not reversed:** its heading is read as "the two break-glass
  `--force` flags"; the two same-named tool parameters are governed here.

## Alternatives considered

- **Rename `resources.deregister`'s `force` (e.g. `--despite-allocations`), keep `runs.boot`'s.**
  Rejected: there is no collision to remove, and `--yes` / `--force` are not redundant — one is the
  CLI destructive confirm, the other a server-checked domain gate. Renaming would fork the CLI flag
  from the tool's own parameter name for a clarity gain that the tool's `--help` text already
  provides, breaking derivability for no safety benefit.
- **Rename both `force` parameters** so no generated verb ever exposes a bare `--force`. Rejected:
  the same objection at twice the cost, and it bends epic #1442's explicit non-goal — "No renames
  driven by taste alone. A rename must remove a derivability failure, a flag collision, or a
  namespace collision." With the global `--force` flag retired, none of the three triggers applies.
- **Edit ADR-0421 decision 8 in place** to add the parameter resolution. Rejected: ADR-0421 is
  Accepted; the repo records completions of an accepted decision through a separate "Amends (does
  not supersede)" ADR (cf. [ADR-0086](0086-dead-worker-gdbstub-reconciler-reset.md)), keeping the
  accepted text stable and giving #1445 an independently citable decision.
