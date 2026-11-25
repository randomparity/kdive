# Authority boundary value ownership

Decision: [ADR-0868](../../adr/0868-authority-boundary-value-ownership.md).

## Problem

Authority transport imports `AuthenticatedPeer` from the authentication SQL
repository and `AuthoritySystemServiceError` from the concrete System service.
These shared boundary values belong with their existing protocol contracts.
Moving the exception alone cannot isolate protocol consumers: importing the
System protocol currently runs its package initializer, which eagerly imports
the service, journal and repository. A fresh-interpreter assertion reproduces it.

## Scope

Move both unchanged class definitions into their respective existing authority
protocol modules. Migrate all actual source/test consumers together. Database
authentication, credential hashing, SQL and database-owned errors stay with the
repository. No duplicate definitions or intentional old-module compatibility
re-exports. Ordinary imports needed by producers are not compatibility APIs.

Preserve the System package's existing exports and `__all__`. Defer only its
`AuthoritySystemService` export until requested. Keep the protocol error export
direct, static type-checker visibility and ordinary submodule imports intact.
Do not add a general lazy loader, export cache or new introspection contract.

No authentication policy, wire model, category, schema, dependency, deployment,
provider operation, journal/fence or public tool change. Other queue owners and
the campaign's final integration/review obligations remain separate.

## Design

The frozen, slotted `AuthenticatedPeer(incarnation_id: UUID | str)` moves into
`providers/external_boot_authority/protocol.py`. It is evidence returned by the
trusted authentication callback; constructing the value never authenticates anyone.
`AuthoritySystemServiceError` moves into `providers/system_authority/protocol.py`.
Its `RuntimeError` inheritance, constructor, category literals and message remain
identical. Producers and consumers import the same canonical classes.

The System package imports its service class under `TYPE_CHECKING` for static
exports. A module `__getattr__` handles only `AuthoritySystemService`, importing
and returning the existing class on demand. Unknown attributes raise normal
`AttributeError`; `from package import *` still follows the unchanged `__all__`.
Explicit service imports retain their normal initialization and failure behavior.

Protocol-value imports must not load the authentication SQL repository or System
service. This is not a claim that the complete transport import is SQL-free:
`module_protocol` also imports `teardown`, whose snapshot uses the database-owned
`AuthorityBinding` record. That separate dependency remains unchanged. No Binding
relocation or broader package cleanup belongs to this class-ownership repair.

## Failure model

- Actors/deployments: trusted authority-host assembly, SQL producers and in-tree
  transport/service consumers on supported Linux x86_64 and ppc64le; local proof
  uses real x86_64 and disposable Postgres, not hardware qualification.
- Assets: authenticated peer identity, credential rejection, exact error mapping,
  protocol bytes, authority/worker fences, journal state and supported exports.
- Accepted classes: callers explicitly requesting the concrete service still
  load its dependencies and may encounter their existing import failures. The
  unrelated transitive database-record dependency described above remains.
- Covered elsewhere: actual authentication and authorization, provider mutation
  isolation and live/hardware proof retain their existing enforcement and suites.

## Success

1. Each shared value has exactly one protocol-owned definition; all actual
   consumers migrate. Protocol-only imports do not initialize their former owners.
2. Existing package member/import/star exports preserve canonical identity;
   requesting the service loads it, and unknown attributes remain unavailable.
3. Actual provider-authority-role authentication returns the canonical peer and
   still rejects invalid, inactive and incompatible credentials.
4. Both System transport operations retain all three categorized error responses,
   authentication-before-service and unexpected-error privacy behavior.
5. Authentication SQL, unrelated production bodies, existing wire models and all
   198 applied migrations remain unchanged. Types, caller suites and hooks pass.

## Validation

Use fresh child interpreters for import isolation and package exports; an AST
boundary guard rejects old concrete-module value imports. Extend the existing
real-role typed-reader test with canonical type identity, retaining credential
rejection. Exercise the real transport dispatcher with categorized service
failures for both operations; keep existing journal/service/connected caller proofs.
Confirm controlled import/error-classification faults fail, restore exact bytes,
then run scoped tests, lint, whole-tree types, hooks and independent code/security
review. Commit and attribute actual source work. No live/hardware claim follows.
