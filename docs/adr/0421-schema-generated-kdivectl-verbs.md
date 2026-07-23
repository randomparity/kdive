# ADR 0421 — Schema-generated `kdivectl` verbs and canonical tool naming (#1443)

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** kdive maintainers
- **Issue:** [#1443](https://github.com/randomparity/kdive/issues/1443) (epic #1442).
- **Narrows:** [ADR-0107](0107-cli-mutating-tool-call-opt-in.md) — relaxes its rejected
  alternative "grow curated verbs to cover everything is an unbounded hand-maintenance
  burden": the objection was to *hand*-maintenance, and schema generation makes full
  coverage O(1) to maintain. The tiered `tool call` opt-in ADR-0107 actually decided is
  unchanged; only that rejected-alternative's premise is narrowed.
- **Extends:** [ADR-0089](0089-operator-cli-mcp-client.md) — its curated-verb model
  (decision 2b) becomes *derived-path handler overrides*: the verb set is generated from
  the live tool schemas, and a curated verb overrides the handler for its already-derived
  canonical path, never a second path.
- **Supersedes in part:** [ADR-0250](0250-ledger-report-cli-verbs.md) (Accepted) on two
  points — (a) its Decision hard-names `ledger report-all` / `ledger report-granted`, which
  canonical naming retires; (b) its projected `{"items", "totals"}` `--json` shape, chosen so
  "a server-side `data` addition cannot silently change the scriptable contract"; this ADR
  answers that projection rationale directly (decision 6) rather than silently reversing it.
- **Builds on (does not supersede):** [ADR-0047](0047-agent-facing-tool-guide-generation.md)
  (the `ToolAnnotations` the tier classifier reads), [ADR-0268](0268-tool-gateway-dispatcher.md)
  (the `tools.search` / `tools.invoke` gateway whose canonical CLI form Q1 resolves).

## Context

`kdivectl` reaches the MCP surface two ways (ADR-0089): a generic `tool call <name>`
passthrough for any tool, and a hand-written registry of *curated verbs*
(`src/kdive/cli/commands/registry.py:55`) that wrap one MCP tool each in a typed,
argument-validated, table-rendered subcommand. The curated set is the operator-ergonomic
front; the passthrough is the escape hatch.

The curated set does not scale. ADR-0107's "Considered & rejected" rejected growing curated
verbs to cover the whole tool census — "91 tools (growing) is an unbounded hand-maintenance
burden". It is now **141 registered tools against 28 curated verbs**. Every new tool either
gets a hand-written verb (a standing maintenance tax) or is reachable only through the raw
passthrough (no typed arguments, no rendering, no help). The gap widens monotonically.

Two facts make this fixable now. First, the registry already declares, per verb, the exact
MCP tool it calls (`Verb.tool`) — the CLI is a pure MCP client and `list_tools()` returns
every tool's full input schema, output shape, and annotations. The verb *is* a mechanical
projection of a tool schema that we happen to type by hand. Second, the objection ADR-0107
recorded was to hand-maintenance specifically, not to coverage: if the projection is
generated, full coverage costs one build step, not 141 hand-written entries.

But the current curated names are not derivable from their tools, and that is the deeper
problem. `ledger report-all` calls `accounting.report_all_projects`; `allocations
force-release` calls `ops.force_release`; `teardown system` calls `ops.force_teardown`. The
verb path, its namespace, and the tool name disagree, so no rule maps one to the other and
the human-facing name must be looked up. Any generation scheme first needs a *canonical*
name rule, and any name that violates it must be fixed at the source (the tool), not papered
over with an alias.

AGENTS.md requires a superseding ADR rather than an in-place edit of an accepted decision, so
this ADR narrows ADR-0107, extends ADR-0089, and supersedes ADR-0250 in part.

## Decision

**We will generate the `kdivectl` verb tree from the live tool schemas at build time, name
every verb by a single canonical rule with no aliases and no exclusions, and keep curated
handlers only as overrides of their derived canonical path.**

1. **Build-time generation from `list_tools()`, committed artifact, CI drift guard.** A
   build step queries the server's `list_tools()` and emits the verb tree (path, positional
   and option arguments, required-vs-optional, help text) as a **committed artifact** checked
   into the tree. The CLI loads that artifact; it does not call `list_tools()` at startup, so
   the shipped CLI stays offline-parseable and its surface is reviewable in a diff. A **CI
   drift guard** regenerates the artifact against the live schemas and fails if it differs
   from the committed copy — so any tool added, renamed, or reshaped surfaces as a required,
   reviewed artifact change, never as silent CLI drift. Regeneration is the fix; the guard is
   the tripwire.

