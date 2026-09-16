# Distinguish a missing domain from a missing SSH forward (#2502)

## Problem

`recorded_ssh_endpoint` on local-libvirt catches every `CONFIGURATION_ERROR` from the SSH endpoint
resolver and returns `None`, so two conditions — no libvirt domain, and a domain recording no
loopback SSH forward — both surface as `reason="ssh_not_provisioned"` with a message naming
neither; #2480 was misdiagnosed on this. Decision, alternatives, and the ADR-0298 `None` contract
this narrows: [ADR-0658](../../adr/0658-missing-domain-is-not-a-missing-ssh-forward.md).

## Scope

Tag the resolver's two `CONFIGURATION_ERROR` raise sites with a `reason` detail and narrow the
`except` so only the no-forward reason becomes `None`. The no-domain error propagates, reaching
clients as `reason="system_domain_not_found"` with a message naming the missing domain and the
endpoint check that misdiagnosis needed. `None` keeps its ADR-0298 meaning; `ssh_not_provisioned`
and its detail text are unchanged.

No port signature change, so remote-libvirt and fault-inject are untouched and the five call sites
need no edit — each already routes a `CategorizedError` through `failure_from_error`, which merges
scalar `details` into the response `data`. Changed: the local-libvirt connect module, the port and
consumer docstrings, the generated tool reference, and the tests. Out of scope: the same pair on
this module's gdbstub transport (approved exclusion, #2502 charter) and remote-libvirt's own
collapse of absent-parity, absent-domain, and absent-port to `None` (a follow-up).

### Failure model

**Actors and deployments** — on the single-host local-libvirt deployment: an MCP client calling
`systems.ssh_info` / `systems.check_ssh_reachable` (VIEWER) or `systems.authorize_ssh_key`
(CONTRIBUTOR, mutating); the two SSH worker jobs; an MCP client attaching drgn-live via
`debug.attach`, which reads the same raise sites through `open_transport` and never swallowed them;
a local operator reading a job failure. No anonymous or cross-tenant caller reaches this code.

**Invariants and assets at stake** — the client-facing reason contract on three MCP tools, the
drgn-live attach path, and two job kinds; `ssh_not_provisioned`'s meaning and detail text, pinned
verbatim by three test modules; the error-detail redaction boundary, which keeps reducing `details`
to JSON scalars.

**Accepted failure classes**
- A client branching on the reason takes its unknown path for the new value. Accepted: intended by
  ADR-0658, bounded to the no-domain condition, which previously returned a wrong value.
- The gdbstub raise sites keep sharing an undiscriminated category. Accepted: approved exclusion.
- `ssh_not_provisioned` still reads as a provider-capability gap rather than "reprovision this
  domain" for a pre-ADR-0281 local domain. Accepted: settled by ADR-0281, pinned by an existing
  test, outside this issue's criteria.
- The propagated message names the libvirt domain, which is `kdive-<system_id>` for the caller's
  own System. Accepted: it derives from the id the caller supplied and carries no host path,
  credential, or libvirt URI.

**Covered elsewhere** — remote-libvirt's own collapse: follow-up candidate from #2502, unfiled.
Endpoint-resolution correctness: #2480, merged. Detail redaction policy: ADR-0019/0123, unchanged.

## Success

1. The no-domain and no-forward conditions return different reason values from `systems.ssh_info`,
   `systems.authorize_ssh_key`, and `systems.check_ssh_reachable` (`data.reason`) and from the two
   SSH job handlers (the worker prefixes detail keys, so a client reads `failure_detail_reason`).
2. The no-domain message names that condition and the endpoint check to make; the no-forward
   response keeps its existing reason and detail text byte for byte.
3. Both conditions remain `CONFIGURATION_ERROR`.

## Validation

- **`recorded_ssh_endpoint` returns `None` only for the no-forward reason, and each raise site
  carries its own reason.** focused-test — `tests/providers/local_libvirt/test_connect.py`, one case
  per condition over the existing fake-connection helper plus `details["reason"]` assertions on the
  two existing raise-site cases; the no-domain case is red before the `except` narrows. Green:
  `just test-verbose tests/providers/local_libvirt/test_connect.py`.
- **The no-domain error reaches all three MCP tools as the new reason.** focused-test —
  `tests/mcp/lifecycle/test_systems_ssh_access.py`, a raising fake connector exercising `ssh_info`,
  `authorize_ssh_key`, and `check_ssh_reachable`. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_ssh_access.py`.
- **Both job handlers propagate it rather than collapsing it.** focused-test —
  `tests/jobs/handlers/test_ssh_{authorize,reachable}.py`, one case each asserting the raised error
  carries `details["reason"] == "system_domain_not_found"`. Green:
  `just test-verbose tests/jobs/handlers/test_ssh_authorize.py`.
- **`ssh_not_provisioned` is unchanged for the no-forward case.** focused-test — the pinned
  assertions in the three consumer test modules stay green unedited.
- **Generated tool reference matches the registrar docstrings.** focused-test — it is generated from
  the live registry, so a changed docstring must be regenerated with `just docs`; red before that,
  green: `just docs-check`, with cross-references resolving under `just docs-links`.
- **Port and consumer docstrings state the narrowed contract.** task-test-not-applicable — prose
  describing a contract the focused tests prove; no executable consumer reads it.
