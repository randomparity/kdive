# 0851 — Shared lifecycle services own admission and reclaim

## Status

Accepted (2026-11-23)

## Context

System teardown admission and reconciler repairs import helpers under worker handlers.
Importing the external-boot package also imports its registrar and executable operation tree.
Both helpers are shared core orchestration, with database/provider contracts already defined.

## Decision

Move activation-derived payload construction to `services/external_boot/payloads.py` and
post-provider-absence core cleanup to `services/systems/reclaim.py`. Redirect their direct
production/test readers and evidence paths; remove the old internal modules. Keep the existing
reverse-admission guard separate. Preserve moved executable AST, signatures, payload/timing/PREP
policy, caller transactions, locks, fenced discharge and bounded retirement/failure behavior.
No package exports or replacement forwarding layer are added.

## Consequences

Shared consumers load service helpers without loading worker handlers. Worker execution still
calls the same logic with the same authority flags. New internal module locations and logger
namespace follow the service owner; messages and levels stay. Public protocols, persisted data,
SQL and privileges are unchanged. Cold-import tests protect the dependency direction.

## Considered & rejected

- **Leave the helpers under handlers.** verified: at764abbc1d, the two fresh-process tests in
  the private shared-lifecycle reproduction fail in1.11s, reporting worker-owned functions and
  loaded handler modules for both shared consumers. The external-boot package initializer
  directly imports its registrar, so importing that helper brings executable handlers with it.
- **Keep forwarding modules.** judgment: loading the old package keeps its unwanted import
  side effect; these internal paths have no retained public compatibility contract.
- **Add timing injection or a general lifecycle framework.** judgment: existing service timing
  policy and two existing helper implementations can move without changing their interfaces.
