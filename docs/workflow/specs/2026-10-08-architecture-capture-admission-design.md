# Architecture-sensitive System capture admission

## Problem

The explicit fadump branch returns after checking the provider-wide descriptor,
bypassing guest-profile architecture. A current-source reproduction on a valid
x86_64 profile returns FADUMP instead of capability_unsupported. Existing provider
capability and ppc64le tests pass because their intended static superset is valid.

## Scope

#3153; domain architecture selector, vmcore admission and existing System/resource
read wrappers with owning tests. ADR-0757 defines a new optional System response
field; the operator approved that interface on 2026-10-12. Provider-wide
descriptors/resources remain unchanged under ADR-0349. Native POWER qualification
(#2818), capture carriers (#2814/#3114) and compressed-core (#2763) remain outside
this change. No new data model, tool or provisioning behavior.

## Success

1. Explicit fadump on a valid x86_64 System is configuration_error with data.reason=capability_unsupported before any
   capture-related mutation, job or idempotency record, with no fadump in supported.
2. Implicit and explicit ppc64le fadump behavior remains unchanged; host architecture
   and accelerator do not remove it. Other capture methods and ordinary validation
   precedence remain unchanged. This is unit/DB proof, not native POWER capture.
3. systems.get emits sorted architecture-compatible data.supported_capture_methods
   when runtime/profile are known. x86 omits fadump; ppc retains it if provider supports
   it. A provider without fadump never gains it. Empty supported set stays empty.
4. Unknown provider or malformed profile omits only unavailable capability facts;
   existing metadata/other independently known fields remain readable. systems.list
   stays unchanged. Resource describe keeps the accepted provider-wide superset.
5. Agent-visible wrapper docs distinguish provider scope, System architecture and
   capture readiness. Real HTTP unsupported attempts and advertisement observations
   must match actual candidate identities; no pending carrier may be called qualified.

## Design

Add `capture_methods_for_arch(methods: frozenset[CaptureMethod], arch: str) ->
frozenset[CaptureMethod]` in existing domain/capture.py. Resolve arch_traits(arch);
return methods when fadump_capture, else methods minus FADUMP. Unknown architectures
retain the trait reader's categorized configuration error. This is one shared policy
for implicit admission, explicit fadump admission and System advertisement.

In _resolve_capture_method retain explicit token parsing, non-core and descriptor
checks first. For explicit FADUMP after those checks, resolve system_arch(system),
select the compatible set and refuse absence with the existing capability envelope;
catch CategorizedError using existing failure_from_error. Explicit non-fadump paths
remain unchanged. Implicit resolution already parses profile; use its arch to narrow
the accepted set and the missing-method accepted_values. No job/query side effect in
this pure resolution step. Keep later KDUMP-family kernel/image gates unchanged.

In systems.view.get_system reuse the existing runtime resolution. Compute the new
list under its own categorized-error handling after the snapshot/traffic booleans
are known, so a bad profile does not erase independent capabilities. Pass an optional
list to system_envelope; emit iff not None (an empty list is meaningful). Leave list
callers' default None and existing failure/history rendering unchanged.

Update wrappers in systems/registrar.py, vmcore/registrar.py and catalog/resources.py.
The provider superset remains static even on an x86 host; System metadata is the
narrower architecture view. No host/QEMU discovery or provider composition edits.

## Failure model

- Rejected: x86 fadump admitted, stale mismatched System advertisement, unknown arch
  defaulting to host, unsupported provider gaining a method, rejection writing durable
  capture/idempotency state, or profile failure making systems.get unreadable.
- Accepted: readiness can change after this architecture observation; firmware,
  kernel, guest image and current System state remain independent existing gates.
- Accepted: legacy corrupt profile causes omitted System capability and typed capture
  error; no automatic profile repair is part of this issue.
- Accepted: ppc TCG remains architecture-compatible without a new end-to-end support
  claim. Existing ADR-0349 native POWER evidence requirement remains.

## Validation

Domain tests cross both declared architectures with actual provider method sets and
empty/no-fadump sets; unknown arch fails. Database handler tests use the real local
runtime with x86 crashed System/Run to prove capability_unsupported and zero new jobs,
activation mutations and keyed rows; preserve existing ppc positive tests.
System read tests compare architecture metadata with resolution for the same System,
including provider absence, malformed profile, empty set and unchanged list behavior.
Resource tests continue expecting the static superset. The wrapper text is prose; the
docs-check gate keeps the generated tool reference in step with it. No imports from
collected test modules.

Run the #2814 x86 local fadump cells when its carrier is available: four unsupported
functional and four capability cells across two configurations and two exposures.
Current main has no node binding for these eight cells. If still unimplemented,
run the smallest separately identified HTTP component proof on an assigned disposable
stack and retain the missing-carrier obligation; never manufacture node bindings or
claim that component calls qualify the eight contract cells. Host scheduling and any
real crash require root's concrete fixture grant. No native POWER result is inferred.
