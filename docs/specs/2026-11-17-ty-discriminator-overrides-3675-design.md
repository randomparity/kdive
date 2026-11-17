# Design — pydantic discriminators that narrow without a ty ignore (#3675)

## Problem

ty 0.0.99 reports `invalid-attribute-override` when a pydantic subclass re-declares an inherited
field with a different `Literal`. #3674 silenced the nine sites with per-line ignores, and each new
model in these families must repeat one. Seven sites narrow a schema discriminator (`schema_`, one
`protocol`) that a concrete parent already pinned to a sibling `Literal`; the eighth,
`SlotState.schema`, shadows the deprecated `BaseModel.schema` method.

## Decision

- **Widen the shared declaration, narrow in each concrete model.** A field typed `str` in a base
  may be narrowed to a `Literal` by a subclass without a diagnostic; a `Literal` parent cannot be
  narrowed to a different `Literal`. Where a concrete model is also the parent of a narrowing
  subclass, the shared fields, validators and methods move to a new private base that declares
  the discriminator as `str` with the same alias and no default. The former parent and every
  former subclass then declare their own `Literal` and default. Pydantic keeps a re-declared field
  at its base position, so field order, aliases, defaults and validation of every concrete model
  are unchanged. The new bases are never instantiated.
- **Rename `SlotState.schema` to `schema_` with `alias="schema"`.** The model sets
  `serialize_by_alias=True`, so `state.json` bytes and `model_dump()` keys stay `schema`;
  constructors keep passing `schema=1` through the alias. The `immutable` tuple names the
  attribute.
- Rejected: restating the parent as a `Literal` union (changes what the parent validates);
  `Final`/`ClassVar` (not valid pydantic fields); keeping the ignore (the issue's problem).
- Changed `isinstance` relations (a former subclass is no longer a subclass of its former concrete
  parent) are accepted only where `just type` and the tests show nothing relies on them.

## Out of scope

Any change to a wire format, an alias or what validation accepts (operator-approved exclusion).
`tests/guards` (sibling quest #3676).

## Failure model

If a narrowed model silently lost its default or alias, validation would reject or accept a
document it did not before. Each restructured family therefore keeps a test pair: the concrete
model's `model_dump(by_alias=True)` equals its pre-change bytes, and a document carrying a sibling
model's schema string is rejected. The `SlotState` pair also pins `model_dump_json()`.

## Verification

`rg 'ty: ignore\[invalid-attribute-override\]' src deploy` is empty; `just type`, `just lint` and the
existing wire tests of each family pass; the new byte-equality tests pass on the pre-change commit
and the branch.
