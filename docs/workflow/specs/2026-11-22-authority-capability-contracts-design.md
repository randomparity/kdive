# Declared authority execution capabilities

## Problem

The remote coordinator accepts a six-method protocol but casts it to three other
required operation contracts. Module lifecycle handlers guard the declared module
capability, then dispatch through an unrelated executor hidden behind `Any`.

## Scope

Compose the four existing narrow remote operation protocols into one coordinator
requirement. Annotate its constructor with that requirement and remove the nine
capability casts; keep the narrow protocols and unrelated casts. Production
composition already supplies the concrete operations implementation; preserve its
fifteen method signatures and runtime dispatch without new detection or fallback.
In a separate commit, send module restore/reap through `authority.modules`, remove
the helper's executor parameter and update its three calls. Replace the lazy `Any`
bridge with the existing service function's explicit keyword-only signature and
`ModuleCompletion` result; retain lazy service loading. Narrow only that service
function's sender annotation to `ModuleAuthority`, retaining broader preparation
requirements elsewhere. Preserve operation/capability/receipt guards, deadlines,
cancellation and obligation evidence writes. Add no new provider capability.

### Failure model

- Prevent: accepting incomplete coordinator operations and routing through an
  executor whose declared contract does not contain module lifecycle methods.
- Detect: incomplete production wiring, wrong sender identity, broken skip/refusal
  guards or changed conflict/observation/teardown behavior.
- Recover: revert the local interface repair; no durable format changes.
- Accepted: isolated tests may retain explicitly partial doubles for one exercised
  path; this repair adds no runtime structural checks or optional fallback.

## Success

1. The coordinator declares its complete existing method requirements.
2. Module lifecycle uses its declared capability through a typed lazy bridge.
3. Existing lifecycle evidence and provider-operation behavior remain unchanged.

## Validation

- `Mode: focused-test`: a private checked type witness accepts the six-method
  dependency before and rejects it afterward; whole-tree `just type` validates
  concrete production assembly. Existing remote authority and host tests prove
  conflict, observation and teardown behavior; keep partial test doubles explicit.
- `Mode: focused-test`: extend existing lifecycle operation cases to assert sender
  identity; demonstrate wrong routing fails before repair. Cover absent capability,
  unsupported operation and missing receipt, retaining existing deadline and
  obligation tests. Run lifecycle, remote module host and remote authority tests.
- Run `just lint`, whole-tree `just type`, documentation checks and configured
  hooks before each local conventional commit.
