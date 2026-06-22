# Plan — local-libvirt offline drgn introspection (`introspect.from_vmcore`, B2 / #676)

Derived from the hardened spec
[`docs/specs/2026-06-22-local-offline-drgn-introspection.md`](../../specs/2026-06-22-local-offline-drgn-introspection.md)
and [ADR-0210](../../adr/0210-local-libvirt-live-debug-introspection.md) §2.

## Context for every task

- **Repo conventions:** `CLAUDE.md` / `AGENTS.md`. Python 3.14, `uv`. The `justfile` is the single
  source of truth for commands. Absolute imports only. ≤100 lines/function, 100-char lines.
  Conventional-commit subjects ≤72 chars, ending with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- **TDD is mandatory** (`superpowers:test-driven-development`): failing test first, confirm it fails
  for the right reason, minimal implementation, rerun focused test + guardrails, refactor green.
- **Guardrail commands** (run the relevant ones before every commit; full set before push):
  `just lint`, `just type`, `just test`, and the doc gate `just docs-check` (regenerates and diffs
  the committed tool reference). `just ci` runs the whole PR gate.
- **Known trap (override #7):** local `ty` can diverge from CI because the live `drgn` package is
  installed in this venv (it resolves imports CI cannot). If a `ty` error is *purely* a live-dep
  import-resolution artifact in code you did not change, note it and rely on CI's type job; fix
  everything else to zero warnings.
- **`live_vm` tests stay gated.** Do not un-gate them; do not widen what a gate admits.
- **Scope fence:** touch only `LocalLibvirtVmcoreIntrospect` (the offline class), its `from_env`,
  the single `supported_introspection=` kwarg in `composition.py`, the `introspect.from_vmcore`
  tool's maturity `providers` string, the honesty test, and the offline-port / composition /
  admission / describe tests. **Do not touch** `LocalLibvirtLiveIntrospect` (B3/#677 owns it) or
  the `introspect.run` tool's maturity. The shared `drgn_program` seams and `assemble_report` are
  unchanged.

## Task 1 — Wire the real drgn seams in `from_env()` and drop the dead placeholder

**Where it fits:** spec §1. Turns the offline port from off-gate-stub to wired (the core of B2).

**Files:**
- `src/kdive/providers/local_libvirt/debug/introspect.py` (impl)
- `tests/providers/local_libvirt/test_introspect_drgn.py` (tests)

**Implementation:**
1. In `from_env()` (currently `introspect.py:67-79`), construct the port with the three real shared
   seams, mirroring `RemoteLibvirtVmcoreIntrospect.from_env()`:
   - import from `kdive.providers.shared.debug_common.drgn_program`:
     `open_vmcore_program`, `read_vmcoreinfo_build_id`, `run_introspection_helper`;
   - pass `read_vmcore_build_id=read_vmcoreinfo_build_id`, `open_program=open_vmcore_program`,
     `run_helper=run_introspection_helper` (keep `fetch_object=_real_fetch_object`).
2. **Remove** the now-dead `_real_read_vmcore_build_id` function (`introspect.py:252-256`) and its
   import usage — it is replaced, not deprecated (CLAUDE.md "replace, don't deprecate"). Confirm no
   other reference survives (`rg _real_read_vmcore_build_id`). Leave `_real_fetch_object` and
   `_normalize_attach_error` (the latter is still used by the untouched Live class at line 215).
3. Update the module/class docstrings that say the offline seams "stay disabled until the live
   runner injects" so they describe the wired-but-import-lazy reality (mirror remote's wording);
   keep the change minimal and factual (no banned doc-style words).

**TDD — write these tests first, watch them fail, then implement:**
- `test_from_env_wires_real_drgn_seams`: `LocalLibvirtVmcoreIntrospect.from_env(secret_registry=…)`
  returns a port whose `_open_program is open_vmcore_program`, `_run_helper is
  run_introspection_helper`, and `_read_vmcore_build_id is read_vmcoreinfo_build_id` — asserted by
  identity, importing those names from `drgn_program` (no `drgn` import). This fails today (seams
  are `None`).
- **Replace** the existing `test_from_env_real_seams_raise_missing_dependency`
  (`test_introspect_drgn.py:385-389`), whose premise (the up-front `None`-guard) no longer holds.
  New `test_from_env_reaches_drgn_import_missing_dependency`: build a vmcore blob with a valid
  VMCOREINFO `BUILD-ID=<hex40>` line; call `from_env(...).from_vmcore(vmcore_ref=…,
  debuginfo_ref=…, expected_build_id=<that hex40>)` with the real seams but **monkeypatch the
  module's object-store fetch** so both `vmcore_ref` and `debuginfo_ref` resolve (the vmcore blob
  for the core ref, any bytes for the vmlinux ref). On the CI host `drgn` is absent, so the call
  must raise `MISSING_DEPENDENCY` from `open_vmcore_program` → `_require_drgn`. This proves control
  reached the import (past provenance + both fetches), not the removed `None`-guard.
  - *Fetch interception:* `from_env` wires `fetch_object=_real_fetch_object`, which calls
    `object_store_from_env()`. Rather than stand up a store, build the port directly with
    `LocalLibvirtVmcoreIntrospect(fetch_object=<fake serving both refs>,
    read_vmcore_build_id=read_vmcoreinfo_build_id, secret_registry=…,
    open_program=open_vmcore_program, run_helper=run_introspection_helper)` — i.e. assert the *real
    seams* drive the import-reaching path without mocking the store. (The identity test above
    already proves `from_env` selects exactly these seams, so the two tests together cover
    "`from_env` wires the real seams" **and** "the real seams reach the import".) If `drgn` is
    importable in this dev venv the test would instead proceed into drgn; gate this single test with
    the existing pattern used for "drgn-absent" expectations — `pytest.importorskip` is the wrong
    direction here, so instead assert: `with pytest.raises(CategorizedError) as exc: …` and accept
    either `MISSING_DEPENDENCY` (drgn absent, CI) **or** `DEBUG_ATTACH_FAILURE` (drgn present but
    the synthetic blob is not a real core) — both prove the import was reached and the old
    `None`-guard is gone. Document this dual-accept in a comment citing the live-dep divergence.
- Keep every other existing offline-orchestration test (provenance mismatch, byte-cap, redaction,
  both-seams-required, open-failure) unchanged — behavior is unchanged.

**Acceptance check:** `from_env` exposes the real seams (identity); the wired path reaches the drgn
import on a host without (or with) drgn; the full offline suite stays green.

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/providers/local_libvirt/test_introspect_drgn.py -q`.

**Rollback:** revert the `from_env` body and restore the deleted placeholder + its test.

## Task 2 — Flip `supported_introspection` to `offline-vmcore` in composition

**Where it fits:** spec §2. Makes admission admit the now-wired plane and makes `describe` report it.

**Files:**
- `src/kdive/providers/local_libvirt/composition.py` (impl)
- `tests/providers/local_libvirt/test_composition.py` (test)

**Implementation:**
1. In `build_runtime` (`composition.py:102-127`), add
   `supported_introspection=frozenset({"offline-vmcore"})` to the `ProviderRuntime(...)` call.
2. Update the adjacent comment (`composition.py:114-117`) that says "The debug-transport and
   introspection sets start empty … Epic B's B1/B2/B3 populate them" so it records that B2 has now
   populated introspection with `offline-vmcore`, while debug-transport (B1) and the `live` mode
   (B3) remain empty. Keep it factual and minimal.

**TDD — edit the existing assertion first (it will fail), then implement:**
- In `test_composition.py:74`, change `assert runtime.supported_introspection == frozenset()` to
  `assert runtime.supported_introspection == frozenset({"offline-vmcore"})`. Keep the adjacent
  `assert runtime.supported_debug_transports == frozenset()` unchanged (proves no over-reach into
  B1/B3). Run it red, then implement task 2's impl to make it green.

**Acceptance check:** `build_runtime(...).supported_introspection == frozenset({"offline-vmcore"})`
and `supported_debug_transports == frozenset()` (no over-reach).

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/providers/local_libvirt/test_composition.py -q`.

**Rollback:** drop the kwarg and restore the `frozenset()` assertion.

## Task 3 — Admission admit-path + `describe` projection tests for local

**Where it fits:** spec acceptance criteria — turn "admission admits offline introspection on
local" and "describe reports offline-vmcore" from claims into tests.

**Files:**
- `tests/mcp/debug/test_introspect_tools.py` (admission admit-path)
- `tests/mcp/catalog/test_resources_tools.py` (describe projection — update the existing local test)
- `tests/mcp/systems_support.py` (**only if** admit-path harness option (b) is chosen; additive
  optional kwarg)

**Implementation (tests only — no src change):**
1. **Admit path:** the deny path already exists
   (`test_from_vmcore_unsupported_plane_is_capability_unsupported`, using
   `provider_resolver(supported_introspection=frozenset())` driven through `_call_registered_tool`).
   The admit path must pass the `_require_introspection` gate **and** then run a working introspector
   behind it — but the shared `provider_resolver` helper hardwires `vmcore_introspector=unused_port`
   (`tests/mcp/systems_support.py:142`) and exposes **no** parameter for it. So
   `provider_resolver(supported_introspection=frozenset({"offline-vmcore"}))` alone would pass the
   gate and then call `unused_port.from_vmcore(...)` → `AttributeError`, not a success. Two viable
   harnesses; **prefer (a)** to keep the scope fence tight:
   - **(a) Bespoke resolver (no shared-helper edit):** in the new test, build a `ProviderRuntime`
     (or a `SimpleNamespace` cast to `ProviderRuntime`, matching the existing
     `test_register_adds_the_tool` pattern at lines 360-368) carrying
     `supported_introspection=frozenset({"offline-vmcore"})`,
     `vmcore_introspector=_FakeIntrospector()`, `component_sources` with `provider="local-libvirt"`,
     and the minimum other fields `with_runtime_for_run` reads; wrap it in a `ProviderResolver`
     keyed by `ResourceKind.LOCAL_LIBVIRT`. Drive it through `_call_registered_tool` so the full
     registered tool → `_gated` → `_require_introspection` → `_FakeIntrospector.from_vmcore` path
     runs. (Inspect what `with_runtime_for_run` resolves a runtime from — if it resolves by Run/
     System the test must seed a built Run with a core, reusing `_built_run_with_core` like the deny
     test.)
   - **(b) Additive `provider_resolver` param (alternative):** add an optional
     `vmcore_introspector: object | None = None` kwarg to `provider_resolver`, defaulting to
     `unused_port` (purely additive — no existing caller passes it, so none change). Then the admit
     test reads `provider_resolver(supported_introspection=frozenset({"offline-vmcore"}),
     vmcore_introspector=_FakeIntrospector())`. Only choose this if (a) proves to need too much
     hand-wired runtime; it widens the touched-file set to a shared helper.
   - **Assertions (either harness):** response is a success envelope (`status == "succeeded"`,
     `data["report"]` present) and the `_FakeIntrospector` recorded the call (proving the gate did
     **not** short-circuit). This pins ADR-0209 admission for the wired descriptor. If `_built_run_with_core`
     or `_FakeIntrospector` are module-private, reuse them in-module (the new test lives in the same
     `test_introspect_tools.py`).
2. **Describe projection:** `tests/mcp/catalog/test_resources_tools.py` already has
   `test_describe_projects_local_partial_capability` (≈line 235), which injects
   `introspection=frozenset()` and asserts `"introspect" not in capabilities` and
   `supported_introspection == []`. That scenario encodes the **pre-B2** local reality and is now
   stale: `_capability_planes` (resources.py) derives the `introspect` plane from a non-empty
   `supported_introspection`, so a local System advertising `offline-vmcore` *does* report
   `introspect`. **Update this test** to inject `introspection=frozenset({"offline-vmcore"})`, and
   change its assertions to: `capabilities == {"build", "boot", "kdump", "introspect"}`,
   `"introspect" in capabilities`, and `resp.data["supported_introspection"] == ["offline-vmcore"]`.
   Update its comment (line 236) to state local now reports introspect (offline-vmcore) and still
   NOT debug/host-dump. This converts the existing test into the describe-projection acceptance
   test; do not add a parallel duplicate. (The injected descriptor is independent of `build_runtime`,
   so task 2's composition change does not itself flip this test — updating it is a deliberate,
   required honesty edit so the describe contract matches the wired reality.)

**Acceptance check:** an admitted local `introspect.from_vmcore` returns success through the fake
port; `describe` lists `offline-vmcore` for a local System.

**Guardrails:** `just lint && just type && uv run python -m pytest
tests/mcp/debug/test_introspect_tools.py tests/mcp/catalog/test_resources_tools.py -q` (adjust the
second path to the module actually edited).

**Rollback:** delete the two added tests.

## Task 4 — Update maturity `providers` pointer + honesty guard + regenerate docs

**Where it fits:** spec §3. Keeps the tool `partial` but tells the honest post-wiring story, and
keeps the drift-guard strictly as strong.

**Files:**
- `src/kdive/mcp/tools/debug/introspect.py` (the `introspect.from_vmcore` maturity meta only)
- `tests/mcp/core/test_tool_docs.py` (honesty guard set + new positive assertion)
- `docs/guide/reference/introspect.md` (regenerated, not hand-edited)

**Implementation:**
1. In `introspect.py`, the `introspect.from_vmcore` tool's `maturity_meta(...)` `providers` string
   (currently `introspect.py:240`): change **only** the `introspect.from_vmcore` pointer to the
   exact spec wording:
   `"local-libvirt: wired, pending live KVM proof (M2.8 B6 #680); remote-libvirt: implemented; fault-inject: n/a."`.
   Maturity stays `"partial"`, reason stays `LIVE_DEPENDENCY`. **Do not** touch the
   `introspect.run` pointer (line 284) — that is B3's.
2. In `test_tool_docs.py`:
   - Remove `"introspect.from_vmcore"` from `_LOCAL_PLANNED_PROVIDER_TOOLS` (line 442). Leave
     `"introspect.run"` in the set (still B3-planned).
   - Add `test_introspect_from_vmcore_pointer_marks_wired_pending_live`: read the
     `introspect.from_vmcore` tool's `maturity_detail.providers`; assert maturity is `"partial"`;
     assert `"local-libvirt: wired"` **and** `"remote-libvirt: implemented"` are present; assert
     **neither** `"local-libvirt: planned"` **nor** `"local-libvirt: implemented"` appears. (The
     `local-libvirt: implemented` absent-check is prefixed, so the legitimately present
     `remote-libvirt: implemented` does not false-positive — verify by reading the string in the
     assertion.)
3. Regenerate the tool reference: `just docs` (rewrites `docs/guide/reference/introspect.md` from
   the registry). Review the diff: only the `introspect.from_vmcore` "Provider support:" line should
   change. Commit the regenerated doc with the code change so `docs-check` stays green.

**TDD note:** for a metadata + drift-guard change, write the new honesty assertion first (it fails
against the old pointer), update the pointer string to make it pass, then regenerate the doc. The
removal from `_LOCAL_PLANNED_PROVIDER_TOOLS` is required for the old guard to stop failing once the
pointer no longer says "planned".

**Acceptance check:** `introspect.from_vmcore` is `partial` with the pinned pointer; the new
assertion passes and the old planned-set guard no longer covers this tool; `just docs-check` clean.

**Guardrails:** `just lint && just type && uv run python -m pytest tests/mcp/core/test_tool_docs.py
-q && just docs-check`.

**Rollback:** restore the old pointer string, re-add the tool to `_LOCAL_PLANNED_PROVIDER_TOOLS`,
delete the new assertion, and `just docs` to regenerate.

## Final verification (before push, step 7)

- Run the **full** `just ci` once (catches architecture/doc/snapshot tests outside touched dirs).
- Re-grep for dead refs: `rg _real_read_vmcore_build_id` returns nothing.
- Confirm `LocalLibvirtLiveIntrospect` and the `introspect.run` maturity are byte-for-byte
  unchanged vs `main` (scope fence): `git diff main -- src/kdive/providers/local_libvirt/debug/introspect.py`
  shows changes only inside the Vmcore class / its `from_env` / the removed placeholder, and
  `git diff main -- src/kdive/mcp/tools/debug/introspect.py` touches only the `from_vmcore` pointer.

## Task dependency / ordering

1 → 2 → 3 → 4 is the natural order (2 depends on nothing from 1 but is cleaner after; 3 depends on 2;
4 is independent of 1-3 but shares the file with nothing they touch). Each task is its own commit.
Tasks are tightly coupled to one small file set and share the feature branch — implement **directly
in this session** (not parallel subagents); the change is one logical feature across four small,
ordered commits.
