# `faultinject.inspect`: read-only guest fault-injection state (#3683)

Decision record: [ADR-0825](../../adr/0825-faultinject-inspect-via-optional-local-libvirt-port.md).
Parent: #918. Predecessor: #3682 (helper, ADR-0822). Successor: #3684 (set/reset).

## Problem

An agent cannot see which debugfs fault-injection knobs (`failslab`, `fail_page_alloc`) a ready
local-libvirt System supports or whether they are armed. The helper `kdive-faultinject read`
exists in the guest image, but no MCP tool, port or System-keyed SSH path reaches it.

## Scope

In: one MCP tool `faultinject.inspect(system_id)`; one optional provider port; one local-libvirt
implementation; exposure, tool-index, docs, generated CLI verb, coverage obligations; a live_stack
tool cell; live proof on a real x86_64 local-libvirt System. Out (owners in the charter): set and
reset (#3684), a ProviderSupport flag, task-filter and gfp flags, remote-libvirt `allowed_programs`,
openSUSE image staging, knobs beyond the two, other providers, a cap on `times`.

Ownership: no transition. The optional port sits beside `domain_presence` on `ProviderRuntime`;
`_live_ssh_argv` gains a `program` parameter defaulting to `kdive-drgn`, so its callers are unchanged.

### Contract

- Tool `faultinject.inspect`, read-only annotation, exposure `_VIEWER`, input `system_id` (UUID).
- Success `data`: `{"knobs": {"<knob>": {"present": bool, "armed": bool, <param>: int, ...}}}` for
  `failslab` and `fail_page_alloc`. Parameters are the helper's names: `probability`, `interval`,
  `times`, `space`, `verbose`, and `min-order` for `fail_page_alloc`. An absent knob is
  `{"present": false, "armed": false}`. `armed` is `probability > 0 and times != 0`.
- Errors: unknown or foreign-project System `not_found`; invalid id the existing invalid-id error;
  System not `ready` `readiness_failure`; viewer role missing is denied by `require_role`;
  provider without the port `configuration_error` `reason=capability_unsupported`; no SSH forward
  `configuration_error` `reason=ssh_not_provisioned`; helper exit 3 `configuration_error`
  `reason=debugfs_not_mounted`; exit 4 `reason=fault_injection_unsupported`; exit 127
  `reason=helper_not_installed`; each with a remediation sentence; ssh exit 255 `transport_failure`;
  other exit, reply over 4096 bytes, or invalid shape `infrastructure_failure`.
- Limit contract: the helper reply is capped at 4096 bytes per call, applied to guest stdout before
  parsing; an over-cap reply is `infrastructure_failure` and a retry changes nothing; the tool
  never returns truncated data.
- `suggested_next_actions`: `["systems.get"]`. The reset tool is not named because it does not
  exist yet (#3684, recorded as a follow-up).
- The wrapper docstring and `Field` text carry the contract above, including the definition of
  armed and the current `probability` and `times`.

### Failure model

1. Actors and deployments: an authenticated viewer-or-higher agent calling the server; the server
   host running the local-libvirt provider on loopback SSH to a kdive-built guest; the kdive
   operator rebuilding images. Not designed for: remote-libvirt, byo-host, other providers.
2. Invariants and assets: no caller text reaches argv or a path; the per-System bootstrap key is
   never logged or returned and its temp file is removed; the pool connection is not held across
   SSH; the response cannot exceed the byte bound or carry guest-authored strings.
3. Accepted failure classes: worst-case 80 s thread block per call (bounded by the SSH retry
   deadline plus one attempt, acceptable for a single read); a guest whose root has altered the
   helper returns `infrastructure_failure` or arbitrary validated integers (the guest is the
   debug target, not a trust anchor); concurrent calls on one System run in parallel (reads only).
4. Covered elsewhere: set/reset safety and a `times` cap (#3684); helper argv validation
   (ADR-0822, `tests/scripts/test_kdive_faultinject_helper.py`); redaction registry
   (`security/`); project isolation and role (`require_role`, existing exposure guards).

### Threat model

- Boundaries added: server to guest root exec (new program, fixed argv `read`); guest stdout to
  server parser. Widened: bootstrap key use by a viewer-level read.
- Actor: authenticated tenant at viewer on the System's project; untrusted guest output.
- Controls: project/role check before any provider call; fixed argv; loopback-only endpoint
  validation; 4096-byte cap; redactor then strict shape validation to ints; no stderr echo;
  key materialized in a temp file for the call only.
- Out of scope: a compromised guest forging integers; key theft from the server host; DoS by
  call volume (rate limiting is a platform concern).

## Success

1. A viewer on a ready local-libvirt System receives both knobs with present flag, parameter
   integers and `armed`.
2. Helper exit 3, 4 and 127 return `configuration_error` with the three reasons and remediation.
3. Foreign project, non-ready System, unsupported provider and invalid id return the categories
   above; a caller below viewer cannot see or call the tool.
4. Output is capped, redacted and shape-validated; invalid or oversized replies are
   `infrastructure_failure`.
5. The tool is read-only annotated, in the tool index and exposure map, and its wrapper carries the
   full contract.
6. On a real x86_64 local-libvirt System with a rebuilt image, the tool returns both knobs
   `present: true` and the configuration_error path is shown; the live_stack cell is added and run.

## Validation

- focused-test: parse and map exit codes, cap, redaction, argv. `tests/providers/local_libvirt/test_fault_inject.py`; red: module absent; green: `just test-verbose tests/providers/local_libvirt/test_fault_inject.py`.
- focused-test: handler behavior (success, not found, role, not ready, unsupported provider, no forward, error mapping, no pool connection held). `tests/mcp/debug/test_faultinject_inspect.py`; green: `just test-verbose tests/mcp/debug/test_faultinject_inspect.py`.
- focused-test: exposure, annotation, tool index, wrapper contract text, docs guards. Existing guards `tests/mcp/core/test_exposure.py`, `tests/mcp/core/test_tool_docs.py`, `tests/mcp/test_tool_index.py`; red: guard fails until registered.
- focused-test: `_live_ssh_argv` default unchanged. `tests/providers/local_libvirt/test_introspect_drgn.py`.
- task-test-not-applicable: composition wiring of the port is asserted by the existing composition test (extend one assertion); docs, generated verbs and obligations are checked by their generators' check recipes.
- Live proof: `tests/integration/live_stack/` cell, run on a rebuilt-image System; evidence on the issue.
