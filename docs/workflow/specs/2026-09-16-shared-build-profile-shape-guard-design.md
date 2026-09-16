# Shared build-profile shape and its cross-suite guard — design (#2511)

## Problem

Three live-stack suites each define a private `_build_profile()` returning the identical
`{"schema_version": 1, "arch": "x86_64"}`, and `tests/integration/test_live_stack.py` restates
it inline for `ppc64le` three more times. A `BuildProfile` change (`src/kdive/profiles/build.py`)
has to land in all six copies, and the guard #2483 wanted has no one definition to assert against.

## Scope

`tests/integration/live_stack/spine.py` gains one provider-agnostic
`build_profile(arch: str = "x86_64") -> dict[str, object]`, and its docstring draws the line it
left implicit: build profiles are shared here, provider-specific *provision* factories stay
per-suite
([ADR-0665](../../adr/0665-the-shared-build-profile-shape-lives-in-the-spine-module.md)). The
three suites drop their `_build_profile()` and the three inline `ppc64le` dicts and import it.
New non-gated `tests/integration/live_stack/test_build.py` holds the guard, so it runs in CI
where the suites skip; its stem is what makes `scripts/select_changed_tests.py` select it when
`src/kdive/profiles/build.py` changes.

`_provision_profile` dedup stays out of scope, and #2551's try/finally allocation-release
discipline in `test_remote_live_stack.py` is untouched — only the `build_profile=` argument moves.

### Failure model

- **Actors and deployments** — CI running the non-gated guard; an operator running a
  `live_stack`/`live_vm` tier on a provisioned host, where the suites send the document.
- **Invariants at stake** — the document the suites put on the wire is one `BuildProfile`
  accepts; a `BuildProfile` field change cannot pass while the suites still send the old shape.
- **Accepted failure classes** — two restatements elsewhere in `tests/integration/` stay as they
  are, outside this surface and carried as follow-ups: `test_finalization_measurement.py:185`, and
  `_seed.py:63`, whose divergent document seeds the DB out of band and never meets the validator.
  A `BuildProfile` field arriving with a default stays green, the old document still being one the
  model accepts. An unsent `SUPPORTED_ARCHES` arch is proved parseable, not bootable.
- **Covered elsewhere** — `BuildProfile` validation rules: `tests/profiles/`.

## Success

1. One shared definition replaces all six restatements named in Problem; no suite keeps its own.
2. The guard fails when the shared document stops being one `BuildProfile` accepts — an extra
   key, a required field the model gained, or an arch it rejects.
3. Every arch in `SUPPORTED_ARCHES` is constructible through the factory and parses.
4. Behaviour is unchanged: each former call site sends the document it sent before.

## Validation

`focused-test` entries in that new module, green via `just test-changed`:

- Success 2 — `test_the_shared_build_profile_parses_through_the_real_validator`, and
  `test_the_shared_build_profile_carries_only_known_required_fields`: its keys are `BuildProfile`
  field names covering every required one. Not key-set equality, which would red on a defaulted
  field and direct the wrong fix.
- Success 3 — `test_every_supported_arch_is_constructible`, parametrised over `SUPPORTED_ARCHES`;
  red on an arch the factory renders unparseable.
- Success 4 — `test_the_shared_build_profile_is_the_document_the_suites_sent` pins the literal
  those sites sent — the independent oracle deriving it from `BuildProfile` would destroy.
- Success 1 — `task-test-not-applicable`: no runtime observation separates one shared definition
  from six identical ones, so it is read in review against Problem's six sites. The guard's bite
  is proved before hand-off by a controlled fault: a shape it must reject, red, green on revert.
