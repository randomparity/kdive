# Runbook: `kdivectl` operator CLI

Operator guide for `kdivectl`, the kdive admin CLI. `kdivectl` is a FastMCP **client**: it
attaches an OIDC bearer token and calls the same MCP tools an agent does — there is no new
server transport and no direct database or object-store access from the operator host (the
host holds only the bearer token). Every registered MCP tool is reachable as a
`group subcommand` verb: a hand-curated subset renders read tools as tables/JSON and wraps the
M1.3 break-glass mutations, and every other tool is a **schema-generated verb** with typed
flags (see [The generated verb surface](#the-generated-verb-surface)). A tiered, fail-closed
`tool call` passthrough additionally reaches any tool by raw name for scripting. See
[ADR-0089](../../adr/0089-operator-cli-mcp-client.md),
[ADR-0421](../../adr/0421-schema-generated-kdivectl-verbs.md),
[ADR-0423](../../adr/0423-generic-generated-verb-dispatch.md), and the
[M2.2 plan](../../archive/superpowers/plans/2026-06-10-m22-admin-cli.md).

Every call `kdivectl` makes is attributed: the server records the OIDC `client_id` and
resolves an `actor`. When you authenticate under the dedicated `kdivectl` OIDC client, your
actions are audited as **`operator-cli`** — distinct from an agent's `agent` actor. Reading
the audit trail by `actor` is how you separate operator break-glass from routine agent work
(see [Reading the audit trail](#reading-the-audit-trail-by-actor)).

## Prerequisites

1. **A dedicated `kdivectl` OIDC client.** Register a client in your IdP whose id is
   recorded as the CLI's `azp`/`client_id`. The server maps this client id to
   `actor=operator-cli`. The default is `kdivectl`; override with `KDIVE_CLI_CLIENT_ID` if
   you registered a different id.
2. **Environment.** `kdivectl` reads its configuration from `KDIVE_*` environment variables
   (the single config source of truth, ADR-0087):

   | variable | purpose | default |
   |----------|---------|---------|
   | `KDIVE_SERVER_URL` | the server's streamable-HTTP MCP endpoint | `http://127.0.0.1:8080/mcp` |
   | `KDIVE_TOKEN` | bearer token (prod path; overrides the login cache) | unset |
   | `KDIVE_CLI_CLIENT_ID` | OIDC `client_id` the CLI authenticates under | `kdivectl` |
   | `KDIVE_OIDC_ISSUER` | mock-OIDC issuer base URL (dev `login` path) | unset |
   | `KDIVE_OIDC_AUDIENCE` | token audience (dev `login` path) | the server default |

   Point `KDIVE_SERVER_URL` at the running stack's MCP endpoint (for a local stack that is
   typically `http://127.0.0.1:8000/mcp` — keep it in sync with the bind address; see
   [live-stack.md](live-stack.md)).
3. **Install.** `kdivectl` is the `kdivectl` console script from this package
   (`pip install kdive` / `uv pip install kdive`), or run it in-tree as
   `python -m kdive.cli`.

## Authenticating

There are two token paths; `KDIVE_TOKEN` always wins over the login cache.

### Production: supply `KDIVE_TOKEN`

Have your IdP mint an access token for an operator principal under the `kdivectl` client
and export it:

```bash
export KDIVE_TOKEN="$(your-idp-mint-operator-token)"
export KDIVE_SERVER_URL="https://kdive.example.com/mcp"
kdivectl resources list
```

`kdivectl` never prints or logs the token.

### Development: `kdivectl login` against the mock-OIDC issuer

With `KDIVE_OIDC_ISSUER` set, `kdivectl login` drives the mock-OIDC authorization-code flow,
mints a token under `KDIVE_CLI_CLIENT_ID`, and caches it `0600` (under a `0700` parent) at
`$XDG_STATE_HOME/kdive/token` (default `~/.local/state/kdive/token`). The cached token is
read automatically when `KDIVE_TOKEN` is unset.

```bash
export KDIVE_OIDC_ISSUER="http://127.0.0.1:8081/default"
export KDIVE_SERVER_URL="http://127.0.0.1:8000/mcp"

kdivectl login                              # no platform role
kdivectl login --platform-role platform_operator
kdivectl login --platform-role platform_admin
```

The `--platform-role` axis encodes the platform role into the minted token. Break-glass
mutating verbs need the platform role the underlying tool gates on — see
[Break-glass mutating verbs](#break-glass-mutating-verbs).

## The generated verb surface

`kdivectl` exposes **every** registered MCP tool as a verb, not just the hand-curated ones
below. Each tool contributes one verb at a canonical path derived from its name, so the CLI
surface tracks the server's tool surface automatically (ADR-0421, ADR-0423). Run `kdivectl
--help` to list the groups, `kdivectl <group> --help` to list a group's verbs, and `kdivectl
<group> <verb> --help` to see a verb's flags — the parser is built offline, so `--help` and
[shell completion](#shell-completion) never open a session.

### Canonical path derivation

A tool named `namespace.op` maps to the verb `namespace op`, with underscores in `op`
becoming dashes; each scalar tool parameter `some_param` maps to the flag `--some-param`
(ADR-0421 §2). For example:

| MCP tool | generated verb |
|----------|----------------|
| `control.force_crash` | `kdivectl control force-crash` |
| `systems.authorize_ssh_key` | `kdivectl systems authorize-ssh-key` |
| `accounting.set_quota` | `kdivectl accounting set-quota` |
| `debug.set_breakpoint` | `kdivectl debug set-breakpoint` |

A parameter the generator cannot express as a typed scalar flag — a nested object, an object
array — is surfaced as a single `--<param>-json` escape that takes a JSON object or array
(validated at parse time; a malformed or bare-scalar value is a usage error, exit `2`). A
generated verb emits the server response envelope the same way `--json` does for curated verbs
(the scriptable contract is the tool's own output schema, not a CLI-chosen column subset).

### Curated vs. generated verbs

A **curated** verb (the read verbs and break-glass mutations documented below) overrides the
generated shape at its path with hand-tuned positionals, flags, and table rendering; it wins at
its own path only. **Every other tool** takes the schema-derived shape: typed `--flags` plus
the `--<param>-json` escapes, rendering the response envelope. Both kinds call the same
underlying tool and are gated by the **same** server-side authorization — the generated shape
is a convenience layer, never a second authorization path.

### Generated-verb mutation ceremony

Curated break-glass verbs and the `tool call` passthrough are not the only way to reach a
mutating or destructive tool: a generated verb reaches its own tool directly. The ceremony
differs from the passthrough's in one way — **naming the generated verb is itself the
acknowledgement, so no `--allow-mutating` / `--allow-destructive` opt-in flag is used** (ADR-0421
§4, ADR-0423). The tier is resolved from the tool's **live** server annotations at call time
(never the committed artifact, so a stale artifact cannot downgrade a tool's tier), and drives
the ceremony:

- **read-only** tool → called directly.
- **mutating** tool → a fail-closed token-`exp` preflight runs first (a near-expired token is
  refused up front; re-run `kdivectl login` and retry), then the call.
- **destructive** tool → the preflight **plus** a typed-`yes` confirmation on a TTY (or `--yes`
  for non-interactive use; a non-interactive stdin without `--yes` refuses).
- **unclassifiable** tool (annotations missing or not a literal `readOnlyHint`/`destructiveHint`)
  → fail-closed and **unreachable** (exit `3`), so nothing slips through unclassified.

The server-side destructive-op gate (ADR-0006/0020) remains the real authorization boundary;
this ceremony is the client-side UX guard on top of it. Server-side authorization for each
generated verb is the underlying tool's own — a mutating/destructive verb still requires
whatever platform role or project grant the tool enforces, exactly as the break-glass verbs do.

## Read verbs

The curated read verbs call one read-only MCP tool and render a table (or JSON with `--json`).
They are a hand-tuned subset of the [generated surface](#the-generated-verb-surface) above;
every other read tool is reachable as a generated verb (`kdivectl <group> <verb>`) or through
the read-only [`tool call` passthrough](#tiered-passthrough-tool-call):

```bash
kdivectl resources list [--kind <kind>]
kdivectl resources describe <resource_id>
kdivectl allocations list --project <project>
kdivectl allocations get <allocation_id>
kdivectl systems list [--state <state>]
kdivectl systems get <system_id>
kdivectl runs get <run_id>
kdivectl jobs list
kdivectl jobs get <job_id>
kdivectl accounting usage-project --project <project>
kdivectl accounting report-all-projects [--group-by principal] [--since <ts>] [--until <ts>]
kdivectl accounting report-granted-set [--projects a,b] [--group-by principal] [--since <ts>] [--until <ts>]
kdivectl inventory list [--project <project>]
```

`--json` may be given before or after the verb (`kdivectl --json resources list` or
`kdivectl resources list --json`). It emits the server response envelope **verbatim** — the
same shape every verb returns, curated or generated: `object_id`, `status`, `data`,
`suggested_next_actions`, `refs`, `error_category`, and nested `items` for a collection. The
scriptable contract is the tool's own published output schema (ADR-0421 §6), not a CLI-chosen
column subset. The default (no `--json`) table output is unchanged; only its columns are
projected. **Breaking change (pre-1.0):** `--json` previously emitted only the verb's declared
columns; scripts that read those column keys must now read them from the envelope's `data`
(and each row from `items[*].data`).

`--project` is **required** for `allocations list` and `accounting usage-project` (no square
brackets): each underlying tool (`allocations.list`, `accounting.usage_project`) reads exactly
one project, so the CLI enforces the flag up front — omitting it is a usage error (exit `2`),
not a cross-project listing. `inventory list` is the exception: its `--project` is an
**optional** narrowing filter on a cross-project auditor read (`inventory.list`, see
[the matrix below](#read-authorization-platform-axis-vs-project-axis)), omitted for the
all-projects view. There is no "list across all my projects" verb today; query each project
in turn.

### Cross-project accounting reports

Two verbs render the multi-project accounting rollups (`reserved` / `reconciled` / `variance`
per project, plus a totals footer). They map to the report tools and split across the same
two authorization axes as the rest of the read surface:

```bash
kdivectl accounting report-all-projects [--group-by principal] [--since <ts>] [--until <ts>]
kdivectl accounting report-granted-set [--projects a,b] [--group-by principal] [--since <ts>] [--until <ts>]
```

- `accounting report-all-projects` (`accounting.report_all_projects`) is the **platform-axis**
  read: it needs a `platform_auditor` token (satisfied by `platform_admin`) and rolls up every
  project. A token without that role gets `authorization_denied` (exit `3`) — it is in the
  platform-axis row of [the matrix below](#read-authorization-platform-axis-vs-project-axis).
- `accounting report-granted-set` (`accounting.report_granted_set`) is the **project-axis** read: it
  rolls up the projects you hold a role on. `--projects a,b` narrows to a named subset (each
  is `viewer`-checked; a project you are not a member of is denied). Omit `--projects` for all
  your granted projects; a given-but-empty value (e.g. a stray comma) is a usage error
  (exit `2`).
- `--group-by principal` groups rows by principal instead of per-project. `--since` and
  `--until` are timezone-aware ISO-8601 bounds forming a half-open window; omit both for all
  time. The bounds are validated server-side — a non-ISO-8601, timezone-naive, or inverted
  (`start >= end`) window returns `configuration_error` (exit `2`).
- Both render the per-project rows as a table with a totals footer. Under `--json` they emit
  the whole server envelope (like every verb): the rollup totals in `data` and the per-project
  rows as nested `items[*]` envelopes — not the former projected `{"items": ..., "totals": ...}`
  object.

### Read authorization: platform axis vs. project axis

Read verbs split across the two independent authorization axes (ADR-0043 §7), and the split
is **load-bearing**: a platform role does **not** grant project-scoped reads, and project
membership does **not** grant cross-project reads. A `kdivectl login --platform-role …` token
with no project grant sees no project tenant data — there is no implicit "admin sees
everything." To read a specific project's data, be granted on that project; for the
cross-project oversight view, use a `platform_auditor` token.

| read | authorized by | denied to |
|------|---------------|-----------|
| `allocations list/get`, `systems list/get`, `runs get`, `jobs list/get`, `accounting usage-project` (`accounting.usage_project`) | per-project `viewer` on the **target project** (`require_role`) | a platform-only token with no membership on that project sees no project tenant data. A by-id `get` returns a **not-found-shaped** result (exit `4`; tenant existence is not revealed, and **no** distinct authorization-denied code is emitted). A read that **names a project** the caller is not a member of (`allocations list --project …`, `accounting usage-project` / `accounting.usage_project`, `accounting.estimate`) is denied `authorization_denied` (**exit `3`**, ADR-0098) — the named project carries no existence to leak, so the denial surfaces distinctly (ADR-0043 §4a) |
| cross-project `inventory list` (`inventory.list`), `accounting.report` (all-projects), `audit.query` (cross-project) | `platform_auditor` (satisfied by `platform_admin`) | a project-member token holding no platform role |
| `secrets list`, `doctor` | `platform_operator` | any token lacking `platform_operator` |
| `resources list/get`, `fixtures list` | plain authenticated read (no project scope, no role floor) | unauthenticated callers only |

Note `inventory list` is the **cross-project auditor** read (it maps to the `inventory.list`
tool, gated `platform_auditor`), not a per-project read — it is the one read verb where a
platform-axis token is *granted* and a bare project member is *denied*. Every other project-data
read is the inverse.

The matrix is keyed by the **underlying tool**, so every [generated read
verb](#the-generated-verb-surface) inherits the axis of its tool — the curated verbs above are
a subset, not the whole authorized surface. For example `audit query` (`audit.query`) is a
platform-axis auditor read like `inventory list`; `session whoami` (`session.whoami`) and
`projects list` (`projects.list`) are plain authenticated reads like `resources list`; and
`runs list` / `jobs wait` are per-project `viewer` reads like `systems get`. When in doubt,
`kdivectl <group> <verb>` returns the same `authorization_denied` (exit `3`) or
not-found-shaped (exit `4`) result its tool would for an agent.

Three project-axis outcomes are distinct and should not be conflated. (1) A **non-member**
(including a platform-only token) reaching a **by-id** `get` gets the
**not-found-shaped** result above (exit `4`) — the tool resolves the object's project, finds the
caller is not a member, and returns not-found *before* the role check, so a non-grant never
surfaces a distinct authorization-denied code (and is **not** audited; only platform-role
*overreach* within the platform tier leaves a denial row — ADR-0043 §4, see
[Reading the audit trail](#reading-the-audit-trail-by-actor)). The distinction is deliberate: a
by-id lookup must not become a cross-tenant existence oracle, so "ungranted, exists" is
indistinguishable from "absent". (2) A **non-member naming a project** in a named-scope read/op
(`allocations list --project`, `accounting.usage_project`, `accounting.estimate`) is denied
`authorization_denied` (**exit `3`**, ADR-0098) — the caller already supplied the project name, so
there is no existence to hide, and the denial surfaces distinctly rather than collapsing to a
generic error; like the by-id non-grant it is **not** audited (the non-member case is
non-amplifying). (3) A **member** whose role ranks below the required floor reaches `require_role`,
which surfaces `authorization_denied` (**exit `3`**) **and** is audited as a member-over-reach
denial.

### Secret-presence and fixture reads

Two reads surface catalog presence without exposing values. `secrets list` is
platform-role gated; `fixtures list` is a plain authenticated read:

```bash
kdivectl secrets list                       # secret *presence* (refs only), platform-gated
kdivectl fixtures list                       # available fixtures, plain authenticated read
```

`secrets list` reports presence/refs only — it never returns secret values.

## Diagnostics (`doctor`)

`doctor` runs the read-only deployment diagnostics through the operator-gated
`ops.diagnostics` tool and renders one verdict row per check (`check`, `status`, `detail`,
`fix`, `provider`). It is usable as a deployment/CI gate as well as interactively:

```bash
kdivectl doctor                              # the three cheap read checks (default)
kdivectl doctor --provider remote-libvirt    # diagnose one named registered provider
kdivectl doctor --with-egress                # also run the heavyweight egress probe
kdivectl doctor --json                       # the whole verdict envelope (checks under items)
```

The exit code is **gate-safe** (ADR-0091 §5): all-`pass` exits `0`; any `fail` exits `1`
(a contract is violated, and `fix` names the remediation); a check that could not run to a
verdict (a down dependency) is reported as `error` and exits `6` — a *distinct* nonzero code,
so a gate never goes green on a check that did not actually pass. A `fail` and an `error`
together exit `1` (a real contract violation is never masked by an unrelated down
dependency). `doctor` is operator-gated, so it exits `3` if your token lacks
`platform_operator`. The default run is the three read checks; `--with-egress` is opt-in
because the egress probe provisions a probe guest.

## Shell completion

`kdivectl completion {bash,zsh}` prints a self-contained completion script covering every
group, verb, and per-verb flag across the merged curated + generated surface (ADR-0424). It
resolves **offline** — no bearer token, no server call — so it is safe to run and install on any
host, authenticated or not: the script is walked from the parser tree, which the CLI builds
without opening a session. Regenerate it after upgrading `kdivectl` to pick up new verbs.

Positional argument *values* (a `resource_id`, a tool `name`) are not completed — they are
runtime/tenant data with no offline source; completion of a verb's `--` flags still works after
one is typed.

### bash

```bash
# System-wide (bash-completion loads it lazily):
kdivectl completion bash | sudo tee /usr/share/bash-completion/completions/kdivectl >/dev/null

# Or per-user:
mkdir -p ~/.local/share/bash-completion/completions
kdivectl completion bash > ~/.local/share/bash-completion/completions/kdivectl

# Or source it directly from ~/.bashrc:
source <(kdivectl completion bash)
```

Requires `bash-completion` (bash 4+). Start a new shell to load it.

### zsh

```zsh
# Autoload from your fpath (run once, then restart the shell):
kdivectl completion zsh > "${fpath[1]}/_kdivectl"

# Or source it from ~/.zshrc, after `compinit`:
source <(kdivectl completion zsh)
```

The same script works either way: autoloaded from `$fpath` zsh runs it as the completion
function; sourced from `~/.zshrc` it registers itself with `compdef`.

## Tiered passthrough (`tool call`)

To reach any MCP tool by raw name — for scripting, or a tool with no convenient generated
verb — use the passthrough. It is **read-only by default and fail-closed**: it lists the
server's tools, classifies the target from its live annotations, and admits only tiers you
have explicitly opted into (ADR-0107).

```bash
kdivectl tool call accounting.usage_project --json '{"project": "my-proj"}'  # read-only default
kdivectl tool call resources.set_status --allow-mutating --json '{...}'       # opt in to mutating
kdivectl tool call systems.teardown --allow-destructive --yes --json '{...}'  # opt in to destructive
```

The tier opt-in is cumulative: no flag admits read-only tools only; `--allow-mutating` also
admits mutating tools; `--allow-destructive` implies `--allow-mutating` and also admits
destructive tools. A target above the tier you opted into exits `3` without calling the tool,
naming the flag that would admit it. A mutating or destructive call runs the same fail-closed
token-`exp` preflight the [break-glass verbs](#break-glass-mutating-verbs) do; a destructive
call additionally needs a typed-`yes` confirmation on a TTY (or `--yes`). An unclassifiable
tool (annotations missing or not a literal hint) is fail-closed and unreachable at every tier
(exit `3`), so nothing slips through unclassified.

This is a client-side policy/UX guard; the server-side destructive-op gate (ADR-0006/0020)
remains the real authorization boundary, so opting a tier in never bypasses the platform-role
or project grant the underlying tool enforces.

## Break-glass mutating verbs

These verbs route through the M1.3 break-glass tools. Each is single-call and re-runnable
(the server tools are idempotent against already-torn-down/already-released state). Before
its one call, each verb runs a fail-closed token-`exp` preflight: a near-expired token is
refused up front (re-run `kdivectl login` and retry) rather than risking a mid-operation
401. These curated verbs are the ergonomic path to the break-glass tools; the same tools are
also reachable — with the same server-side authorization — as [generated
verbs](#the-generated-verb-surface) or through the `tool call`
[passthrough](#tiered-passthrough-tool-call) with an explicit `--allow-mutating` /
`--allow-destructive` opt-in.

```bash
kdivectl ops force-teardown <system_id> --reason <R> --force   # ops.force_teardown (needs --force)
kdivectl ops force-release <allocation_id> --reason <R>  # ops.force_release
kdivectl resources cordon <resource_id>                     # resources.cordon
kdivectl resources drain <resource_id> [--mode passive|force_release] [--reason <R>]  # resources.drain
```

**Platform roles required (these are not implied by one another):**

| verb | gated on |
|------|----------|
| `ops force-teardown`, `ops force-release` | `platform_admin` |
| `resources cordon` | `platform_operator` |
| `resources drain --mode passive` (default) | `platform_operator` |
| `resources drain --mode force_release` | `platform_admin` (it empties tenant allocations) |

`platform_admin` does **not** imply `platform_operator`, and vice versa — authenticate with
the platform role the specific verb gates on. A verb invoked without the required platform
role exits `3` (`authorization_denied`), and — when your token holds *some* platform role —
the denial is itself audited under `actor=operator-cli` (separation-of-duties accountability).

`ops force-teardown` additionally requires `--force` as an explicit break-glass acknowledgement.

### Exit codes

| code | meaning |
|------|---------|
| `0` | success |
| `1` | generic failure |
| `2` | configuration error |
| `3` | authorization denied, **or** a client-side ceremony refusal: a `tool call` target above the opted-in tier, an unclassifiable (fail-closed) tool on the passthrough or a generated verb, a token-`exp` preflight refusal, or an unconfirmed destructive call |
| `4` | not found |
| `5` | conflict |
| `6` | `doctor` only: a check could not run to a verdict (`error`); not a passed contract |

## Reading the audit trail by `actor`

Every break-glass call writes a `platform_audit_log` row carrying the `tool`, a `scope`
(the target project and object id), a one-way digest of the arguments (the `reason` is
digested, never stored in plaintext), the held `platform_role`, and the resolved `actor`.
When you authenticate under the `kdivectl` client, that `actor` is `operator-cli`.

To review operator break-glass activity, filter by `actor` against the stack's Postgres:

```sql
SELECT ts, principal, tool, scope, platform_role, actor
FROM platform_audit_log
WHERE actor = 'operator-cli'
ORDER BY ts DESC;
```

Both successful break-glass calls and audited denials appear here, so the trail is a
complete operator accountability record. (A denial from a token holding *no* platform role
is the routine non-grant case and is deliberately not recorded; only platform-role overreach
leaves a denial row.)

## Exit-criterion boundary test

`tests/integration/test_kdivectl_boundary.py` is the load-bearing proof of the above: it
drives `kdivectl ops force-release` through the real entry point twice — once with a
`platform_admin` token (succeeds; `operator-cli` audit row) and once with an under-privileged
`platform_operator` token (exit `3` + a `operator-cli` denial audit row). It is gated
`live_stack`, so it runs only against a running stack (`just stack-up` + the app tier, then
`just test-live-stack`) and skips cleanly in normal CI. Running it is part of this runbook,
not the CI gate.
