# Use production provider runtime assembly in its callers

## Problem and outcome

The assembly facade exposes three standalone runtime wrappers alongside the descriptor-driven
production resolver. Tests inspect advertised capabilities through the wrappers, and the coverage
inventory uses separate provider-owned builders. This allows checks to bypass production wiring.
Use the existing production resolver for policy checks and the existing concrete builders for
provider-internal tests, then remove the unused alternative wrappers.

## Contract assessment and ownership

The removed exports are `build_local_runtime`, `build_remote_runtime` and
`build_fault_inject_runtime` in provider assembly. Caller inventory finds only in-tree tests of
these wrappers; the coverage script independently aliases two leaf builders with the same names.
[ADR-0041](../../adr/0041-versioning-release-process.md) protects MCP tools, response/error taxonomy
and persisted state, not these internal Python imports. User authorization covers internal
interface/architecture changes. Remove the wrappers and their `__all__` entries without aliases.
All three provider-owned `build_runtime` entry points retain their signatures and implementations.

Production remains `_RuntimeDescriptor.build_runtime` and
`ProviderComposition.build_provider_resolver`. Keep every descriptor, enabled predicate, factory,
dependency argument, registrar and reaper unchanged. No new assembly framework or API is needed.
The completed inventory-registration prerequisite means local has the actual registrar and
remote/fault/BYO have `None`; generic explicit `creates=False` safety remains implemented.

### Caller transitions

- Advertised capability, discovery and registration checks use `ProviderComposition` and resolve
  the required kind. Fixed provider sets explicitly pass all four enable flags; tests of actual
  environment/default opt-in retain those inputs. A small helper confined to the main composition
  test may replace its many identical fixed-kind constructions; add no shared test-builder module.
- Internal engine/hotplug and concrete remote-port/build-without-config assertions call the existing
  provider-owned builders with unchanged inventory, engine, behavior and registry arguments.
- MCP real-local resolver helpers use a local-only production resolver. Other fake handlers remain
  unchanged. Pinned capture checks use exactly local and remote and retain their asserted methods.
- Coverage `read_inventory` uses one explicit local+remote resolver, with fault/BYO disabled.
  Its two keys and `ProviderSupport` values, inventory/contract digests and matrix cells stay equal.
  No coverage obligation, evidence, mapping or snapshot changes are intended.
- Replace the wrapper-only wiring test with stronger assertions in the existing resolver wiring
  test: same registry/store reach local and remote, same store reaches fault-inject, exact non-null
  remote authority factory reaches the remote builder, and local authority default remains absent.
  Retain local registrar/None inventory registration, generic false guard, real-PG stable local
  registration and provision-to-reaper shared inventory assertions.
- Delete the obsolete MCP AST test file that only forbids a wrapper being removed. Add a bounded
  no-alternate-export check in the existing composition test. Keep unrelated boundary guards.

### Parity regression from the prerequisite

A focused baseline run of the two capability parity checks produced one failure and one pass:
`port:discovery_registrar` is now local-only. The old remote no-op masked this intentional ownership
difference. Add one reasoned entry to the existing `_WAIVERS` table citing
[ADR-0112](../../adr/0112-systems-inventory-config.md): local discovers its host, while inventory
creates remote resource rows. Keep every other table entry and both parity predicates unchanged.
This records implemented policy; it neither removes capabilities nor waives a health finding.

### Failure model and threat model

- Actors: process assembly, capability checks, MCP admission tests and coverage inventory readers.
  Assets: provider opt-in/isolation, correct capabilities, shared secret redaction registry/store,
  authority routing, local discovery and resource identities, accurate coverage obligations.
- Trust crossings: operator enables select provider factories; shared dependencies enter privileged
  provider ports; discovery enters host/database work; the coverage inventory becomes declared
  obligations. Production assembly remains byte/AST preserved except removed wrappers/imports.
  No test or inventory read may start provider operations or access a live host/service.
- Explicit fixed sets must not accidentally depend on ambient inventory or fault/BYO flags. Keep
  environment-driven tests distinct. Callback identity assertions must pass through real builders
  rather than create a second test-only assembly implementation or exercise remote connections.
- Accepted: internal wrapper importers migrate together, with no compatibility alias. The existing
  leaf defaults remain for provider-internal construction. Local-only discovery remains a documented
  parity exception; other capabilities and exception reasons are unchanged.
- Excluded: public MCP/wire/error/domain/port changes, persisted data/schema/migrations, leaf/runtime/
  resolver/descriptor/gate policy, authority behavior, dependencies/config/floors, broader cleanup,
  historical records, host/VM/libvirt/SSH/stack mutations and external writes. No new ADR is needed.

## Success and proof

1. No runtime wrapper definition/export/caller remains in source, scripts or tests. Historical docs
   remain revision records. Both coverage and advertised-runtime checks use production policy.
2. Production dependency identities, enabled/default behavior, concrete ports, shared inventory,
   local registration and inventory-owned absence remain tested without weaker assertions.
3. Baseline parity failure becomes green through the single documented intentional exception;
   stale-exception detection and every other parity assertion remain intact.
4. Inventory and full contract digests/cells match the pre-edit baseline. All other script functions,
   production assembly functions/classes and provider/core/SQL bytes are preserved.
5. All migrated and related provider, capture/coverage, authority-sender and MCP/database tests pass;
   deliberate removed-export, dropped-store/factory and missing-local-registration faults fail.
   Build/install the actual wheel, check bytes/origins and run relevant behavior against installed
   runtime code. Distinguish script/source-only structural proof from packaged runtime proof.
6. Run lint, whole-tree types, documentation checks and hooks; independent bounded code/security
   reviews; conventional local commit, exact health attribution and next. No forced rescan.

## Global Constraints

Python 3.14, managed with `uv`. Declared targets are Linux x86_64 and ppc64le; host verification is
Linux x86_64 and does not claim live hardware coverage. Preserve public contracts and persisted
bytes. Use existing dependencies and project guardrails. Keep all commits and changes local.
