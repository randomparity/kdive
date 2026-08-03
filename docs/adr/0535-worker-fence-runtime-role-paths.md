# 0535 — Worker-fence runtime role paths

## Status

Accepted (2026-08-02)

## Context

ADR-0533 separates worker-fence ownership from lifecycle termination evidence and revokes direct
protected-table access from runtime process roles. Deployment-role tests showed that three required
runtime paths were consequently unavailable: the server could not list uses for operator diagnosis,
the server could not carry an authorized recovery request through the evidence-checked transition,
and the reconciler could not test exact generation pins before garbage collection.

Granting the server raw protected-table reads or writes would bypass the intended authority boundary.
Granting the reconciler worker authority would let it acquire or release another attempt's fence.
Platform operator authority also cannot imply tenant-data access: ADR-0043 keeps platform and project
roles orthogonal.

## Decision

Add two bounded `SECURITY DEFINER` functions owned by the migration role. The server may list at most
100 oldest-first build-use rows per request through a function that accepts the caller's projects on
which it holds at least `viewer`. The function joins use to generation to investigation and returns
only rows whose authoritative project is in that granted set. A platform-only caller supplies an empty
set and receives an empty list.

The server and reconciler may invoke one exact recovery function. Its inputs include the use UUID, the
caller's authorized project set, the expected holder, actor, and reason. It returns false unless the
use joins to a project in that set, the exact holder matches, and the immutable worker-incarnation row
contains terminal evidence. A foreign-project use, absent use, holder mismatch, or missing evidence is
therefore indistinguishable to the caller and leaves the pin in place. On success, the function locks
the incarnation and exact use lineage, copies database-derived evidence to the permanent recovery
ledger, and deletes only the matching pin. The server keeps this transition and its platform request
audit in one transaction and retains no direct protected-table mutation authority.

The reconciler receives column-level `SELECT` only on
`investigation_build_uses(investigation_id, generation)`. This is sufficient for exact pin exclusion in
generation garbage collection and does not reveal holder credentials, job attempts, or recovery
evidence. It may call the same project-scoped recovery function when it has an explicit project scope;
it receives no worker credential functions and cannot publish termination evidence.

The MCP list and recovery surfaces require both `platform_operator` and at least `viewer` on the
pin's project. The application derives and de-duplicates that project set from the verified request
context before invoking either database function. List output remains capped at 100 rows. Holder and
reason remain capped at 512 UTF-8 bytes. These are per-request limits with no reference clock; excess
list limits are clamped, and invalid recovery inputs leave the use pinned for a corrected retry.

This decision partially supersedes ADR-0533's runtime process authority mapping. ADR-0533's worker,
lifecycle-witness, protocol, deployment, and evidence decisions remain in force.

## Consequences

Operators can diagnose and recover stranded pins under the deployed server credential without gaining
cross-tenant visibility or direct table access. A platform operator must also receive an ordinary
project role for each tenant whose pins it may inspect or recover. Platform-only operational accounts
see no tenant pins.

Generation garbage collection can preserve exact live pins under the reconciler credential without
acquiring broader worker authority. Recovery continues to fail closed when authorization scope,
holder identity, terminal evidence, audit persistence, or exact-row deletion does not hold.

Runtime-role deployment tests must connect through the actual server, reconciler, worker, and witness
DSNs. They prove direct protected-table access is denied, function grants are exact, mixed-project
lists do not leak foreign rows, foreign and missing recovery requests have the same refusal shape, and
generation garbage collection preserves only exact pinned generations.

## Considered & rejected

- **Grant server `SELECT` or `DELETE` on build uses.** This makes the application role the protection
  boundary and permits unbounded inspection or mutation outside the audited transition.
- **Let `platform_operator` span all tenant data.** ADR-0043 deliberately separates infrastructure
  authority from project-data authority; combining them creates an implicit global tenant reader.
- **Resolve the project after an unscoped list or lookup.** The returned row already discloses tenant
  data. Project filtering must happen inside the bounded definer query.
- **Give the reconciler worker-role membership.** Garbage collection needs two pin-key columns, not
  credential-bound acquisition and release authority.
- **Treat lease expiry as recovery evidence.** A cancelled provider thread may continue after lease
  loss, so time cannot prove that artifact consumption stopped.