2. **Canonical path derivation, no aliases, no exclusions.** The verb path is a pure function
   of the tool name: `<ns>.<op>` → `kdivectl <ns> <op-with-dashes>` (underscores in `<op>`
   become dashes; the namespace is the group, the operation is the subcommand). Examples:
   `resources.cordon` → `kdivectl resources cordon`; `accounting.usage_project` → `kdivectl
   accounting usage-project`; `ops.force_teardown` → `kdivectl ops force-teardown`. **Every**
   tool gets its derived verb — no exclusion list, no per-tool opt-out. When a tool's *name*
   makes a bad verb, the fix is to **rename the tool** (a server-side change reflected back
   through generation), not to add a CLI alias or an exclusion. A one-tool-to-one-path
   function with no exceptions is what makes the artifact reviewable and the mapping
   memorable; every alias or exclusion is a special case an operator must learn and a
   maintainer must carry.

3. **Curated verbs become handler overrides for their derived path, never a second path.**
   A hand-written handler (nicer argument shaping, bespoke rendering, a totals footer) is
   retained by binding it to the **derived canonical path** of the tool it wraps — it
   overrides the generated handler in place. It never introduces a second path or an alias
   for the same tool. Concretely: the `resources.cordon` / `resources.drain` handlers already
   sit on their canonical paths and simply become overrides; the `ops.force_teardown` handler
   moves from the non-canonical `teardown system` to the canonical `kdivectl ops
   force-teardown`; the `ops.force_release` handler moves from `allocations force-release` to
   `kdivectl ops force-release`. The pre-canonical paths are retired, not aliased.

4. **Verb-name-as-acknowledgement for mutating verbs; destructive verbs keep the typed-`yes`
   confirm.** A derived verb is a named, argument-typed operation, not the generic `tool
   call` the ADR-0107 tier flags guard. For a **mutating** (non-destructive) verb, *running
   the named verb is itself the acknowledgement* — `kdivectl resources drain <id>` needs no
   `--allow-mutating` flag, because the operator has already named the mutation and cannot
   fat-finger a read into it the way a bare `tool call` allows. For a **destructive** verb,
   the verb name is necessary but not sufficient: it additionally keeps the ADR-0107
   interactive **typed-`yes`** confirm (suppressible only by `--yes` for non-TTY automation).
   This mirrors the ADR-0107 gradient — the strongest ceremony is reserved for the highest
   blast radius — but applies it to first-class verbs instead of the passthrough.

5. **Tier is classified from live server annotations, never from the generated artifact.**
   Whether a verb is read-only, mutating, or destructive — which decides the decision-4
   ceremony — is derived at call time from the tool's live MCP `ToolAnnotations` via
   `classify_tool` (`src/kdive/cli/passthrough.py:45`), exactly as the passthrough does today.
   The generated artifact records *paths and arguments*, not tiers. A tool re-annotated from
   mutating to destructive tightens the confirm on its next call without regenerating the
   artifact; the artifact must never become a second, staleable source of truth for
   authorization ceremony. (The server-side destructive-op gate, ADR-0006/0020, remains the
   real boundary regardless; the client ceremony is a UX guard.)

6. **`--json` emits the server envelope verbatim; the drift guard is the projection ADR-0250
   wanted.** ADR-0250 hand-projected each report verb's `--json` onto a declared
   `{"items", "totals"}` key set specifically so "a server-side `data` addition cannot
   silently change the scriptable contract." Under generation there is no per-verb place to
   hand-write such a projection, and 141 hand-maintained projections is the same tax this ADR
   retires. **We answer that rationale rather than reverse it:** `--json` emits the tool's own
   `structured_content` envelope verbatim, and the *scriptable contract is the tool's
   published output schema*, whose stability is enforced by the decision-1 committed artifact
   and drift guard. A server-side `data` addition changes the tool's output schema, changes
   the generated artifact, and **fails the drift guard until reviewed and re-committed** — the
   exact "cannot silently change the contract" property ADR-0250 sought, relocated from a
   lossy per-verb projection to a single generation gate. This is strictly more faithful
   (scripts see everything the tool returns, not a subset the CLI froze) and strictly less
   hand-maintenance, and it does not reintroduce silent drift because the guard, not a
   projection, is the tripwire. Curated handlers may still render a human table with a totals
   footer; that is a *rendering* concern for the default (non-`--json`) output and does not
   fork the scriptable contract.

