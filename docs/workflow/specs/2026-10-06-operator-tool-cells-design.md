# Read-only operator MCP tool cells (#2812)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment. Builds on the #2811 carrier
([spec](2026-10-02-core-tool-cells-design.md)) and reuses its harness unchanged.

## Problem

Owner 2812 in `scripts/coverage_campaign/obligations.toml` holds a 24-tool operator service group
and a 4-tool break-glass provider group: 628 cells, none bound to a test. The operator split the
issue on 2026-10-06. #2812 keeps the eight read-only operator service tools — `audit.query`,
`inventory.list`, `ops.diagnostics`, `ops.export_cost_classes`, `ops.export_systems_toml`,
`ops.jobs_list`, `ops.tool_trail`, `secrets.list` — whose 124 cells (31 scenarios, two
configurations, two exposures) this change proves. The rest moves to sibling owners.

## Scope

1. **Ownership split** in `obligations.toml`, following commit `ba1d235f1`. Group 2812 keeps the
   eight tools above. New service groups take the 14 mutating tools (owner 3110, with the
   existing overrides for `inventory.clear_override`, `ops.reconcile_now`,
   `ops.set_queue_paused` and `resources.drain`) and `ops.build_uses_list` plus
   `ops.recover_build_use` (owner 3111, with the `ops.recover_build_use` override). The
   break-glass provider group changes owner to 3112.
2. **ppc64le routing** in `contract.py` `_tool_cells`: a provider cell of owner 3112 on `ppc64le`
   is owned by #2818, as owner 3062's already is.
3. **Carrier** `tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell`,
   parametrized over the 124 cells and framed by `run_tool_cell`, `prove_functional` and
   `prove_rejection`. `[implementations]` binds the 31 scenarios to it.
4. A runbook section beside the other tool-cell carriers, and a live run of both lanes.

No product, harness, ADR or migration change.

### Grants

Each cell uses a fresh project `cov-<8 hex>` (P) and the default `project_state(P)` snapshot. A
functional cell's token holds the tool's gate: `platform_operator` for `ops.diagnostics`,
`ops.export_*`, `ops.jobs_list` and `secrets.list`; `platform_auditor` for `inventory.list`,
`ops.tool_trail` and the all-projects `audit.query`; project `admin` of P for the project form.

### Functional effects

Independent sources are the evidence database read in a read-only session, the configured
`systems.toml` read by the test process, the checkout's `HEAD`, and calls the cell makes itself
with a separate token whose subject and agent session are unique to the cell.

- `audit.query`: a viewer of P and of P2 (a second fresh project) calls the project form of
  `audit.query` twice in P and once in P2. Each call is a rank-below denial, which the
  denial-audit boundary records as one `audit_log` row with transition `denied`. The project
  form, as admin of P filtered to that principal, returns exactly the two P rows. The
  all-projects form, as auditor, returns all three. Every item equals its database row, and paging
  with `limit=1` through `next_cursor` yields the same rows in `ts DESC, id DESC` order.
- `inventory.list`: a granted 1-vCPU, 1 GiB allocation is taken in the funded `KDIVE_PROJECT` and
  released on exit. The project-filtered listing equals that project's allocation and System rows
  in `created_at DESC, id DESC` order, and it holds the granted allocation. The caller holds no
  grant on that project, so the read is cross-project. A `resource_id` filter returns only that
  host's rows. `limit=1` reports one allocation and `truncated` exactly when the database holds
  more.
- `ops.diagnostics`: the default run returns `secret_ref` with status `pass` and the four local
  worker-vantage checks (`multiarch_gdb`, `pseries_fadump`, `guest_arch_accel`,
  `depmod_toolchain`), none carrying the worker-unavailable detail. `has_failure` and `has_error`
  agree with the item statuses, and `service_version.commit` is a prefix of the checkout's
  `HEAD`. The deliberately unavailable prerequisite is the probe-guest seam: no local
  contribution supplies an egress check, so `with_egress=true` returns exactly one `error` item
  whose detail says diagnostics could not be assembled. The cell refuses that call (stops
  `blocked`) if the default run lists a remote-libvirt check, because a remote seam would
  provision a guest.
- `ops.export_cost_classes`: the fragment parsed with `tomllib` equals the
  `cost_class_coefficients` rows (name and exact decimal string), in name order.
