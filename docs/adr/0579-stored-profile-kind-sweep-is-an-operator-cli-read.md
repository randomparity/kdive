# 0579 — The stored-profile kind sweep is a read-only operator CLI subcommand

## Status

Accepted (2026-08-25)

## Context

[ADR-0549](0549-profile-policy-declares-its-resource-kind.md) closed the **write path**:
`validate_profile_for_provider` rejects a provisioning profile whose `provider` section is not the
bound Resource's `ResourceKind`, at `systems.provision` and `systems.reprovision`. Its own
Consequences section records what it deliberately did not do:

> This is a write-path fix only, and it repairs nothing already stored. […] Detecting that residue
> is separate work, tracked in issue #1907; nothing here sweeps for it.

The residue is not inert. A fault-inject Resource holding a libvirt-section profile passed
admission completely before ADR-0549 — `FaultInjectProfilePolicy.rootfs_source` returned `None`
and its `validate_profile` was a no-op — and the fault-inject provisioner discards the profile
entirely, so such a System reached `ready`. It then raises a bare `AttributeError` from
`ProviderSection.fault_inject` at first use, on four lanes — the same four ADR-0549 names:
`_op_opt_in` (`mcp/tools/lifecycle/control/registrar.py`), `install_method_for`
(`services/runs/steps.py`), `inert_capture` (`jobs/handlers/runs/boot_evidence.py`), and
`_resolve_capture_method` (`mcp/tools/lifecycle/vmcore/handlers.py`) — each named by the
function defined in the file cited, not by the `ProfilePolicy` method it calls, so an operator
handed the printed list can grep for it and find it. #1907's own body lists the debug-session
lifecycle in place of boot-evidence; that is wrong — the only profile-policy call there is
`drgn_live_seeds_bootstrap_key` (`mcp/tools/debug/sessions/lifecycle.py:445`), and the
fault-inject adapter returns `False` from it without dereferencing anything. The two libvirt
kinds could not reach `ready`, so the expected residue is fault-inject Systems specifically.

**That bounds the candidate population, and it bounds it hard.** The fault-inject runtime "is
opt-in and absent from the default production composition (ADR-0071)" — its own words,
`src/kdive/providers/fault_inject/__init__.py:5-6`, gated by `_fault_inject_enabled` in
`providers/assembly/composition.py:189`. A deployment that never composed it holds no
`fault-inject` Resource, hence no candidate row, hence a sweep that is clean by construction.
The residue is real only where an operator opted the mock provider in. That is the fact that
sets the scale of everything below, so it belongs here rather than in a reader's head.

Issue #1907 asks for **detection, not repair**: a query over `systems` joined through
`allocations` to `resources`, a report naming each mismatch, and a test seeding one mismatched
`ready` System. It leaves the exposure open — "whether the sweep ships as a `just` recipe, a
reconciler pass, or an admin MCP tool" — and asks for the one that matches how other one-off
integrity checks are exposed here. Choosing between them is the decision this record settles.
The remediation policy (cordon, teardown, operator notification) is explicitly not settled here.

## Decision

**The sweep is `python -m kdive verify-profile-kinds`, a read-only operator subcommand that
prints one line per mismatch and always exits `0`.**

It is built the way `verify-project` ([ADR-0256](0256-onboard-target.md)) is built: an impure
reader taking an open connection, a pool-opening wrapper so an unset `KDIVE_DATABASE_URL` raises
`CONFIGURATION_ERROR` before any query, and a pure formatter returning the report text.
Module path, function names, field list and ordering are the spec's to fix, not this record's.

Two things about the report **are** decided here, because each is a contract a reader depends on
rather than presentation:

- **it always exits `0`.** The printed report is the answer. #1907's criteria ask for a query, a
  report, no mutation, and a test; none asks for an exit code. An exit contract is not merely
  unasked-for, it is unbuildable *within this scope*: any usable one would have to distinguish
  rows that can still clear from rows that never will, and which states matter is exactly the
  triage call #1907 defers to "a separate call" — the same call this record declines below when
  it rejects reporting only the live states. Deciding the exit code would decide the triage rule
  by another route.

  The obstacle is authority, not mechanism, and the distinction matters to whoever picks up the
  follow-up below. A gate keyed on live states *would* return to green once those rows were
  repaired; it is buildable. What this change lacks is the sanction to define "live", so the
  follow-up that settles remediation is where an exit contract becomes available — not a later
  rediscovery that one is possible.
- **a non-empty message names the four raising lanes and states that remediation is manual**, so
  the report needs no companion document. The target database is named through the existing
  `redact_database_url`.

**A System is clean only when its stored `provider` is exactly `{<the bound kind>: {…}}`.** The
`WHERE` clause is

```sql
jsonb_typeof(s.provisioning_profile -> 'provider' -> r.kind) IS DISTINCT FROM 'object'
   OR s.provisioning_profile -> 'provider'
        IS DISTINCT FROM jsonb_build_object(r.kind, s.provisioning_profile -> 'provider' -> r.kind)
```

Read it as two halves: the section under the bound kind is not an object, **or** the `provider`
object holds anything besides that one section. The second half is load-bearing rather than
belt-and-braces — the first half alone passes `{"fault-inject": {}, "local-libvirt": {}}` on a
fault-inject Resource as clean, and that row is *worse* than the residue this sweep targets:
`_require_exactly_one_provider` (`profiles/provisioning.py:306-315`) makes it fail
`ProvisioningProfile.parse` outright, so it breaks every parse site rather than the four lanes
above.

The predicate is **total** — no System row can fall out of the scan unreported, which is what
makes a clean result mean anything. Measured on `postgres:17` (17.10 at the time of measurement;
the tag floats and now resolves to 17.11, with identical results), `jsonb -> text` returns SQL
`NULL` for a missing key and for every non-object left operand (array, string scalar, JSON
`null`), and `jsonb_typeof(NULL::jsonb)` is `NULL`. Run over ten seeded profile shapes on a real
join, the clause returns the eight wrong ones — mismatched key, both two-key shapes, JSON-`null`
section, empty object, scalar, array, absent `provider` — and neither correct one.

Totality rests on the **join** as well as the predicate, and that half is a dependency rather
than a proof: `allocations.resource_id` is nullable (`0016_pending_queue.sql`,
`0017_queue_terminal_null_resource.sql` permit `NULL` only for `requested`/`released`/`failed`),
so an inner join would silently drop a System whose Allocation had none. It holds today because
`systems.allocation_id` is `NOT NULL`, a System is only minted against a placed Allocation, and
no path NULLs `resource_id` afterwards. The query is **not** widened for a case nothing can
reach — a `LEFT JOIN` would add a NULL branch to `resource_kind`, a reported field, to cover a
row no code can construct.

**This is an accepted residual, and it is deliberately not guarded.** A test comparing the
join's row count against the fixture's `systems` count would look protective and detect
nothing: a migration relaxing the `resource_id` CHECK adds no such row to a fixture the test
seeds, so both counts stay equal and the suite stays green through exactly the change worth
catching. Stated plainly instead: the sweep's totality depends on `allocations.resource_id`
being non-NULL for every state a System can be bound in, and a migration that relaxes that
narrows the sweep silently.

The **key** under `provider` is not extracted in SQL; the row carries
`provisioning_profile -> 'provider'` back and a pure Python helper renders the label. That label
is a **refinement beyond #1907's criterion 2**, which the raw section value would already
satisfy, and it earns that twice over. It keeps caller-controlled text out of an operator report:
`LibvirtProfile.domain_xml_params` is an agent-supplied `dict[NonEmptyStr, NonEmptyStr]`, and a
hand-edited row's `provider` keys are arbitrary JSON strings, so the renderer maps any key
outside the three `ResourceKind` values to `<unrecognized>` rather than printing its bytes. And
it keeps the report legible where the raw value cannot: a section that is present but not an
object renders `<kind>=<not-an-object>`, because rendering the bare key would print
`profile_section=fault-inject resource_kind=fault-inject` — two identical strings under a header
saying the row is mismatched. The spec holds the full mapping and its measured cases. See the
rejected alternative for why the extraction is not done in SQL.

