# Runbook: kdivectl operator CLI

`kdivectl` calls KDIVE's MCP endpoint with a bearer token. Use this page for authentication,
command discovery, output handling, and CLI safeguards. The [tool reference](
../../guide/reference/index.md) owns each tool's parameters and behavior; [Safety and RBAC](
../../guide/safety-and-rbac.md) owns the permissions model.

## Before you start

Follow [installation from source](../install.md#from-source). In the configured checkout,
activate the installed environment so the commands below resolve:

```bash
source .venv/bin/activate
kdivectl --help
```

Set `KDIVE_SERVER_URL` to your deployment's streamable-HTTP endpoint, including `/mcp`.
For a development host stack, use the endpoint printed by the [live-stack runbook](live-stack.md).
The [configuration reference](../../guide/reference/config.md) lists CLI defaults. The CLI
connects to the server; ordinary tool calls do not need database or object-store credentials.

## Authenticating

In production, obtain an operator token through your identity provider's supported flow and
supply it as `KDIVE_TOKEN`. A nonempty value takes precedence over the login cache. The token
must have the audience and grants required by the server and requested tool.

For platform audit attribution, the token's verified `azp` or `client_id` must match the
server's `KDIVE_CLI_CLIENT_ID`. Merely running the CLI, or setting that variable locally with
an existing token, does not make the request `actor=operator-cli`.

`kdivectl login` implements the bundled mock-OIDC development flow. Configure
`KDIVE_OIDC_ISSUER` and `KDIVE_OIDC_AUDIENCE` for that stack, then choose the needed role:

```bash
unset KDIVE_TOKEN
kdivectl login --platform-role platform_operator
kdivectl session whoami --json
```

Use `platform_operator` for diagnostics or `platform_admin` for administrative break-glass
operations. These roles do not imply one another; neither grants project membership. Login
without `--platform-role` requests no platform role, and none of these login forms grants
project roles. Follow [project onboarding](../project-onboarding.md) for project-scoped tokens.

Login writes a `0600` token file under a `0700` parent at `$XDG_STATE_HOME/kdive/token`, defaulting
to `~/.local/state/kdive/token`. It prints a confirmation, not the token. Refresh a development
token by logging in again; refresh a production token through the identity provider and replace
`KDIVE_TOKEN`. An old environment token continues to override a newly written cache.

## The generated verb surface

Discover commands and their exact arguments from the installed parser, offline:

```bash
kdivectl --help
kdivectl resources --help
kdivectl resources describe --help
kdivectl jobs wait --help
kdivectl accounting report --help
```

MCP names become grouped commands: `resources.describe` becomes `resources describe`, and
underscores in operation names become hyphens. Generated descriptors own every MCP command's
path and argument grammar, including positionals. Some handlers specialize payload assembly or
rendering; they do not define a second parser. The installed descriptors may differ from a
server running another revision, so compare the client and server versions when a command is
missing or rejected.

Scalar parameters use typed flags or positionals shown by `--help`; complex parameters use
`--<parameter>-json` with a JSON object or array. The server validates the payload's structure.
For example, after setting `KDIVE_TOKEN` to a token with membership in the example project:

```bash
kdivectl allocations list --project example-project --json
kdivectl accounting usage --project example-project --json
```

The role and scope of the underlying tool apply equally to CLI calls. A by-id tenant lookup
can return a not-found-shaped result for an ungranted object; supplying a project name can
instead produce authorization denied. Use the tool reference and permissions guide to resolve
a denial rather than assuming a platform role gives access to every project.

## Output, waits, and exit codes

For MCP verbs, `--json` before the group or after the verb preserves the full server envelope,
including `status`, `data`, `items`, `error_category`, and `suggested_next_actions`. Default
output renders tables or records; specialized handlers choose their own columns. Scripts should
request `--json` and inspect the envelope instead of parsing presentation tables.

`jobs wait` and `allocations wait` perform one bounded server call. Pass `--timeout-s 0` for
one immediate read; use a positive timeout to wait. Read defaults and bounds from `--help` and
the [jobs](../../guide/reference/jobs.md) / [allocations](../../guide/reference/allocations.md)
references. For a job ID obtained from a previous response:

```bash
kdivectl jobs wait "$job_id" --timeout-s 0 --json
```

Exit `0` does not establish that a wait reached its desired outcome. Inspect `status`: a job
can still be `queued` or `running`; an allocation can still be `requested`. Repeat a bounded
wait while the result remains nonterminal, with an overall deadline in your script. A terminal
job may be `succeeded`, `failed`, or `canceled`; an allocation leaving `requested` may be granted
or may have ended without a grant. Handle the actual state, including failures.

| Code | Meaning |
|------|---------|
| `0` | No mapped tool error; inspect the returned state. Doctor has separate verdict semantics below. |
| `1` | Generic failure, including tool errors without a mapped category; doctor check failure. |
| `2` | Argument usage error or tool `configuration_error`. |
| `3` | Tool `authorization_denied`, or refusal by the generic command/passthrough ceremony. |
| `4` | Tool `not_found`. |
| `5` | Tool `conflict`. |
| `6` | Tool `capacity_exhausted`; also doctor errors without failed checks. |

Client failures can exit without a server envelope. Inspect the diagnostic output and fix its
cause before retrying; a nonzero exit alone does not establish that retrying will help. When
capturing output, preserve the command's exit status. Piping through `head`/`tail` or appending
`echo $?` makes the shell command report the last command's status instead.

## Tiered passthrough (`tool call`)

Use the passthrough to call a server tool by its MCP name and supply a raw argument object:

```bash
kdivectl tool call accounting.usage \
  --json '{"target":{"kind":"project","project":"example-project"}}'
```

Here `--json` is the **input payload**, not an output switch. The passthrough always prints the
full response envelope as JSON.

It lists the server's tools and classifies the target from live annotations. With no opt-in it
admits only read-only tools; `--allow-mutating` also admits mutations; `--allow-destructive`
admits both mutations and destructive tools. Destructive calls additionally require typed `yes`
on a TTY, or `--yes` for noninteractive use. Missing or unclassifiable tools are refused at every
tier with exit `3`. These client safeguards do not grant server-side permissions.

## Break-glass mutating verbs

Inspect the command and [operations](../../guide/reference/ops.md) or
[resources](../../guide/reference/resources.md) reference before invoking a mutation:

```bash
kdivectl ops force-teardown --help
kdivectl ops force-release --help
kdivectl resources set-scheduling --help
kdivectl resources drain --help
```

Generic generated mutations treat naming the verb as tier opt-in, check live annotations, and
use the same destructive confirmation as the passthrough. The specialized commands above take
a different execution path: they check token expiry and call the tool directly, without the
generic live-annotation or typed-`yes` ceremony. `ops force-teardown` additionally requires
`--force`. Do not assume every named destructive command prompts before acting.

Before a mutating passthrough, generic mutation, or these specialized commands, the client
requires the token's integer `exp` to be more than 30 seconds beyond the client's current Unix
time. This is a per-call admission check, not a job deadline or token renewal. Missing,
unreadable, or near-expired claims prevent the call. Refresh the token through the appropriate
flow above and retry. Generic commands and passthrough return `3` for this refusal; the
specialized break-glass path currently raises a client error instead.

## Diagnostics (`doctor`)

`doctor` calls the operator-gated `ops.diagnostics` tool and renders one row per check. It requires
`platform_operator`; a token lacking that role exits `3`.

```bash
kdivectl doctor
kdivectl doctor --json
```

JSON preserves the full envelope, with check results under `items[].data`. Any failed check exits
`1`; errors without failures exit `6`; no failure/error flags exits `0`. An empty verdict also
exits `0`, so verify the expected checks are present. These codes describe diagnostic verdicts;
transport errors and tool denials follow their own failure paths.

The default check set comes from configured provider contributions and includes worker jobs.
The production factory currently does not filter those contributions by `--provider`; read each
row's scope instead of treating that flag as isolation. Neither production provider supplies the
optional guest-egress probe: `--with-egress` currently yields a diagnostics assembly error and
exit `6`. It is not enabled by staging a guest image.

Use [deployment diagnostic verification](doctor-exit-criterion.md) for fault injection, evidence
capture and the TLS, ACL, secret-coverage and unavailable-egress limitations. Check process health
before invoking diagnostics; a missing worker or unavailable check is not a passed contract.

## Shell completion

Completion is generated from the installed parser and needs neither a token nor a server.
Regenerate or reload it after upgrading the CLI. It completes command names and flags, not
runtime object IDs or other positional values.

For Bash with `bash-completion` and Bash 4 or later, add this to `~/.bashrc`:

```bash
source <(kdivectl completion bash)
```

For Zsh, add this after `compinit` in `~/.zshrc`:

```zsh
source <(kdivectl completion zsh)
```

## Reading the audit trail by `actor`

Platform audit rows classify callers from verified token claims as described under
[authentication](#authenticating). With authorized database access, inspect recent rows
attributed to the CLI:

```sql
SELECT ts, principal, tool, scope, platform_role, actor
FROM platform_audit_log
WHERE actor = 'operator-cli'
ORDER BY ts DESC
LIMIT 100;
```

This is the platform audit table, not a complete record of every CLI attempt. Client-side
refusals never reach the server, and platform denials from callers holding no platform role
are deliberately not recorded here. Project audit queries have their own
[tool and scope rules](../../guide/reference/audit.md).
