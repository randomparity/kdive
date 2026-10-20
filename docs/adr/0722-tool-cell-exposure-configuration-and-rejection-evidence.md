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

The `recovery` configuration exists only when the server runs with a durable worker-death
verifier: `build_plane_registrars` registers the build-use recovery tools only when
`assembly.worker_death_verifier` is set (`src/kdive/mcp/assembly/tool_registration.py`), and
`build_process_assembly` keeps only the `docker` and `kubernetes` verifiers
(`src/kdive/assembly.py`, `_durable_worker_death_verifier`), so `local` leaves the tools out.
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
   recovery lane is the same stack started with `KDIVE_WORKER_DEATH_VERIFIER=docker`. Its Docker
   death endpoint is not reachable on the host-process lane, so a recovery-tool call there cannot
   prove a worker dead and fails closed; the tool cells here never make one. Both lanes write into
   one evidence root, which is assembled once. A missing issuer fails the run rather than skipping
   it, because without one the configuration cannot be read.
3. **Rejections.** One harness proves every boundary:
   - `authentication`: a token carrying the cell's claims but signed by a key the server does
     not trust is sent as a raw `tools/call`, to the tool (`direct`) or to `tools.invoke`
     (`gateway`), and must receive HTTP 401, while the same request with the issued token must
     not;
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

### Amendment (2026-10-02): a gateway validation rejection is the binding failure (#3103)

This narrows decision 3's `validation` rule for `gateway`. `tools.invoke` answers
`configuration_error` for causes that are not schema validation: an unknown or hidden tool, an
inner handler's error, and a tool body re-validating its own data. None of them is validation
evidence. A `gateway` validation cell therefore requires `tools.invoke`'s argument-binding
failure: a `configuration_error` envelope carrying a non-empty `data.field_errors` and a detail
naming a failed schema validation (`src/kdive/mcp/tools/gateway.py`). The cell's own token can
see the tool, so the binding failure carries `data.field_errors`. The `direct` rule is unchanged.
`systems.provision`, `systems.reprovision`, `runs.create` and `allocations.request` are the
exception: `BindingErrorMiddleware` (`src/kdive/mcp/middleware/binding_errors.py`) re-envelopes
their binding failures before `tools.invoke` sees them, without `data.field_errors`, so a
`gateway` validation cell for one of them fails under this rule until a later decision names
its evidence.

### Amendment (2026-10-12): three filtering list boundaries prove denied visibility (#3108)

This is an amendment because a later decision supersedes part of decision 3 for three
named cells only. [ADR-0756](0756-filtered-list-boundary-evidence.md) replaces the
failure-envelope requirement for `investigations.list` `authorization`, `investigations.list`
`project-isolation` and `artifacts.list` `project-isolation`. These tools filter the caller's
view instead of failing. Their cells prove denial with a positive control. First, a viewer
sees an independently known protected item. Then the denied caller gets the tool's canonical
empty page in the same exposure. Protected-state equality and cleanup still apply. Every other
`authorization` and `project-isolation` cell keeps the failure-envelope rule. The outcome
`rejection` is unchanged.

### Amendment (2026-10-13): a capability cell follows the provider's support verdict (#2815)

This amends the boundary list in the Context and decision 3, by operator decision R2
(2026-10-13). The contract added a `capability` rejection cell to every capability-gated tool
whatever the provider's verdict. Both libvirt providers support every gated capability, so
those cells demanded a refusal no run can produce. A capability-gated tool's cells now follow
their final verdict: the `ProviderSupport` verdict `_capability_variants` derives, after
`_tool_cells` applies the `ArchTraits` fadump override:

- **Supported.** The functional cell is the proof. No `capability` cell is generated.
- **Unsupported.** The `unsupported` cell keeps its reviewed rule, and a `capability`
  rejection cell proves the refusal. A real-issuer token with the cell's grants must receive
  ADR-0209's refusal: a `configuration_error` envelope whose `data.reason` is
  `capability_unsupported`. `configuration_error` alone also answers schema and name failures,
  so it is not capability evidence. Protected state and cleanup follow decision 4, and the
  outcome is `rejection`. The refusal comes from the tool's own admission, so the cell first
  meets that tool's other preconditions: `vmcore.fetch` checks the CRASHED state before the
  capture method, so its x86_64 `fadump` cell needs a crashed, Run-bound System (#2814).

Cell identities and the other boundaries are unchanged. A provider that gains or loses a
capability changes the cell set, which moves the contract digest, so a qualification against
the old set fails `matrix-mismatch` rather than passing stale evidence.

### Amendment (2026-10-14): ten more filtering list boundaries prove denied visibility (#2813)

This amends the 2026-10-12 amendment above, which names three cells and keeps the
failure-envelope rule for every other `authorization` and `project-isolation` cell. By
operator decision R3 (2026-10-13), [ADR-0756](0756-filtered-list-boundary-evidence.md)'s
2026-10-14 amendment adds the `authorization` and `project-isolation` pairs of `runs.list`,
`systems.list`, `jobs.list`, `allocations.list` and `debug.list_sessions`. Their cells prove
denial with ADR-0756's positive control and canonical empty page. The contract's
`FILTERED_LIST_BOUNDARIES` owns the named set; every pair outside it keeps the
failure-envelope rule.

### Amendment (2026-10-14): recovery evidence is the witness's termination row (#3111)

This corrects decision 2 and the first Consequence. Decision 2 says a recovery-tool call on the
host-process lane "cannot prove a worker dead". The Consequence says a carrier of the recovery
tools needs a lane whose death verifier can reach its authority. Both assume that
`ops.recover_build_use` asks the configured verifier. It does not:

- `KDIVE_WORKER_DEATH_VERIFIER` only decides whether the two tools are registered. No caller
  invokes `verify_dead`.
- `recover_investigation_build_use` (`src/kdive/db/schema/0108_worker_fence_runtime_paths.sql`)
  accepts only one fact as death evidence: a durable `worker_incarnations` row in state
  `terminated` for the exact holder on the pin.
- The systemd lifecycle witness writes that row on the demo-up lane when a worker unit's
  invocation dies (`worker-lifecycle.sh status`).

The functional cells of both recovery tools therefore run on the demo-up `recovery` lane
(`KDIVE_WORKER_DEATH_VERIFIER=docker`). They strand a real pin under a frozen worker unit,
observe the tool while that holder lives, kill the unit, and let the witness publish the
termination. The worker there has a `local` incarnation, and the recovery row records
`authority_kind = 'local'`. Docker and Kubernetes incarnations write the same row through their
own witnesses, so the same evidence rule covers them. The configuration proof and the other
decisions are unchanged.

### Amendment (2026-10-20): a variant the tool's handler refuses is unsupported (#3114)

This amends the 2026-10-13 amendment's verdict. `_capability_variants` turns every advertised
capture method into a `vmcore.fetch` variant. `remote-libvirt` advertises `console` and `gdbstub`,
but the handler admits only its core-producing set (`VMCORE_METHODS`). It answers any other method
with `configuration_error`, `data.reason` "method does not produce a vmcore", and no job. That
refusal does not depend on the provider or the architecture. A `vmcore.fetch` variant outside that
set is therefore `unsupported`, with the handler as its `unsupported_reason`, on every lane. This
covers the x86_64 and ppc64le `remote-libvirt` `console` and `gdbstub` cells.

The refusal is not ADR-0209's `capability_unsupported`, so the verdict generates no `capability`
rejection cell. The `unsupported` cell proves the refusal itself. It first meets the handler's
preconditions: a crashed, Run-bound System. Then a real-issuer `contributor` token in the cell's
exposure must receive exactly that envelope. The System must stay `crashed`, and the jobs naming
the System or Run must be unchanged. Cell identities, owners and the other boundaries are
unchanged. A change to `VMCORE_METHODS` moves the cell set and the contract digest.

## Consequences

- Recovery-configuration evidence exists only from a run whose catalog proved it. A carrier of
  the recovery tools themselves (#2812) needs a lane whose death verifier can reach its authority.
- A run that cannot prove its configuration records nothing, so `qualify` reports `missing-result`.
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
