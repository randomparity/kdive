# Provider Capability Parity Design

## Problem

`ProviderRuntime` represents snapshot and traffic-capture support twice: as booleans in
`ProviderSupport` and as optional runtime ports. Nothing currently prevents a runtime from
advertising an operation without wiring its port, or wiring a port without advertising it.
Admission, API projection, and worker execution can therefore observe conflicting capability
states.

## Decision

Keep the existing `ProviderSupport.supports_snapshots` and
`ProviderSupport.supports_traffic_capture` fields because they are part of the provider descriptor
consumed by admission and API projection. Add construction-time validation to `ProviderRuntime`
that requires each boolean to equal the presence of its corresponding port:

- `supports_snapshots` must equal `snapshot is not None`.
- `supports_traffic_capture` must equal `traffic_capturer is not None`.

An inconsistent runtime raises `ValueError` immediately with a message naming both fields. This
makes assembly errors fail at their source while preserving the existing public capability shape.
No factory or alternate construction path is added.

## Testing

Unit tests construct runtimes for both valid states of each capability and verify that each
mismatch fails. Existing local-libvirt, remote-libvirt, fault-inject, and test runtime construction
then exercises the invariant through the normal test suite.

## Scope

This change covers only snapshot and traffic-capture capabilities because those are the duplicated
boolean/optional-port pairs identified by the review. Other support fields describe sets or
capabilities whose runtime representation is not a single optional port and are unchanged.