7. **Q1 — `tools.search` / `tools.invoke` under canonical derivation.** Confirmed against the
   live surface, the gateway tools (ADR-0268) are named `tools.search` and `tools.invoke`.
   They are ordinary tools in the `tools` namespace, so under decision 2 they derive to
   `kdivectl tools search` and `kdivectl tools invoke` — no exclusion, no special case. They
   are, however, **redundant-by-construction for the CLI**: `tools.invoke` is an LLM-facing
   progressive-disclosure dispatcher for clients that see only a demoted `tools/list`, whereas
   generation already gives every inner tool its own first-class derived verb, so an operator
   reaches the inner tool directly rather than through `kdivectl tools invoke <name>`. Per the
   no-exclusions rule they are kept as derived verbs anyway; the CLI's coverage does not depend
   on them, so a future gateway rename is absorbed by regeneration like any other.

8. **Q2 — the two `force` parameters under verb-name-as-acknowledgement.** Both curated
   break-glass verbs carry a `--force` flag today: `teardown system` (→ `ops.force_teardown`)
   and `allocations force-release` (→ `ops.force_release`). Both underlying tools are annotated
   `destructive()`. Under decision 2 they canonicalize to `kdivectl ops force-teardown` and
   `kdivectl ops force-release` — the `force-` in the verb name is the path-level break-glass
   acknowledgement (decision 4), and because both classify **destructive** (decision 5) each
   additionally keeps the ADR-0107 typed-`yes` confirm. The boolean **`--force` flag is
   therefore retired**: it was a redundant third acknowledgement now covered by naming the
   `force-` verb plus typing `yes`. This removes the ADR-0107 curated-verb `--force`
   duplication and unifies the break-glass ceremony on one gradient.

## Consequences

- **Coverage is O(1) to maintain.** A new tool gets a typed, rendered, helped verb with no
  hand-written registry entry; the curated set shrinks to genuine ergonomic overrides.
- **The verb name is now derivable from the tool name and vice versa.** An operator or agent
  can predict `kdivectl <ns> <op>` from a tool name and read a tool name off a verb, removing
  the lookup the disagreeing legacy names forced.
- **Breaking CLI renames land for the misnamed verbs.** `teardown system`, `allocations
  force-release`, `ledger show`, `ledger report-all`, and `ledger report-granted` are retired
  in favour of their canonical paths (`ops force-teardown`, `ops force-release`, `accounting
  usage-project`, `accounting report-all-projects`, `accounting report-granted-set`). This is
  a deliberate pre-1.0 break; the implementing PR must document the renames.
- **New obligations:** a generation build step, a committed verb artifact, and a CI drift
  guard become part of the release surface; a tool rename is now also a CLI-surface change
  reviewed through the artifact diff.
- **The generated artifact is not an authorization source.** Tiers stay live-classified
  (decision 5); the artifact carrying stale annotations could never loosen a confirm.
- **Follow-on ADRs are not forced**, but a tool-rename policy (which of the disagreeing names
  are the tool's fault vs. the verb's) will want to be recorded as the renames land.
- **This ADR is directional (no single implementing PR).** It is Accepted as the decision of
  record; the generation pipeline, artifact, drift guard, and renames land in follow-up
  implementation issues under epic #1442.

## Alternatives considered

- **Keep hand-writing curated verbs (ADR-0107 status quo).** Rejected: the 141-vs-28 gap is
  the maintenance tax ADR-0107 named; generation removes the tax without removing the typed,
  rendered ergonomics.
- **Generate, but keep aliases for the legacy names.** Rejected: aliases reintroduce the
  many-names-one-tool ambiguity generation exists to remove, and each alias is a special case
  an operator must learn. Where a name is wrong, rename the tool.
- **Allow a per-tool exclusion list.** Rejected: an exclusion is an invisible hole in coverage
  and a second place to look; the no-exclusions rule is what makes the artifact a complete,
  reviewable census.
- **Classify tiers from the generated artifact (avoid a live `list_tools()` at call time).**
  Rejected: it makes the artifact a staleable second source of truth for the confirm ceremony;
  a re-annotated destructive tool must tighten immediately, not at the next regeneration.
- **Keep ADR-0250's per-verb `{"items", "totals"}` projection under generation.** Rejected:
  there is no per-verb place to hand-write 141 projections, the projection hides fields the
  tool actually returns, and the drift guard delivers the same "no silent contract change"
  property across every verb at once (decision 6).
- **Regenerate the artifact at CLI startup instead of committing it.** Rejected: it makes the
  shipped surface depend on a reachable server, un-reviewable in a diff, and non-deterministic
  across server versions; the committed artifact plus drift guard keeps the surface offline and
  auditable.
- **Exclude the `tools.invoke` / `tools.search` gateway from derivation** (since the CLI does
  not need them). Rejected: it is exactly the special-case exclusion decision 2 forbids; they
  derive like everything else and are simply unused by the CLI's own coverage (Q1).
