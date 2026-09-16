# 0665 — The shared build-profile shape lives in the spine module

## Status

Accepted (2026-09-16)

## Context

Three live-stack suites — `tests/integration/test_live_stack.py`,
`tests/integration/test_console_parts_live.py` and
`tests/integration/test_remote_live_stack.py` — each defined an identical private
`_build_profile()` returning `{"schema_version": 1, "arch": "x86_64"}`, and the local suite
restated the same document inline three more times for `ppc64le`. Six copies of one wire
document across those three files, so a `BuildProfile` field change
(`src/kdive/profiles/build.py`) lands in six places or in none, and the shape guard #2511 asks
for has no single place to assert against. Two further restatements sit elsewhere under
`tests/integration/` — `test_finalization_measurement.py:185` and a divergent `BUILD_PROFILE`
in `_seed.py:63` that is seeded straight into the database and never meets the validator. Both
are outside #2511's surface and are carried as untracked follow-up candidates, not resolved
here.

`tests/integration/live_stack/spine.py` is already the shared, provider-agnostic scaffolding
every one of those three suites imports (`mint_role_token`, `drain_job`, `phase`,
`LOCAL_ALLOCATION_DISK_GB`). Its docstring reserves the per-suite modules for
*provider-specific* pieces. A build profile names only `schema_version` and the target `arch`
since the server-build lane was removed (ADR-0048); it carries nothing provider-specific.

## Decision

We will put the one shared `build_profile()` factory in `tests/integration/live_stack/spine.py`
rather than create a new support module, and guard its shape against the real `BuildProfile`
validator from `tests/integration/live_stack/test_build.py`.

## Consequences

The suites keep one import list instead of two, and the `spine.py` docstring now has to draw
the line it did not: provider-agnostic *build* profiles are shared, provider-specific
*provision* profiles stay per-suite. The distinction is real but it is one more thing a reader
of that module must hold. `spine.py` grows, and it is already 877 lines.

The guard's filename is load-bearing rather than descriptive: `scripts/select_changed_tests.py`
maps a changed source file onto `tests/**/test_<stem>.py`, so only the stem `build` makes a
`src/kdive/profiles/build.py` change select this guard. A more descriptive
`test_build_profile.py` would silently never run on the drift it exists to catch.

`_provision_profile` stays duplicated across the suites. That is deliberate and out of scope
here — those bodies genuinely differ per provider and per test intent. No tracker owns that
dedup yet, and this record does not claim one does.

## Considered & rejected

- **A new `tests/integration/live_stack/build_profiles.py` support module**, which is what
  #2511's "extract a shared support module" literally asks for. judgment: a module holding one
  five-line factory, imported beside `spine.py` by every suite that already imports
  `spine.py`, buys separation no reader needed and costs a second import block in four files.
- **Derive the document from the validator** — `dump_build_profile(BuildProfile.parse({...}))`
  — so the factory cannot disagree with the model by construction. verified: `parse` at
  `src/kdive/profiles/build.py:69` and `dump_build_profile` at `:100` both exist, and `spine.py`
  already imports from `kdive.*`, so the pairing is available. Rejected because it makes the
  guard tautological: the suites' document would be whatever the model says, so a model change
  could never be observed as drift. The literal is the independent oracle worth keeping.
- **A module-level constant instead of a factory.** verified: `rg -n build_profile
  tests/integration/test_live_stack.py` shows `arch: "ppc64le"` sent at `:1152`, `:1386` and
  `:1580` alongside `x86_64`, so a single frozen document cannot serve the call sites.
- **Fold the guard into the existing `tests/integration/live_stack/test_spine.py`.** verified:
  `select_targets` in `scripts/select_changed_tests.py:94` looks up `test_index[Path(path).stem]`,
  and no `src/**/spine.py` exists, so a `build.py` change would never select it.
- **Leave the duplication and add a per-suite guard.** judgment: this is what #2483 declined —
  three copies of the shape knowledge means each guard asserts its copy, not the contract.
- **Do nothing.** verified: the copies have already drifted in prose while their return values
  stayed equal — `test_live_stack.py:202` and `test_console_parts_live.py:138` carry the same
  docstring, `test_remote_live_stack.py:115` a reworded one. The next `BuildProfile` field is
  when that drift becomes a silent wire defect instead of a cosmetic one.
