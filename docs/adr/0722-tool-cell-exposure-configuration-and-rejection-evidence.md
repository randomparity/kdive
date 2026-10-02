# 0722 — Tool-cell exposure, configuration proof and rejection evidence

## Status

Accepted (2026-10-02)

## Context

The ADR-0686 contract crosses every registered tool with two server configurations (`default`,
`recovery`) and two exposures (`direct`, `gateway`), and adds a rejection cell per applicable
boundary: `authentication`, `authorization`, `project-isolation` and `validation`
(`scripts/coverage_campaign/contract.py`, `_tool_cells`). One pytest node is bound per scenario
and must cover every configuration and exposure cell of it. ADR-0715 fixes how a live carrier
binds evidence to the deployed build; it says nothing about what a tool cell's configuration,
exposure or rejection is. #2811 is the first tool-cell carrier, and #3095-#3098 reuse its
harness, so the meaning has to be fixed once.

The `recovery` configuration exists only when the server runs with a worker-death verifier:
`build_plane_registrars` registers the build-use recovery tools only when
`assembly.worker_death_verifier` is set (`src/kdive/mcp/assembly/tool_registration.py`).
`KDIVE_WORKER_DEATH_VERIFIER` defaults to `disabled`, and the demo-up lane does not set it.

## Decision

1. **Exposure.** `direct` is a real-issuer token whose `azp` is the configured `kdivectl` client
   id, which the server resolves to the operator-direct profile; the cell calls the tool by
   name. `gateway` is a real-issuer token without that `azp` (the agent-gateway profile); a
   functional cell first finds the tool with `tools.search(names=[tool])` and then calls it
   through `tools.invoke`. `tools.search` and `tools.invoke` are reached through `tools.invoke`
   the same way.
2. **Configuration proof.** Before a cell runs, a `platform_operator` operator-direct catalog is
   read once per stack. Both `ops.build_uses_list` and `ops.recover_build_use` listed means
   `recovery`; neither means `default`; one alone fails the run, and so does a catalog clipped to
   the gateway's core tools, which shows the token was not taken for `kdivectl`. A cell of the other
   configuration is skipped and writes no record. The proof is stored as an artifact on each
   record, not as an assertion, because the contract fixes each cell's assertion set. The
   recovery lane is the same stack started with `KDIVE_WORKER_DEATH_VERIFIER=local`. Both lanes
   write into one evidence root, which is assembled once.
3. **Rejections.** One harness proves every boundary:
   - `authentication`: a token carrying the cell's claims but signed by a key the server does
     not trust is sent as a raw `tools/call`, to the tool (`direct`) or to `tools.invoke`
     (`gateway`), and must receive HTTP 401;
   - `authorization` and `project-isolation`: a real-issuer token with the grants the cell
     supplies must receive a failure envelope whose category is in the cell's closed set;
   - `validation`: schema-invalid arguments must receive a `configuration_error` envelope, or,
     on `direct` only, a tool-error result whose text names a validation error.

   A completed rejection cell records outcome `rejection`, which `qualify` requires of its kind
   (`scripts/coverage_campaign/results.py`); a functional cell records `success`.
4. **Protected state.** The default snapshot is a per-project digest: row count and row-text
   hash of every `public` table with a `project` column, except `audit_log`,
   `platform_audit_log` and `tool_invocation`, which a rejected or successful call is expected
   to write. The tables come from `pg_catalog`, so one the evidence DSN cannot read fails the
   snapshot rather than dropping out of it. A rejection cell's `unchanged-state` and `cleanup` both compare that snapshot with
   the one taken before the call. A cell may replace the snapshot with a narrower one.

## Consequences

- Recovery-configuration evidence exists only from a run whose catalog proved it. A run that
  cannot prove its configuration records nothing, so `qualify` reports `missing-result`.
- A rejection that writes a denial audit row still passes; any other durable write to the cell's
  project fails it. Writes outside the project are not observed.
- A read-only functional cell proves `cleanup` by the same unchanged snapshot. A carrier whose
  tool creates durable state must prove its own cleanup.
- Changing the gateway contract (ADR-0268) or the operator CLI client-id rule changes what these
  cells mean and needs an amendment here.

## Considered & rejected

- **Read `KDIVE_WORKER_DEATH_VERIFIER` from the test's environment.** judgment: the variable says
  what the shell asked for, not what the server loaded; the catalog is what the configuration
  changes and the only one of the two the server reports.
- **Record the other configuration's cells as `blocked`.** verified:
  `EvidenceWriter.record` replaces an earlier record for the same cell
  (`tests/integration/live_stack/evidence.py` at `9e322375d`), so a blocked record from the
  second lane would overwrite the first lane's success.
- **Call core tools directly by name in the gateway exposure.** judgment: the exposure would mean
  different things for core and non-core tools, and `tools.invoke` is what an agent uses.
- **Use an expired real-issuer token for authentication.** judgment: `mint_token(lifetime_s=-60)`
  could mint one, but it tests the expiry check of a trusted signature; a foreign signature tests
  that the server refuses claims it did not issue, the boundary an unauthenticated caller crosses.
- **A separate pytest function per scenario.** judgment: the contract binds many scenarios to one
  node, as the deep-lifecycle cells already do, and one dispatching function avoids fourteen copies
  of the same frame.
