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
  `config_ref` echoed by `buildconfig.set`/`list`/`get` for a `source='server'`
  build. It does **not** state that `provider` is decorative or that "any
  non-empty value works" — that framing is not forward-safe (decision 3); the
  decorative fact stays in the `catalog_config_ref` factory comment and this
  spec, off the agent surface.
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
  - a `BuildProfile.parse` of a `source='server'` document carrying the echoed
    `config_ref` succeeds, and a `source='external'` document carrying the same
    ref is rejected as a `CONFIGURATION_ERROR` whose error detail names the
    `config` field (the existing `extra="forbid"` → `configuration_error`
    mapping at the parse boundary) — pinning the lane boundary and confirming the
    cross-lane paste fails categorized, not as a raw crash. Enriching that
    message to name `source='server'` is out of scope (it would special-case
    pydantic extra-field errors for a priority:low change; the detail already
    names `config` and the Field text names the lane).
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
  **resolved row's `entry.name`** (not the requested string). All three sites
  derive the ref from the canonical row name so a future case-insensitive or
  normalized name column cannot make `get` echo a non-canonical name that
  resolves differently from `list`.

`buildconfig.get` is included beyond the two tools the issue names because it is
the inspect tool the `runs.create` `Field` text points agents to ("Call
buildconfig.get to inspect a named fragment"); echoing there closes the loop for
the same one-call cost and keeps the three sibling tools consistent.

### 3. `runs.create` Field points to the echoed ref — no decorative rule taught

Append to the `config` clause of the `build_profile` `Field` description
(`registrar.py:83-86`), inside the `source='server'` part of the paragraph that
is the only place `config` is accepted: for a **`source='server'`** build, obtain
the ref by pasting the `config_ref` echoed by `buildconfig.set`/`list`/`get`
(which fills in the required `provider` for you). Naming the lane explicitly stops
an agent on the recommended `source='external'` default from pasting the ref into
a profile with no `config` field. Keep the existing worked example. Do **not**
describe a `source`→`provider` mapping, and do **not** state in agent-facing text
that `provider` is decorative / "any value works" (see Forward-safety).

**Refinement of the issue's proposed fix #2.** The issue proposed documenting in
the Field text that `provider` is "not consulted — any non-empty value works". We
deliberately do **not** teach that rule on the agent surface: the echoed
canonical ref already removes the guesswork the issue targets, and teaching "any
value works" is the one framing that is *not* forward-safe. The decorative fact
stays where only maintainers read it — the `catalog_config_ref` factory comment
and this spec — not in the tool schema an agent acts on.

**Forward-safety.** Echoing a canonical `config_ref` that agents copy verbatim is
forward-safe: if #1033 (or later multi-tenant/namespaced-catalog work) makes
`provider` meaningful, the echoed value changes at one source
(`catalog_config_ref`) and every agent that copied it stays correct. An agent
that had internalized "any value works" would instead construct wrong refs.
#1033 owns re-evaluating this contract; see Out of scope.

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
