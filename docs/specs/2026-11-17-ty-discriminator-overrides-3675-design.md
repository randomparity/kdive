# Design — pydantic discriminators that narrow without a ty ignore (#3675)

## Problem

ty 0.0.99 reports `invalid-attribute-override` when a pydantic subclass re-declares an inherited
field with a different `Literal`. #3674 silenced nine sites with per-line ignores. Eight narrow a
discriminator (seven `schema_`, one `protocol`) that a concrete parent pinned to a sibling
`Literal`; the ninth, `SlotState.schema`, shadows the deprecated `BaseModel.schema` method.

## Scope

- A `str` field in a base may be narrowed to a `Literal`; a `Literal` may not become another
  `Literal`. Where a concrete model is also the parent of a narrowing subclass, its shared
  fields, validators and methods move to a new private base declaring the discriminator as `str`
  (same alias, no default). The former parent and each subclass declare their own `Literal` and
  default. Pydantic keeps a re-declared field at its base position, so order, aliases and defaults
  are unchanged. The new bases are never instantiated.
- Families (parent -> narrowing subclass): `AuthoritySystemMarkerV1` ->
  `_AuthoritySystemAttemptBinding` -> `AuthoritySystemJournalRecordV1`, `_AuthoritySystemProofBinding`;
  `_AuthorityBinding` -> `AuthorityTeardownMutationRequestV1`; `LocalSystemTeardownAnchorV1` ->
  `LocalSystemTeardownIntentV1`; `RemoteSystemTeardownAnchorV1` -> `RemoteSystemTeardownIntentV1`;
  `RemoteModuleVolumePreparationResponseV1` -> `RemoteModuleTerminalPreparationResponseV1`;
  `RemoteModuleRecoveryRefV1` -> `RemoteModuleRecoveryRefV2`.
- `SlotState.schema` becomes `schema_` with `alias="schema"`, and the config gains
  `serialize_by_alias=True`, so `state.json` and `model_dump()` keep the key `schema`.
- Out of scope (operator-approved): any change to a wire format, alias or what validation accepts;
  `tests/guards`.

## Success

`rg 'ty: ignore\[invalid-attribute-override\]' src deploy` is empty, `just type` and `just lint`
are green, and no concrete model's schema, serialized bytes or accept/reject behaviour changes.

## Validation

- focused-test: `tests/providers/test_discriminator_schemas.py` pins a hash of
  `model_json_schema(by_alias=True)` for every model in the six modules, taken before the change.
  Controlled fault: dropping one default or alias turns it red.
- focused-test: `tests/processes/lifecycle/systemd/test_systemd_worker_state.py` pins the
  `model_dump_json()` prefix and `model_dump()` key of `SlotState`.
- Existing family tests stay green; a lost discriminator default would fail them and the hashes.
- Failure model: a narrowed model that lost its default or alias would reject or accept a
  document it did not before; the schema hash and `SlotState` byte test observe exactly that.
