# Distinguish a missing domain from a missing SSH forward (#2502)

## Problem

`recorded_ssh_endpoint` on local-libvirt catches every `CONFIGURATION_ERROR` from the SSH endpoint
resolver and returns `None`, so two conditions — no libvirt domain for the System, and a domain
recording no loopback SSH forward — both surface as `reason="ssh_not_provisioned"` with a message
naming neither. #2480 was misdiagnosed on this. Decision and rejected alternatives:
[ADR-0658](../../adr/0658-missing-domain-is-not-a-missing-ssh-forward.md).

## Scope

Tag the resolver's two `CONFIGURATION_ERROR` raise sites with a `reason` detail and narrow the
`except` so only the no-forward reason becomes `None`. The no-domain error propagates, reaching
clients as `reason="system_domain_not_found"` with a message naming the missing domain and the
endpoint check #2480 needed. `None` keeps its ADR-0271 meaning; `ssh_not_provisioned` and its
detail text are unchanged.

No port signature change, so remote-libvirt and fault-inject are untouched and the five call sites
need no edit — they already route a `CategorizedError` through `failure_from_error`, which merges
scalar `details` into the response `data`. Changed: the local-libvirt connect module, the port and
consumer docstrings stating the contract, the generated tool reference, and the tests. Out of
scope: the same pair on this module's gdbstub transport (approved exclusion, #2502 charter) and
remote-libvirt's own domain-absent collapse (reported as a follow-up).

### Failure model

**Actors and deployments** — an MCP client calling `systems.ssh_info` or
`systems.check_ssh_reachable` on the single-host local-libvirt deployment; the `authorize_ssh_key`
and `check_ssh_reachable` worker jobs; a local operator reading a job failure. No anonymous or
cross-tenant caller reaches this code.

**Invariants and assets at stake** — the client-facing `data.reason` contract on two MCP tools and
two job kinds; `ssh_not_provisioned`'s meaning and detail text, pinned verbatim by three test
modules; the error-detail redaction boundary.

**Accepted failure classes**
- A client branching on `data.reason` takes its unknown path for the new value. Accepted: intended
  by ADR-0658, bounded to the no-domain condition, which previously returned a wrong value.
- The gdbstub raise sites keep sharing an undiscriminated category. Accepted: approved exclusion.
- `ssh_not_provisioned` still reads as a provider-capability gap rather than "reprovision this
  domain" for a pre-ADR-0281 local domain. Accepted: settled by ADR-0281, pinned by an existing
  test, outside this issue's criteria.

**Covered elsewhere** — remote-libvirt's domain-absent collapse: follow-up candidate from #2502,
unfiled. Endpoint-resolution correctness: #2480, merged.

### Threat model

**Boundary inventory** — widens one existing boundary, adds none: the MCP tool response, which now
carries a resolver message the `except` previously discarded.

**Actor model** — an authenticated project-scoped MCP caller holding VIEWER on the System;
`require_role` and the project check run before the resolver.

**Control per boundary** — `safe_error_details` reduces `details` to JSON scalars and forwards no
submitted value. The propagated message names the libvirt domain, which `systems.get` already
discloses to the same caller; no host path, credential, or libvirt URI enters it.

**Out of scope** — the redaction policy itself (ADR-0019/0123, unchanged) and whether a domain name
is disclosable at all (settled by ADR-0271's existing descriptor).

## Success

1. The no-domain and no-forward conditions return different `data.reason` values from
   `systems.ssh_info` and `systems.check_ssh_reachable`, and different `details["reason"]` from the
   two SSH job handlers.
2. The no-domain message names that condition and the endpoint check to make; the no-forward
   response keeps its existing reason and detail text byte for byte.
3. Both conditions remain `CONFIGURATION_ERROR`.

## Validation

- **`recorded_ssh_endpoint` returns `None` only for the no-forward reason.** focused-test —
  `tests/providers/local_libvirt/test_connect.py`, one case per condition over the existing
  fake-connection helper; the no-domain case is red before the `except` narrows. Green:
  `just test-verbose tests/providers/local_libvirt/test_connect.py`.
- **The no-domain error reaches an MCP response as the new reason.** focused-test —
  `tests/mcp/lifecycle/test_systems_ssh_access.py`, a raising fake connector for both tools. Green:
  `just test-verbose tests/mcp/lifecycle/test_systems_ssh_access.py`.
- **`ssh_not_provisioned` is unchanged for the no-forward case.** focused-test — the pinned
  assertions in the three consumer test modules stay green unedited.
- **Generated tool reference matches the registrar docstrings.** focused-test — the reference is
  generated from the live registry, so a changed docstring must be regenerated with `just docs`;
  red before regeneration, green: `just docs-check`.
- **Port and consumer docstrings state the narrowed contract.** task-test-not-applicable — prose
  describing a contract the focused tests prove; no executable consumer reads it.
