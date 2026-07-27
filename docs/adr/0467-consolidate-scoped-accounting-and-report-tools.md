# ADR 0467 — Consolidate the scoped accounting and report tools onto discriminated inputs

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1593
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, through the mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) established.
- **Supersedes:** the two-tool scope split in
  [ADR-0043](0043-platform-scoped-rbac-tier.md) §3, [ADR-0212](0212-report-generation-tool.md)'s
  `reports.generate_granted_set` / `reports.generate_all_projects` pair, and
  [ADR-0250](0250-ledger-report-cli-verbs.md)'s two curated report verbs. The **authority split**
  those ADRs describe is unchanged; only its expression as two tool names is superseded.
- **Amends:** [ADR-0007](0007-metering-budgets-admission.md) §6's two usage entry points and
  [ADR-0421](0421-schema-generated-kdivectl-verbs.md)'s generated-verb derivation for a
  union-typed `request`.

## Context

Six tools encoded two binary choices in their names:

| choice | tools |
| --- | --- |
| what to report spend for | `accounting.usage_project`, `accounting.usage_investigation` |
| which projects to roll up | `accounting.report_granted_set`, `accounting.report_all_projects` |
| which projects to render | `reports.generate_granted_set`, `reports.generate_all_projects` |

Each pair shares annotations (`read_only`), execution class (synchronous read), response shape,
and — for the usage pair — authorization. Epic #1576 requirement 5 allows consolidating exactly
that, and `audit.query` already ships the shape: one `@app.tool`, a discriminated union body, and
`isinstance` dispatch to two handlers with different authorization.

The report pairs are the harder case, and the one this ADR has to argue: the granted-set form is
`viewer`-reachable and the all-projects form requires `platform_auditor`. Merging them makes the
tool's authorization **branch-dependent**. Requirement 5 permits that only when it is explicit and
tested.

## Decision

### 1. Three tools, each taking a discriminated model

- `accounting.usage(target)` — discriminated on `target.kind` ∈ {`project`, `investigation`}.
- `accounting.report(request)` — discriminated on `request.scope` ∈ {`granted-set`,
  `all-projects`}; `projects` exists only on the granted-set member.
- `reports.generate(request)` — the same `scope` discriminator, plus `formats`.

The discriminator values reuse the scope vocabulary already written to `platform_audit_log`
(`granted-set`, `all-projects`) and matching `audit.query`, so one word means one thing across the
tool argument, the audit row, and the response `data.scope`.

`request` / `target` is a wrapper object, which [ADR-0372](0372-flat-params-for-mutation-tools.md)
forbids — for **mutation** tools. These are reads, `audit.query` is the read precedent, and a
discriminated union has to be a single parameter to carry its discriminator.

The scope-bearing tools take a **required** argument: there is no default scope. A caller states
which report it wants; the server decides whether it may have it.

### 2. Tool identity was never the authorization gate

This is the argument the merge rests on.

`_TOOL_SCOPES` in `exposure.py` is advisory listing metadata — its own module docstring says so,
and epic #1576 requirement 2 restates it. It decides catalog visibility, never execution. The real
check has always run inside the handler, before any read: `require_platform_role(ctx,
PlatformRole.PLATFORM_AUDITOR)` is the first statement of `_report_all_projects` and of
`generate_all_projects`'s gate block. Removing the separate tool name removes no gate, because the
name was not one.

So the merged tools carry the any-of set `{project_viewer, platform_auditor}`, exactly as
`audit.query` does. A viewer sees the tool listed. A viewer that calls it with
`scope="all-projects"` is refused by the branch, with `authorization_denied` and zero rows.

### 3. `scope` selects a branch; the branch performs its own check

Dispatch is on the **parsed model type** (`isinstance(request, AccountingAllProjectsReportRequest)`),
not on a string compare, and each branch keeps its own gate as its first statement. The argument
is never trusted: it routes, it does not authorize. Input parsing (`group_by`, `window`, `formats`)
still runs ahead of the gate, unchanged from before the merge — it reads nothing and its only
effect is to shape the denial-audit args.

The matrix is tested rather than asserted: every identity — project viewer, contributor, admin;
platform operator, admin, auditor; and an unauthorized token — is driven at **both** scopes, in
`tests/mcp/accounting/test_accounting_report.py` and `tests/mcp/tools/reports/test_generate.py`.
Both files also pin the specific case this ADR exists to answer: a project viewer that *sends*
`scope="all-projects"` lands in the all-projects gate and is denied.

### 4. Cross-project masking is code, not tool identity — so it is untouched

The asymmetry between the two scopes lives in their **resolvers**, and the merge keeps both,
verbatim and separate:

- the granted-set branch resolves from `ctx.projects` (`_resolve_granted_set`,
  `_resolve_granted_targets`) and `require_role(viewer)`s every explicitly named project. It never
  reads the project universe.
- the all-projects branch bypasses `ctx.projects` entirely and reads the universe from SQL
  (`ledger UNION budgets`, plus `systems` and `allocations` for the generated report) — reachable
  only past the platform-role gate.