**It reports every state, and it never writes.** `state` is in the report because #1907 asks for
it; filtering to live states would be the triage call #1907 excludes. Nothing in the module issues
a statement other than that `SELECT`.

## Consequences

- An operator upgrading past ADR-0549 has a one-command answer to "did this deployment mint any
  such System before the check landed" — an empty report wherever the fault-inject provider was
  never composed, which is the default.
- **Nothing here is a gate.** The `verify-project` analogy stops before the exit code:
  verify-project's `1` clears by running `seed-project`, and this sweep has no clearing action at
  all — every state is reported, and #1907 authorizes no repair. That is why the exit is always
  `0`: the sweep informs a human, and a caller that treated it as a pass/fail signal would be
  reading something this record does not offer. A follow-up that settles remediation is what
  could give this a gate contract.
- The report names the affected lanes and states that remediation is not automated, so the output
  is actionable without a second document. When #1907's follow-up decides between cordon,
  teardown, and notification, that decision gets its own record and may reuse
  `scan_profile_kinds` unchanged — it is a read with no policy in it.
- `state` and `resource_kind` are carried as `str`, not `SystemState` / `ResourceKind`. This is a
  residue sweep: parsing a stored value into an enum is a way for the sweep to raise on the exact
  data it exists to find. The column `CHECK` constraints keep the values inside the vocabulary
  anyway.
