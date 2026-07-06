# `buildconfig` echoes the config ComponentRef; `provider` documented as decorative (#1032)

- **Issue:** #1032 (`BLACK_BOX_REVIEW.md` pain point P1; epic #998; related #1033)
- **ADR:** none — see "No ADR" below.
- **Status:** Draft

## Problem

An agent that publishes or lists an operator build-config fragment
(`buildconfig.set` / `buildconfig.list`) and then wants to reference it from
`runs.create` (`build_profile.config`) has to hand-construct a
`CatalogComponentRef` `{kind:"catalog", provider:???, name}`. Nothing on the
agent-facing surface tells it what `provider` to use:

- `buildconfig.set` returns `{name, sha256, bytes, source}` and
  `buildconfig.list` returns `{name, sha256, source, description}`
  (`src/kdive/mcp/tools/catalog/build_configs.py:189-213`) — neither echoes a
  usable `ComponentRef`.
- The only worked `provider:"system"` example is shown for the seeded `kdump`
  fragment on `runs.create` and marks `provider` **required**
  (`docs/guide/reference/runs.md`, generated from the `config` `Field` at
  `src/kdive/mcp/tools/lifecycle/runs/registrar.py:83-86`), implying `provider`
  is a meaningful namespace.

**Verified premise correction.** `provider` is **not consulted** for a
build-config catalog ref. The `build_config_catalog` table is keyed by name
alone (`src/kdive/build_configs/catalog.py`), and resolution reads only
`ref.name` (`src/kdive/providers/shared/build_host/configuration/config.py:100-101`,
`catalog_fetch(ref.name)`); `validate_config_ref`
(`config.py:105-115`) does not inspect `provider` either. The ref model only
requires a non-empty string (`src/kdive/components/references.py:97-100`). This
is already stated internally at `src/kdive/build_configs/defaults.py:18-20`:
*"provider is decorative for build configs (the catalog is keyed by name alone)
but the ref model requires it; system matches the seed tenant."* So
`provider:"system"` works because `provider` is ignored — any non-empty value
resolves the same fragment by name. It was never a coin-flip that could fail.

There is **no `source`→`provider` mapping** (`source` is orthogonal
provenance: `operator`/`config`/`seed`). Documenting one would perpetuate the
reviewer's misconception; the fix must not introduce it.

## Goal / acceptance

Close the discoverability gap so an agent never guesses `provider`, without
inventing a namespace that does not exist.

Acceptance:

- `buildconfig.set` success payload includes
  `data.config_ref = {kind:"catalog", provider:"system", name:<name>}` — a ref
  that pastes into `runs.create` `build_profile.config` **for a `source='server'`
  build**. `config` is a `ServerBuildProfile`-only field
  (`src/kdive/profiles/build.py:118`); `ExternalBuildProfile` has no `config`
  field and is `extra="forbid"`, so the acceptance and the `Field` text must say
  the echoed ref applies to server builds, not the `source='external'` default.
- Each `buildconfig.list` item and the `buildconfig.get` payload include the
  same `data.config_ref`.
- The echoed `config_ref` is produced from the same `provider="system"`
  convention that `DEFAULT_CONFIG_REF` uses; the invariant is **enforced by a
  test**, not asserted by prose (see the test criteria below), and the value
  round-trips through `parse_component_ref` as a valid `CatalogComponentRef`.
- The `runs.create` `config` `Field` description directs the agent to paste the
  `config_ref` echoed by `buildconfig.set`/`list`/`get`, and notes that a
  catalog fragment is referenced by `name` (the ref's `provider` is currently
  decorative for build configs). It does **not** teach "any non-empty value
  works" as a durable rule — see decision 3 for why the canonical-ref framing is
  forward-safe and the "any value works" framing is not.
- Tests pin the invariants so a regression fails rather than ships green:
  - a **literal** assertion that `catalog_config_ref("x").model_dump()` equals
    `{"kind":"catalog","provider":"system","name":"x"}` — a drift of the factory
    `provider` (e.g. to `"seed"`) must fail (a factory-vs-factory deep-equal is
    tautological and would not, since every echo site derives from the same
    factory and the ref only requires a non-empty `provider`);
  - `DEFAULT_CONFIG_REF.provider == catalog_config_ref("kdump").provider`, so the
    seed default and the echoed convention share one value;
  - for `set`/`list`/`get`, the echoed `data.config_ref` equals
    `catalog_config_ref(<name>).model_dump()`;
  - a `ServerBuildProfile.parse` of a profile carrying the echoed `config_ref`
    succeeds and an `ExternalBuildProfile.parse` of the same ref is rejected —
    pinning the lane boundary the Field text documents.
