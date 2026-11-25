# External-boot value ownership

## Problem

Durable activation models import provider-neutral serialized values from the provider
port module. That module combines forty value declarations with four operation protocols,
so domain persistence depends on a provider-interface owner.

## Scope

Move the complete value closure from `providers/ports/external_boot.py` unchanged into
`domain/external_boot_values.py`: aliases, six bounds/device constants, canonical base,
identity/NFC/argument helpers, artifact/ownership/root/plan/materialization/observation/
recovery/preparation/object models. Keep the four protocol definitions at their current
owner, with normal domain value imports for annotations. Preserve names and bodies.
Migrate direct in-tree source/test imports, splitting mixed imports and handling local
imports. Include module_documents' private base/NFC imports. Add no compatibility aliases,
wrappers or base/serializer unification; `_ClosedEvidence` remains separate.
Update the existing returned-value declaring-module assertion to the new neutral owner.
Keep serialized schemas, aliases, validator/field order, bounds, optional debuginfo omission,
canonical encodings, digest prefixes and operation signatures unchanged. Import-path changes
are internal; documented MCP/wire and provider operation contracts remain preserved.

### Failure model

- Actors: in-tree core/database/providers and deployment/tests consume shared values;
  callers use the existing serialized/tool/operation contracts.
- Invariants: domain values do not depend on providers; canonical bytes, identities,
  concrete schemas and provider operation signatures retain their current meaning.
- Accepted: declaring-module metadata changes to the domain owner; imports are migrated.
- Covered elsewhere: provider IO, locking, authorization and journal policy remain their
  existing owners; this unit changes no calls, fields or validation algorithms.

## Success

1. Domain activation/value modules have no provider import, and the port module owns
   only its four protocol declarations. Direct value users import domain definitions.
2. Forty moved declarations and retained protocols preserve their complete AST bodies;
   other algorithm bodies change only their value import bindings.
3. Concrete schemas, golden canonical values/identities and SQL/Python equivalence pass.

## Validation

- `Mode: focused-test`: extend the existing contract import-boundary test for domain
  independence and protocol-only declarations. Current domain-to-provider import and
  mixed definitions must fail; controlled reverse-import/definition faults must bite.
- `Mode: task-test-not-applicable`: pure relocation adds no value algorithm; compare
  moved/retained ASTs and capture concrete model schemas before/after. Update the
  provider-neutral returned-value owner assertion for its intended structural contract.
- Inventory direct imports across source/tests/deployment/scripts, including function-local
  and private helpers; prove remaining old-owner imports name only the four protocols.
- Run existing port golden values, domain activation, bounds, plan, discriminator and
  real-DB activation migration/repository proofs (plan/materialization identities with
  and without debuginfo), then affected provider/authority regressions, lint, whole-tree
  type, documentation/schema guards and hooks. No live hardware qualification is claimed.
