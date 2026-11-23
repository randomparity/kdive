# Compose inspect-proxy retirement

## Authority and scope

The eight-field authority is `.desloppify/task-93-charter.md`, assessed as small before design.
Deployment security requires a full specification.
The queued compose_retains_unused_death_proxy finding names a service recovery no longer uses.
The reproduction confirms the service remains in the rendered deployment; the baseline passed
118 selected tests.
Remove only the proxy service/network/server edges and HTTP implementation, retaining the
existing gate at its import path. Preserve the marker constructor, endpoint validation and
setting name/default/parser; clarify help and regenerate config docs. Replace the obsolete
Compose expectation and remove only the retired HTTP permission test. The earlier exploratory
endpoint-ignore proposal is not an accepted defect. No new runtime owner or interface is needed.

## Failure model

Actors are local Compose operators and recovery clients; assets are exact worker identity,
retained terminal evidence, generation pins and named volumes. Docker/Kubernetes registration
and exact-holder terminated-row enforcement stay. Database or inspect failures retain evidence
and pins. Unsupported raw Docker removal can strand pins but never becomes termination proof.
The existing supported stop/select/up procedure owns gated orphan removal and volume retention;
this retirement does not authorize protocol-3 upgrades or changes to witness/worker ordering.

## Threat model

Remove the unused root HTTP listener and Docker socket mount, without migrating callers to a
replacement listener. The existing bounded local Docker CLI and credential-bound durable
witness remain authoritative. Preserve identity/label/nonce/credential/terminal-state/role/SQL
checks and default network connectivity. The proof owns only its unique Docker resources and
credential; no existing user stack, host provisioning, dependency or applied migration changes.

## Validation

The private plan supplies the rewritten config test, focused regressions, exact private artifact
helper and commands. Observe the config expectation RED before edits, then GREEN, and prove
its absence/registration assertions with controlled faults and restoration. Verify retained AST,
marker, supervisor and configuration contracts, the rendered graph and all 198 SQL files. Run guards,
config generation and hooks. Before real isolated lifecycle operations, verify the image's
candidate commit and source hashes; run just test-compose-lifecycle with owned-resource cleanup.
One bounded design review, one scope audit and fresh scoped code/security reviews precede
resolution. The isolated proof covers the gate, Docker Compose and Postgres; the full application
stack, remote providers and POWER hardware require separate verification.

## Global Constraints

Python 3.14 managed with uv; use the repository's just recipes and existing dependencies.
Host: Linux x86_64; declared targets: x86_64 and ppc64le. Preserve public
contracts and applied migrations; use local conventional commits, with no external writes.