- `ops.export_systems_toml`: the parsed export's `[[cost_class]]` entries equal those rows. Every
  `[[local_libvirt]]` and `[[image]]` name the configured `systems.toml` declares appears in the
  export. With `persist=true` and writeback unset in the lane, the tool answers
  `configuration_error` naming the disabled writeback, so no bytes are written. A lane with
  `KDIVE_INVENTORY_WRITEBACK` set fails the cell rather than writing.
- `ops.jobs_list`: the diagnostics call above enqueues a worker-check job under the provider's
  project, which the caller does not hold. The listing's depth fields equal
  `SELECT state, count(*) FROM jobs GROUP BY state`. Its rows (kind, state, project, attempt,
  worker) equal the newest database rows and include that job. Paging the job's state with
  `limit=1` yields the database order. Background jobs move, so the comparison reads the database
  before and after the call and retries, up to three times, until both reads agree.
- `ops.tool_trail`: the unique token calls `projects.list`, `session.whoami` and `inventory.list`
  over `direct`. The last is denied because the token holds no platform role. Filtered to that
  agent session, the trail returns exactly three rows, newest first, with these tools, outcomes
  `denied, ok, ok`, the subject, `actor=operator-cli`, the CLI client id, and the `args_digest`
  of `{}`. Every row equals its database row, and paging with `limit=1` yields the same three.
- `secrets.list`: the configured sources are the `KDIVE_*` settings marked secret in the lane
  environment the test process shares with the server. A value that resolves under
  `KDIVE_SECRETS_ROOT` is a reference, and its file content is a secret; any other value (a DSN,
  a token) is itself a secret. `data.secrets` is sorted and duplicate-free, and each label is
  `<process-global>`, `<scoped>` or one of those references. No secret appears anywhere in the
  serialized envelope.

### Rejection cells

| Boundary | Grants and arguments | Accepted categories |
|---|---|---|
| authentication | a viewer of P; harmless arguments (`{}`, or the project form naming P) | HTTP 401 for the foreign signature; the issued token is not refused |
| authorization | `platform_auditor` for the operator-gated tools; no platform role for `inventory.list` and `ops.tool_trail`; a viewer of P for the project form of `audit.query` | `authorization_denied` |
| project-isolation (`audit.query`) | admin of another fresh project, for the project form naming P | `authorization_denied` |
| validation | the functional grants, which make the tool visible to `tools.invoke`, with a mistyped field or an unknown discriminator | ADR-0722 §3 as amended |

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on a disposable lab host, with one stack per lane (the
     `default` lane, and the `recovery` lane started with `KDIVE_WORKER_DEATH_VERIFIER=docker`),
     local-libvirt only and writeback unset;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes, and a contract whose ownership matches the operator's split;
   - the shared stack left as found: other projects' rows, and the funded project's capacity
     (its allocation is released and checked).
3. **Accepted failure classes:**
   - Audit, platform-audit and tool-invocation rows the cells cause, the released allocation, and
     the finished diagnostics job stay as history. The snapshot excludes the audit tables
     (ADR-0722 §4), and the stack wipe after the proof removes them.
   - The server's secret registry fills lazily, so `secrets.list` presence is checked as a subset
     of the configured sources, not equality. A secret held only in the server's memory and never
     configured is outside what the test can know, and so is a `systems.toml` reference, which
     no local-libvirt lane declares.
   - `ops.export_systems_toml` writeback is not driven. The lanes leave it disabled, and the
     observation calls it optional.
   - Concurrent carriers on one stack are not modelled. The jobs comparison retries only
     background queue movement.
   - The `provider` argument of `ops.diagnostics` is not driven. The service factory ignores it
     (`diagnostics/service.py` `default_service_factory`), and that is reported as a follow-up.
4. **Covered elsewhere:**
   - mutating operator tools (#3110), build-use recovery tools (#3111), break-glass provider
     tools (#3112), ppc64le cells (#2818);
   - durable finalization and cross-replica guarantees (#2681), interruption and resource limits
     (#2816, #3117, #3118), filtering-list isolation (#3108).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| ownership split and ppc64le routing | focused-test | `test_coverage_contract.py`: the owner sets of 2812, 3110, 3111 and 3112 and their overrides; ppc64le break-glass cells owned by 2818 |
| bindings | focused-test | `test_coverage_contract.py`: the 124 cells bind to the new node, and the unbound set shrinks by exactly them |
| live cells | task-test-not-applicable | they act only against a live stack's server, database and worker; proven by the two-lane lab run (`qualify` over the tool cells) |
| runbook section | task-test-not-applicable | prose; `just docs-check` covers links and paths |