- The `buildconfig.set`/`list`/`get` wrapper docstrings mention that the
  response carries a ready-to-use `data.config_ref` (the agent-facing contract;
  the generated `docs/guide/reference/buildconfig.md` renders the docstring, not
  the response shape).
- No `source`→`provider` mapping is documented anywhere.
- `just docs` regenerates `docs/guide/reference/{buildconfig,runs}.md` with no
  drift; `just ci` green.

## Design decisions

### 1. Single source of truth for the `system` convention

Refactor `src/kdive/build_configs/defaults.py` so the `provider="system"`
convention lives in exactly one place:

- Add `catalog_config_ref(name: str) -> CatalogComponentRef` returning
  `CatalogComponentRef(kind="catalog", provider="system", name=name)`.
- Redefine `DEFAULT_CONFIG_REF = catalog_config_ref("kdump")` in terms of it, so
  the seeded default and every echoed ref share one provider constant. The
  existing decorative-`provider` comment moves onto the factory.

Rationale: the echoed ref must match the convention the docs teach; a second
hardcoded `"system"` literal in the tool module could silently drift.

### 2. Echo `data.config_ref` from the three read/write tools

In `src/kdive/mcp/tools/catalog/build_configs.py`, build the ref via
`catalog_config_ref(name).model_dump()` (a `{kind, provider, name}` dict that is
by construction a valid `CatalogComponentRef`) and add it under `config_ref` to:

- `set_build_config` success payload (`:189-199`);
- `_entry_envelope` list-item payload (`:202-213`), keyed on `entry.name`;
- `read_build_config` (`buildconfig.get`) payload (`:118-127`), keyed on the
  requested `name`.

`buildconfig.get` is included beyond the two tools the issue names because it is
the inspect tool the `runs.create` `Field` text points agents to ("Call
buildconfig.get to inspect a named fragment"); echoing there closes the loop for
the same one-call cost and keeps the three sibling tools consistent.

### 3. Document `provider` on `runs.create` — canonical-ref framing, not a rule

Append to the `config` clause of the `build_profile` `Field` description
(`registrar.py:83-86`): for a **`source='server'`** build, a catalog fragment is
referenced by `name`, and the canonical way to obtain the ref is to paste the
`config_ref` echoed by `buildconfig.set`/`list`/`get`; the ref's `provider` is
currently decorative for build configs (`system` by convention). Because the
`config` clause already lives inside the `source='server'` part of the paragraph
that is the only place `config` is accepted, the added text names the lane
explicitly so an agent on the recommended `source='external'` default does not
paste the ref into a profile that has no `config` field. Keep the existing worked
example. Do **not** describe a `source`→`provider` mapping.

**Forward-safety.** The Field text points agents at the *echoed canonical ref*
rather than teaching "any non-empty value works". Echoing a canonical
`config_ref` that agents copy verbatim is forward-safe: if #1033 (or later
multi-tenant/namespaced-catalog work) makes `provider` meaningful, the echoed
value changes at one source (`catalog_config_ref`) and every agent that copied
it stays correct. Teaching the decorative rule as durable guidance is **not**
forward-safe — an agent that internalized "any value works" would then construct
wrong refs. #1033 owns re-evaluating this contract; see Out of scope.

### 4. Wrapper docstrings mention `config_ref`

Update the `buildconfig.set`/`list`/`get` wrapper docstrings
(`registrar`-style `@app.tool` docstrings at `:315`, `:352`, `:384`) to note the
response carries `data.config_ref` — the only agent-visible channel for a
response field, since the generated reference documents parameters only.

## No ADR

No architecture decision is made or superseded: `provider` being decorative for
build-config catalog refs is an already-accepted, already-documented property
(`defaults.py:18-20`); this change surfaces it and adds an additive response
field. There is no layer boundary, ownership split, concurrency invariant,
failure contract, migration, or rollback decision to record. Per the repo ADR
convention (ADRs capture decisions with viable alternatives), none is warranted.

## Compatibility & rollback

Purely additive: a new `data.config_ref` key on three response payloads and
docstring/`Field` text. No schema, migration, auth, or persistence change.
Existing clients that ignore unknown response keys are unaffected. Rollback is a
plain revert; no state to unwind.

## Out of scope

- Documenting a `source`→`provider` mapping (none exists; explicitly excluded).
- Changing how `provider` is validated or resolved (it stays a required
  non-empty string in the ref model; only its documentation and the echoed value
  change).
- The related #1033. **Coordination dependency:** if #1033 makes `provider`
  meaningful for build-config catalog refs, it must revise the
  `catalog_config_ref` provider value (one source) and the `runs.create` `config`
  Field text this spec adds. The canonical-ref framing (decision 3) limits the
  blast radius to those two sites; agents that copied the echoed ref need no
  re-education.
