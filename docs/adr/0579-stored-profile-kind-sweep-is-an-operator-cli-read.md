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
`control._op_opt_in` (`mcp/tools/lifecycle/control/registrar.py:207`), the Run install step
(`services/runs/steps.py:445`), the Run boot-evidence step
(`jobs/handlers/runs/boot_evidence.py:243`), and the vmcore `capture_method` lookup
(`mcp/tools/lifecycle/vmcore/handlers.py:181`). #1907's own body lists the debug-session
lifecycle in place of boot-evidence; that is wrong — the only profile-policy call there is
`drgn_live_seeds_bootstrap_key` (`mcp/tools/debug/sessions/lifecycle.py:445`), and the
fault-inject adapter returns `False` from it without dereferencing anything. The two libvirt
kinds could not reach `ready`, so the expected residue is fault-inject Systems specifically.

Issue #1907 asks for **detection, not repair**: a query over `systems` joined through
`allocations` to `resources`, a report naming each mismatch, and a test seeding one mismatched
`ready` System. It leaves the exposure open — "whether the sweep ships as a `just` recipe, a
reconciler pass, or an admin MCP tool" — and asks for the one that matches how other one-off
integrity checks are exposed here. Choosing between them is the decision this record settles.
The remediation policy (cordon, teardown, operator notification) is explicitly not settled here.

## Decision

**The sweep is `python -m kdive verify-profile-kinds`, a read-only operator subcommand that
prints one line per mismatch and exits non-zero when it finds any.**

It is built the way `verify-project` ([ADR-0256](0256-onboard-target.md)) is built, in
`src/kdive/admin/profile_kinds.py`:

- `scan_profile_kinds(conn)` runs one SQL statement and returns `list[ProfileKindMismatch]`
  (`system_id`, `project`, `state`, `profile_section`, `resource_kind`), ordered
  `s.created_at, s.id`.
- `verify_profile_kinds()` opens a pool with `create_pool()` and delegates, so an unset
  `KDIVE_DATABASE_URL` raises `CONFIGURATION_ERROR` before any query, exactly as verify-project
  does.
- `format_profile_kind_result(...)` is pure and returns `(message, exit_code)` — `0` for a clean
  sweep, `1` when any mismatch is found. The target database is named in the message through the
  existing `redact_database_url`.

**The predicate is total, and the observed section is rendered in Python.** The `WHERE` clause is

```sql
jsonb_typeof(s.provisioning_profile -> 'provider' -> r.kind) IS DISTINCT FROM 'object'
```

Measured on PostgreSQL 17.10: `'[1,2]'::jsonb -> 'a'`, `'"s"'::jsonb -> 'a'`, `'null'::jsonb ->
'a'` and `'{"b":1}'::jsonb -> 'a'` all return SQL `NULL`, `jsonb_typeof(NULL::jsonb)` is `NULL`,
and `jsonb_typeof('null'::jsonb)` is the string `null`. So every System row reaches the predicate,
and a profile with no usable `provider` object — or one whose section is JSON `null` — is reported
rather than silently dropped. The **key** under `provider` is
not extracted in SQL: `jsonb_object_keys` errors on a non-object argument, and while a
`jsonb_typeof` `CASE` guard does hold in practice, PostgreSQL declines to guarantee that a `CASE`
arm's subexpressions go unevaluated — and a sweep whose job is to survive malformed stored data
should not rest on an evaluation-order property the engine will not promise. The row carries
`provisioning_profile -> 'provider'` back instead, and a pure Python helper renders the label: the
single key for a one-key object, the sorted keys comma-joined for a multi-key one, and an
angle-bracketed marker (`<none>`, `<not-an-object>`) for a shape that carries no section at all.

**It reports every state, and it never writes.** `state` is in the report because #1907 asks for
it; filtering to live states would be the triage call #1907 excludes. Nothing in the module issues
a statement other than that `SELECT`.

## Consequences

- An operator upgrading past ADR-0549 has a one-command answer to "did this deployment mint any
  such System before the check landed", and a non-zero exit makes it usable from a deploy script
  the same way `verify-project` is.
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
  `KDIVE_DATABASE_URL` resolves, which is the server/deploy host.
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
- **Ship it as a `just` recipe.** verified: a recipe is a wrapper, not an entry point —
  `justfile`'s `onboard` is exactly a recipe wrapping `migrate`, `seed-project`, and
  `verify-project` against a live `KDIVE_DATABASE_URL` (`justfile:49-50`,
  `scripts/live-stack/onboard.sh`). judgment: so this is not a rival to the decision but a
  question one level up from it — where the Python entry point lives is still what has to be
  settled, and a `just verify-profile-kinds` wrapper stays available afterwards on exactly the
  `onboard` precedent. Note the recipe could **not** join the `ci` chain, since every `*-check`
  in it reads the tree and none opens a database connection (`justfile:656`) — but that is a fact
  about the gate, not a reason against the recipe.
- **Repair the rows as well as report them.** judgment: #1907 states the exclusion in its own
  words ("this is a report, not a migration"; "deciding between cordon, teardown, and operator
  notification is a separate call"), and there is no accepted decision that says which of the
  three is right.
- **Do nothing and let the residue surface as the `AttributeError` it already raises.** judgment:
  that is the status quo ADR-0549 called out as unfinished, and it puts the discovery at first use
  of a `ready` System rather than at upgrade time, which is the whole difference the sweep buys.
- **Report only the live states.** judgment: which states matter is the triage call #1907
  excludes, and `state` is in the report so the operator can make it.
- **Extract the section key in SQL with `jsonb_object_keys`.** verified: on PostgreSQL 17.10
  (`docker run --rm postgres:17`), `SELECT jsonb_object_keys('"s"'::jsonb)` fails with
  `ERROR: cannot call jsonb_object_keys on a scalar`, so the bare form is out. A `jsonb_typeof`
  `CASE` guard around a scalar sub-`SELECT` **does** work in practice — it ran clean over object,
  JSON-`null`, scalar and missing-key rows — so this is rejected on the guarantee, not on a
  failure: PostgreSQL documents that a `CASE` arm is not a promise that the other arms'
  subexpressions go unevaluated. judgment: the sweep's one job is to not raise on the malformed
  stored data it exists to find, which is a poor thing to rest on an evaluation-order property the
  engine declines to guarantee — and a pure Python renderer gets a test per shape, which the SQL
  form does not.
