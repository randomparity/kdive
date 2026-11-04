# BYO host inventory and refusing runtime

## Problem and authority

Issue #1817 is the inseparable enum, database, inventory and runtime prerequisite for
#1814. The operator approved this bounded prerequisite for #1834 on 2026-11-04,
including neutral coefficient 1.0, exclusive capacity 1, shared-HMC LPAR addressing,
and unavailable profile examples. The frozen charter is the issue's WORK:SCOPE
q1817-6aee31c6. Operational plane owners remain #1818–#1834 as listed there.

## Design

Extend existing inventory ownership and resolver seams; no responsibility migration
or compatibility facade is needed. Inventory remains the sole creator and writer of
declared BYO facts. Runtime capabilities and scheduling remain their existing owners.

`ByoHostInstance` extends the inventory instance fields with `arch` restricted to
`x86_64` or `ppc64le`, `ssh_target`, `ssh_key_ref`, `oob`, `baseline_kernel`,
`console_device`, positive strict integer `vcpus`/`memory_mb`, default `cost_class=byo`,
`pool=default`, cap exactly integer 1, and strict boolean `pseries_fadump=False`.
`oob` has kind redfish/ipmi/hmc, endpoint, username_ref/password_ref, plus
managed_system/lpar_name required only for HMC. POWER requires HMC and x86 rejects
HMC; x86 rejects fadump true. All connection/config models forbid unknown fields,
reject empty/whitespace strings, and never accept inline password/key fields.
Reference names identify secrets; this change neither resolves nor logs material.

Within one parsed document names and SSH targets are unique. Redfish/IPMI endpoint
strings are unique across those two kinds. HMC identity is the tuple
(endpoint, managed_system, lpar_name), allowing distinct LPARs on one HMC. Comparison
is over declared strings; aliases that differ across endpoints remain operator-owned.
ADR-0813 records this narrow correction to Proposed ADR-0540 without accepting the
future epic planes. Structural parse errors name entry and field without echoing
supplied secret values. Schema validation performs no DB, SSH, OOB, or object-store I/O.

The single forward migration 0190 widens resources_kind_check, seeds `byo` with
ON CONFLICT DO NOTHING, adds nullable object-shaped systems.byo_adopt_facts JSONB,
and creates Resource-keyed byo_console_leases with UUID id, Resource FK, mode
reading/preempting, holder_worker_id text, acquired_at and expires_at. Expiry must be
after acquisition. A partial unique Resource index covers preempting rows; shared
reading rows are allowed. Cross-mode admission and dead-worker reclaim remain #1818,
which must lock the Resource before checking/inserting. Worker identity uses the
existing text incarnation format, not a UUID coercion. No lease writer ships here.
Migration 0190 lands after foreign #3538's 0189; root owns merge sequencing.

Reconcile keys BYO by (kind,name), never adopts unrelated discovery rows. Host URI is
the declared SSH target. The merged capabilities contain positive size/cap fields,
`guest_arches={arch: {accel: none, emulator: none}}`, the explicitly declared
pseries_fadump bool, and `byo_host` containing the validated refs-only declaration.
The nested declaration is retained for lossless export; public capability projection
emits its established allowlist, not this private config. Unknown runtime keys,
cordon state and UUID survive re-reconcile. Removed/detached/file-departed resources
use existing override, prune and cordon semantics without a second implementation.
Override GC includes BYO identities and compares their full declared facts before
clearing a detached entry; a still-declared removed entry remains suppressed.

InventorySnapshot gains default-empty typed BYO entries. Snapshot reading validates
stored declaration, overlays live name/SSH target/cost/pool/capacity/cap, and fails
before export if required config is missing. Serializer emits all fields and nested
OOB with existing TOML escaping. Thus writeback cannot silently drop a BYO host;
export, parse and reconcile preserve its identity and credential references.

An opt-in runtime descriptor uses valid inventory presence unless explicitly enabled
for buildability tests. Missing/malformed inventory disables BYO without crashing
unrelated provider startup. Discovery is creates=False and never calls its target.
Required typed ports and policy use explicit refusing methods raising existing
not_implemented before I/O. All optional operational ports stay absent and
ProviderSupport stays empty/false. platform_root_cmdline is explicitly None. Binding
is deliberately absent until #1823 because these ports perform no operation.

Provider section metadata marks BYO unavailable with no usable profile model. The
profile examples tool emits a BYO item with provider, available=false, profile=null
and an actionable note that provisioning is unavailable; it never falls through to
local-libvirt. Schema aliases for live profile admission omit unavailable providers.
The existing runtime registration tool explicitly refuses BYO (config remains its
sole creator), and the host-capacity setter refuses non-1 BYO caps; scheduling cordon
remains the existing way to freeze a host without violating its declared cap.
Wrapper documentation describes this result and generated references are regenerated.

## Success

All four enum/CHECK/runtime kinds agree; opt-ins default off without declarations.
Valid x86 and POWER declarations, including two LPARs on one HMC, parse without live
services. Malformed declarations and duplicate coordinates fail before side effects.
Reconcile twice yields one UUID and no second diff, retains runtime keys/scheduling,
and honors override/removal semantics. Exported TOML survives quotes/newlines in
accepted values and produces the same declaration. Opposite architecture fails the
existing admission reader and native none sentinels survive. Every skeleton port
refuses without network, secret resolution or host activity. No BYO profile is invented.

## Failure model

Named deployment: operator-configured BYO inventory in the existing HTTP/worker
service and reconcile/export tools; no operational BYO host plane is shipped.

- Malformed or secret-bearing declarations: reject at the parser; startup disables
  BYO while schema check reports entry.field without supplied secret values.
- Duplicate string coordinates: reject before reconcile; distinct endpoint aliases
  and concurrent independent inventory sources are outside the single-file guarantee.
- Repeated reconcile/overrides/runtime facts: existing keyed upsert and override locks
  retain UUID and runtime-owned state; declared fields overwrite their own keys only.
- Export/writeback omissions or TOML injection: persist the full validated declaration,
  use established scalar escaping, and refuse missing persisted declaration.
- Premature lifecycle/profile use: empty advertised support and refusing typed ports;
  explicit unavailable examples rather than a fabricated operational profile.
- Lease cross-mode races/dead holders: table shape only here; #1818 owns locked
  acquisition/reclaim before any lease writer is enabled.
- Physical machine aliases and hardware truth: operator responsibility, not inferred;
  fadump is false unless explicitly declared and remains operationally unavailable.

## Validation

The implementation plan inventories focused behavior, expected red observations,
Linux tests and guardrails. Unit tests precede implementation; real PostgreSQL proves
DDL/coefficients/reconcile/leases. Mac execution is fast-loop evidence only. Docker
Linux supplies regression evidence, including the ordinary suite; installed pre-push
just ci owns final full local verification. Live hardware tiers are inapplicable to
ports that refuse all host operations and stay with #1833/#1834.
