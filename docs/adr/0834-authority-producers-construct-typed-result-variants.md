# 0834 — Authority producers construct typed result variants

## Status

Accepted (2026-11-23)

## Context

At d067ba8b7, lifecycle handlers build dictionaries despite existing closed result
variants in jobs/external_boot_models.py. authority_result accepts dict[str, Any],
so misspelled fields survive type checking. Derived release builds an ordinary
carrier, dumps it to JSON, and reparses a completion; teardown repeats binding data.

## Decision

Expose the existing producer-used variants and evidence classes from their model
owner. Construct them at lifecycle call sites. Give authority_result a success-only
payload union and keep the existing closed carrier validation. Terminal, conflict
and cleanup evidence producers return their corresponding existing model types.

Share the existing binding projection as authority_carrier_data. Derived release
validates its completion directly from that mapping and its typed release result.
Teardown uses the same mapping with a typed teardown result, overriding both journal
fields from the actual teardown response before validating its completion.

Keep the distinct domain release evidence model: it computes the persisted canonical
identity and has different constraints from the job result evidence. Explicitly
validate that serialized domain evidence into the existing job evidence type.
Do not broaden job cleanup evidence to the domain model's pending cleanup states.

## Consequences

Required constructor fields become type-checked at their producers. Runtime carrier
ownership, purpose, size and subtype validation remains. Internal Python names and
JSON-schema definition labels change; serialized field names and values do not.
No compatibility aliases remain for the renamed private variants. Freshly constructed
instances are not mutated before wrapping; nested model fields are not revalidated
after arbitrary internal mutation. Serialized inputs retain their existing validation.

## Considered & rejected

- Keep raw dictionaries: [fact] source authority_result accepts dict[str, Any] and
  lifecycle emits literal dictionaries at its calls; required variant fields are
  invisible to the existing whole-tree type gate. This leaves the queued defect.
- Replace the job evidence types with domain types: [fact] domain cleanup allows
  pending_system_teardown and a nullable release_identity; the job model allows
  neither. Reuse would change accepted wire contracts.
- Add generic carrier factories or overload families: [judgment] one existing
  projection and existing closed validators suffice for the three carrier classes;
  additional construction machinery would obscure the exact dispatch subtype.