Unifying them into one resolver keyed on a flag is what would have weakened masking. It is exactly
what this ADR declines to do.

### 5. Audit attribution survives in `scope`, `platform_role`, and `args`

Both branches now audit under one `tool` value, so the remaining columns carry the distinction —
and they already did: `scope` is `granted-set:<sorted targets>` vs `all-projects`, `platform_role`
is `NULL` vs the caller's held roles, and `args.scope` repeats it. Per-branch audit behavior is
preserved unchanged:

- the granted-set audit stays **conditional** (>1 target, or `group_by="principal"` for
  `accounting.report`); the all-projects audit stays **unconditional**;
- an all-projects denial is audited **only if the caller holds ≥1 platform role**
  ([ADR-0043](0043-platform-scoped-rbac-tier.md) §4) — a project-only token's denial is the routine
  non-grant case and recording it would let any authenticated token amplify writes into
  `platform_audit_log` on an openly-callable read.

`platform_audit_log.tool` is plain `text`, with no enum, no CHECK, and nothing keying on a tool
name, so **no migration is needed and none is written**. Historical rows keep their original
`accounting.report_all_projects` / `reports.generate_granted_set` values. The repository is
pre-release; a rewrite of an append-only accountability log to make history look like it always
said something else is a worse outcome than a name change an operator can read in this ADR.

### 6. The six names are removed and survive only as search vocabulary

No aliases, no deprecation period (epic #1576 non-goal). `RETIRED_TOOL_NAMES` gains all six rows,
each pointing at its replacement, so `tools.search` finds the merged tool from an old name. The
three merged tools also gain `TOOL_KEYWORDS` for the money vocabulary their names do not spell —
`spend`, `cost`, `billing`, `chargeback`, `export` — so an agent that describes the intent rather
than the name lands there too.

The `reports` namespace keeps its `NAMESPACE_TOC` entry (`reports.generate` still lives there),
reworded from "Generated usage and accounting report retrieval" to describe generation rather than
retrieval.

### 7. One curated CLI verb per merged tool, at the merged tool's generated path

`kdivectl`'s parser only emits a curated verb whose path collides with a schema-generated one, so
a curated verb left at a retired tool's path would silently vanish. The two report verbs collapse
into `kdivectl accounting report --scope granted-set|all-projects` (`--scope` required, `--projects`
valid only with the granted set), and `accounting usage-project` becomes `kdivectl accounting usage`
taking exactly one of `--project` / `--investigation-id` — which also preserves the
investigation read the generated `accounting usage-investigation` verb used to serve.

### 8. A union-typed `request` derives to `--request-json`, not to nothing

`scripts/gen_cli_verbs.py` flattened a `request` wrapper's object body into flags. A discriminated
union has no object body, and the generator's `None` branch collapsed it to *zero* flags and zero
JSON params — a verb with no way to pass its required argument. `reports.generate` has no curated
verb, so shipping the merge without this fix would have deleted its CLI path.

The generator now keeps such a parameter as the whole-parameter `--request-json` escape. This also
repairs `audit.query`, whose generated verb has been argument-less since it adopted the same shape.

## Consequences

- The physical registry drops from 128 tools to 125.
- No authorization boundary moves. Every gate that ran before runs now, in the same place, with the
  same audit behavior.
- `accounting.report` and `reports.generate` are now **listed** to a project viewer that could not
  see `*_all_projects` before. That is a catalog-visibility change only; the wide scope is refused
  the same way it always was. It is the `audit.query` precedent applied consistently.
- Historical `platform_audit_log` rows carry retired tool names. A query filtering
  `tool = 'accounting.report_all_projects'` reads only pre-merge history; the post-merge equivalent
  is `tool = 'accounting.report' AND scope = 'all-projects'`.
- `kdivectl reports generate` loses the per-scope flags the two generated verbs had and takes
  `--request-json` instead — the same shape `audit query` has. A friendlier curated verb can be
  added later without a tool change.

## Alternatives considered

- **Merge the usage pair but leave the report pairs split**, because their roles differ. Rejected in
  §2: the differing role is enforced inside the handler and always was, so the split bought a larger
  catalog and no isolation. Requirement 5 asks for the branch authorization to be explicit and
  tested, which is cheaper and stronger than a name.
- **Dispatch on the raw `scope` string.** Rejected in §3: the parsed model type is what pydantic
  already validated, and an `isinstance` branch cannot drift from the schema the agent was shown.
- **One resolver taking an `all_projects: bool`.** Rejected in §4: that is the single change that
  would weaken cross-project masking, by putting both the member-scoped and universe-scoped reads
  behind one code path.
- **Migrate `platform_audit_log.tool` to the new names.** Rejected in §5: rewriting an append-only
  accountability log to erase what the caller actually invoked is worse than a documented rename.
- **Give `accounting.report` a default scope of `granted-set`.** Rejected: a default would let an
  under-specified call silently pick an authorization axis, and the whole point of §3 is that the
  caller states the scope and the server judges it.
- **Add `--scope` to a curated `reports generate` verb too.** Rejected as scope this issue does not
  ask for; §8's generic escape keeps the path working, and #1611 tracks the guard for this class.