- The sweep is not reachable over MCP, so an agent cannot run it and `kdivectl` does not grow a
  verb. That is deliberate — see the rejected alternative — and it means the sweep runs only where
  `KDIVE_DATABASE_URL` resolves. Deployment sets it (`deploy/systemd/kdive.env.example`, the Helm
  chart); the source-tree live stack deliberately does **not** (`scripts/live-stack/env.sh`: one
  DSN per database authority, #1929), so there the operator supplies it per invocation exactly as
  `onboard.sh` does for `verify-project`. An unset variable is a configuration error, not a clean
  result.
- **The residual of that choice: this is a cross-project read with no `platform_auditor` gate and
  no `platform_audit_log` row.** The same read served over MCP would owe both
  (`src/kdive/mcp/tools/ops/_reads.py`); the subcommand reports every project's Systems and
  records nothing. Accepted, because possession of `KDIVE_DATABASE_URL` already implies
  unrestricted read access to those rows and the report is served to no principal — the remedy for
  a residual this shape is stating it, not bolting audit machinery onto a CLI that has no
  authenticated actor to attribute it to.
- The sweep reports, it does not watch. A deployment that wants continuous coverage would need the
  reconciler pass rejected below; nothing here schedules anything.

## Considered & rejected

- **Add a new `ops.*` MCP tool, gated `platform_auditor` and read-audited.** verified: this is the
  supported administrative surface under [ADR-0089](0089-operator-cli-mcp-client.md), and it is
  the more expensive one. A new tool must be placed in `src/kdive/mcp/exposure.py`'s exposure map
  and in `_BEHAVIOR_TESTS_BY_TOOL` in `tests/mcp/core/test_tool_docs.py`, and it moves three
  committed generated artifacts: `docs-check` and `cli-verbs-check`, each an individually-gated
  step in `.github/workflows/ci.yml`, and the `rbac-matrix-check` artifact, gated under
  `just test`. A fourth, `doc-constants-check`, does **not** move at today's size:
  `_approx_tool_count` in `scripts/gen_doc_constants.py` is `round(len(_registry_tools()), -1)`,
  the live registry holds 123 tools, and `round(123, -1)` and `round(124, -1)` are both 120 — it
  moves only when the registry crosses a ten. Cross-project reads additionally owe the ADR-0062 §6
  `platform_auditor` gate plus a `platform_audit_log` row per served read
  (`src/kdive/mcp/tools/ops/_reads.py`). judgment: that is a large surface for a report an operator
  runs once after an upgrade, and the audience is the person who runs `migrate`, not an agent.
- **Extend the existing `platform_auditor` `inventory.list` with a kind-mismatch filter.** This is
  the cheap version of the alternative above and has to be priced separately. verified: it needs
  none of the entries the bullet above prices — `inventory.list` is already `_PLAT_AUDITOR` in
  `src/kdive/mcp/exposure.py:186` and already mapped in `_BEHAVIOR_TESTS_BY_TOOL`
  (`tests/mcp/core/test_tool_docs.py:102`) — and its `_fetch_systems` already runs the exact join
  this sweep needs, `systems → allocations → resources`, already selecting `r.kind AS
  resource_kind` (`src/kdive/mcp/tools/ops/inventory/inventory.py:154-175`). A new parameter still
  moves `docs-check` and `cli-verbs-check`, which are schema-derived. verified: it is nevertheless
  the wrong shape for an exhaustive sweep — `inventory.list` clamps each stream with
  `_clamp_list_limit`, orders newest-first, and reports `data.truncated` (ADR-0192), so "are there
  any mismatches in this deployment" becomes a paging exercise whose answer is a `truncated` flag
  rather than a count. judgment: and it requires a bearer token and a running server, which the
  upgrade path this sweep serves may not have yet — the operator has just run `migrate`.
- **Add it to `ops.diagnostics` as a new `Check`.** verified: `CheckResult` in
  `src/kdive/diagnostics/checks.py:52-77` carries `check_id`, a three-state `status`, `detail`, an
  optional `fix`, `provider`, `failure_category`, `resource_id`, and
  `data: Mapping[str, str] | None` ("structured, machine-readable non-secret fields surfaced with
  the verdict"); `__post_init__` raises `a fail result must name a fix` when a `FAIL` carries no
  `fix` (`checks.py:87-89`). Every existing check id in that module — `secret_ref`,
  `provider_tls`, `gdbstub_acl`, `remote_libvirt_reachability`,
  `remote_libvirt_base_image_staging`, `multiarch_gdb`, `pseries_fadump`, `guest_arch_accel` —
  probes an environment or provider contract, not stored rows. judgment: `data` is a flat
  `str → str` map, so a variable-length list of System rows needs an ad-hoc encoding inside it;
  and `fix` is mandatory on `fail` while #1907 leaves remediation open, so the field would have to
  lie or the check would have to report `pass`.
- **Ship it as a reconciler pass.** verified: the reconciler's contract is drift *repair*
  (ADR-0021). `src/kdive/reconciler/loop.py`'s module docstring enumerates what a pass runs —
  allocation expiry, orphaned System, abandoned job, dead DebugSession, leaked libvirt domain,
  idempotency-key GC, and the three image-catalog sweeps — and each of those is a write.
  judgment: #1907 asks for a report and excludes the
  repair decision, so a pass would have nothing to do but log on an interval, and a log line on an
  interval is not a surface an operator can query.
- **Ship it as a checked-in `.sql` file plus a `just` recipe running `psql -f`.** This is the
  cheapest form that satisfies #1907 at all, and it needs no Python entry point — so it is a
  genuine rival, not a wrapper question. verified: the form works. The `WHERE` clause above, run
  under `psql` over a seeded three-table join, prints System id, resource kind and the raw
  `provider` value for all eight wrong shapes and neither correct one. The `onboard` precedent
  supports it rather than the alternative: that recipe wraps a **shell** script
  (`justfile:49-50` → `scripts/live-stack/onboard.sh`), not Python. And it is **not** expensive
  to test — `db/migrate.py:139` already executes checked-in `.sql` text with
  `conn.execute(migration.sql.encode())`, so a criterion-4 test would run the file against a
  seeded connection with no subprocess and no stdout parsing. verified: what actually rejects it
  is criterion 5 — this repo has **no** psql-based exposure to match. `rg -n psql justfile
  scripts/` returns nothing, and the only non-migration `.sql` in the tree
  (`deploy/compose/bootstrap-migration-owner.sql`) is a container init-db mount, not an operator
  query. `verify-project` is the pattern criterion 5 points at, and it is a Python entry point.
  judgment: a second, unprecedented exposure mechanism for one report is the "two mechanisms for
  one job" surface this repo avoids. Secondarily, a raw `psql` dump prints
  `LibvirtProfile.domain_xml_params` — an agent-supplied `dict[NonEmptyStr, NonEmptyStr]` — into
  an operator report, where the rendered label prints a fixed vocabulary.
- **Ship it as a plain `just` recipe wrapping the Python entry point.** judgment: not a rival —
  it is one level up from the decision, since a recipe still has to wrap something, and it stays
  available afterwards on exactly the `onboard` precedent. Noted so the alternative above is not
  read as ruling it out. (Either form could **not** join the `ci` chain, since every `*-check` in
  it reads the tree and none opens a database connection, `justfile:656` — a fact about the gate,
  not a reason against a recipe.)
- **Repair the rows as well as report them.** judgment: #1907 states the exclusion in its own
  words ("this is a report, not a migration"; "deciding between cordon, teardown, and operator
  notification is a separate call"), and there is no accepted decision that says which of the
  three is right.
- **Do nothing and let the residue surface as the `AttributeError` it already raises.** judgment:
  foreclosed rather than merely weak — #1907 is an accepted issue asking for detection, so this
  option is outside the charter whatever its merits. Recorded anyway because the Context's own
  bound makes it the sharpest question in this record: if the affected population is only
  deployments that opted a mock provider in, is any of this worth building? Two answers. The
  discovery cost is what ADR-0549 set out to remove — a bare `AttributeError` at first use of a
  `ready` System is the confusing failure, and it is no less confusing in a dev stack. And the
  scale argument does not reach the decision, it reaches the *form*: it is why the cheapest
  adequate exposure was contested above rather than assumed, and criterion 5 is what settled
  that. A reader who thinks the scale should have decided differently is disagreeing with #1907,
  not with this record.
- **Report only the live states.** judgment: which states matter is the triage call #1907
  excludes, and `state` is in the report so the operator can make it.
- **Print a scanned-vs-total System count, so a narrowed join is visible in the run.** The inner
  join to `resources` drops a System whose Allocation carries a NULL `resource_id`, and this
  record accepts that as a residual; a count beside the clean line would surface it at run time
  where the suite cannot. verified: the count is cheap — `SELECT count(*) FROM systems` against
  the same connection, on a table the sweep already seq-scans. judgment: rejected anyway, because
  the divergence it would surface requires a migration that does not exist, #1907 asks for a
  report of mismatches rather than a coverage statistic, and a field added for a hypothetical is
  surface nobody asked for. The residual is therefore accepted **unsignalled** by a count —
  weaker and more honest than unsignallable. The clean line carries the bound instead, in
  wording rather than a field: it reports that no System **bound to a Resource** mismatches, not
  that no System does, so it never quantifies over rows the join did not read.
- **Extract the section key in SQL with `jsonb_object_keys`.** verified: on `postgres:17`,
  `SELECT jsonb_object_keys('"s"'::jsonb)` fails with
  `ERROR: cannot call jsonb_object_keys on a scalar`, so the bare form is out. A `jsonb_typeof`
  `CASE` guard around a scalar sub-`SELECT` **does** work in practice — it ran clean over object,
  JSON-`null`, scalar and missing-key rows. So the guard is not rejected for failing. verified:
  PostgreSQL 17 §9.18.1 (referring to §4.2.14) notes that "CASE evaluates only necessary
  subexpressions" is not ironclad; its documented instance is constant folding, which cannot
  reach a subexpression correlated to a column, so that note does **not** predict a failure here
  — it only declines to promise evaluation order. judgment: that is thin ground to rest on, but
  it is not what decides this. What decides it is testability: a pure Python renderer gets a case
  per stored shape — one key, several keys, empty object, JSON `null`, scalar, array — and the
  SQL form gets none, on the one part of the sweep whose job is to survive malformed data.
