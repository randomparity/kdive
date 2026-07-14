# Implementation plan — arch-aware kernel-artifact upload contract (#1145)

Spec: `docs/design/2026-07-13-arch-aware-kernel-artifact-contract-1145.md` ·
ADR: `docs/adr/0343-arch-aware-kernel-artifact-contract.md`

Branch: `feat/arch-aware-kernel-artifact-contract-1145` · Base: `main` ·
**No migration, no schema change** (`build_profile` is opaque jsonb; an absent `arch`
defaults to `x86_64`).

TDD throughout: write the failing test first, then the code. Commit per task with a
conventional message ending in the repo's `Co-Authored-By` trailer. Keep guardrails green at
each commit — `just lint` (ruff), `just type` (ty, whole tree), `just test`; run `just ci`
before push. Tasks that touch tool docstrings/`Field`s must regenerate the tool reference
(`just docs`) and commit it, or `just docs-check` fails CI.

## Ground truth (verified this session)

- `SUPPORTED_ARCHES = frozenset(_TRAITS) = {x86_64, ppc64le}` in
  `src/kdive/domain/platform/arch_traits.py:74`. `build_artifacts` may import `domain`
  (lower layer); it already imports `kdive.domain.errors`.
- `BuildProfile` (`src/kdive/profiles/build.py:32`) has only `schema_version: Literal[1]`,
  `ConfigDict(extra="forbid", frozen=True)`. `BuildProfile.parse` maps `ValidationError` →
  `CONFIGURATION_ERROR` and scrubs values. `dump_build_profile` is `model_dump(mode="json")`.
- `runs.create` wrapper payload (`src/kdive/mcp/tools/lifecycle/runs/registrar.py:51`) types
  `build_profile: BuildProfile = Field(...)` **directly**, so a new `BuildProfile` field
  auto-surfaces in the runs.create tool schema and the generated reference. Its `Field`
  description still says "currently just {'schema_version': 1}" — must be updated.
- `validate_external_artifacts` (`src/kdive/build_artifacts/validation.py:230`) is called by:
  `services/runs/complete_build.py:179`, `tests/providers/local_libvirt/`
  `test_validate_external_artifacts.py` (~14 sites), `tests/build_artifacts/`
  `test_upload_contract.py:88`. Every existing call uses keyword args and none pass `arch`.
- The boot-member check is `_member_is_bzimage` (validation.py:399), driven by
  `_verify_combined_tar_shape` (372) via `_check_kernel_combined_tar` (337) ←
  `_check_artifact_content` (329, `name == "kernel"`) ← `_validate_one_artifact` (307).
- `_decompress_bounded` (validation.py:355) returns only truncated bytes today; the caller
  cannot tell "hit cap" from "clean EOF".
- The contract advertisement is `EXTERNAL_BUILD_CONTRACTS["kernel"]` (validation.py:125), a
  `LayoutMember` whose boot member carries one `FormatContract` (`format`), serialized by
  `LayoutMember.to_json` (validation.py:84). Read by `artifacts.expected_uploads`
  (`src/kdive/mcp/tools/catalog/artifacts/expected_uploads.py`).
- Generated tool reference `docs/guide/reference/runs.md` (from `scripts/gen_tool_reference.py`,
  gated by `just docs-check`) lists `build_profile` / `schema_version`. Regenerate with
  `just docs` after Task 1/5.

**Tests to touch:** `tests/profiles/test_build.py` (BuildProfile),
`tests/providers/local_libvirt/test_validate_external_artifacts.py` (validator),
`tests/mcp/lifecycle/test_complete_build_tool.py` + `test_runs_tools.py` (handler),
`tests/mcp/complete_build_support.py` (shared `FakeValidator`),
`tests/services/runs/test_complete_build.py` (+ its `unexpected_validator`),
`tests/adversarial/test_complete_build_concurrency.py` (inline validator wrapper),
`tests/build_artifacts/test_upload_contract.py` + `tests/mcp/catalog/test_expected_uploads_tool.py`
(advertisement), `tests/mcp/core/test_tool_docs.py` (every-parameter-has-a-description).

**`CompleteBuildValidation` test doubles (Task 3 breaks these if unlisted).** The callable is
realized by 3-arg doubles: `tests/mcp/complete_build_support.py` `FakeValidator.__call__`
(used ~20× across the complete_build suites), the inline wrapper in
`tests/adversarial/test_complete_build_concurrency.py:35` (which both receives and forwards 3
positional args), and `unexpected_validator` in `tests/services/runs/test_complete_build.py`.
Task 3 makes `arch` **keyword-only with an `x86_64` default** on the type and every double, so
unrelated cases need no per-call edit and none raise `TypeError`.

---

## Task 1 — `BuildProfile.arch` field + `SUPPORTED_ARCHES` validator + tests

**Where it fits:** spec §1. Adds the explicit, persisted arch declaration the whole upload
loop keys off; rejects an unknown arch at `runs.create` (parse) before any upload.

**Test first (`tests/profiles/test_build.py`):**
- `BuildProfile.parse({"schema_version": 1})` succeeds and `.arch == "x86_64"` (default).
- `BuildProfile.parse({"schema_version": 1, "arch": "ppc64le"})` → `.arch == "ppc64le"`.
- `BuildProfile.parse({"schema_version": 1, "arch": "s390x"})` raises `CategorizedError`
  with `category == CONFIGURATION_ERROR`; assert the error detail names field `arch` and
  does **not** leak the submitted value (`s390x` absent from the message — redaction).
- `dump_build_profile(BuildProfile.parse({"schema_version": 1, "arch": "ppc64le"}))` includes
  `"arch": "ppc64le"`; the default profile dumps `"arch": "x86_64"`.

**Code (`src/kdive/profiles/build.py`):**
- Add `arch: str = Field(default="x86_64", description=...)` where the description names the
  allowed values (from sorted `SUPPORTED_ARCHES`) and the `x86_64` default — this is the
  agent-facing text that serializes into the runs.create nested schema (AC#6), since the
  parameter-description test does **not** recurse into nested models (see Task 5).
- Add a `field_validator("arch")` that raises `ValueError` when `value not in
  SUPPORTED_ARCHES` (imported from `kdive.domain.platform.arch_traits`), so `.parse`'s
  existing `ValidationError → CONFIGURATION_ERROR` mapping applies. Message names the
  supported set (e.g. `"unsupported arch; expected one of x86_64, ppc64le"`) — use the sorted
  set, not the submitted value (redaction).
- Update the class/module docstring: the profile now carries `schema_version` **and** `arch`
  (default `x86_64`); it stays a v1 document.

**Acceptance:** the listed tests pass; existing `test_build.py` cases stay green;
`just lint`/`just type`/`just test` green. `schema_version` stays `Literal[1]`.

**Notes:** use a `field_validator` (not a `Literal`) so `SUPPORTED_ARCHES` stays the single
source (spec §1). `extra="forbid"` already rejects typos like `"arche"`.

---

## Task 2 — arch-keyed payload validation in `validation.py` + tests

**Where it fits:** spec §2. Generalizes the x86-only bzImage boot-member check to a per-arch,
machine-strict gate, with the cap-reached message fix and the table-coverage invariant.

**Test first (`tests/providers/local_libvirt/test_validate_external_artifacts.py`):**
- **x86 unchanged:** existing bzImage tests pass **unmodified** (the new `arch` param defaults
  to `x86_64`). Keep at least one call that omits `arch` to prove the default.
- **ppc64le validates (AC#2):** a combined tar whose `boot/vmlinuz` is a minimal ELF64-LE
  header with `e_machine = EM_PPC64 (21)` + a `lib/modules/<ver>/` member validates under
  `arch="ppc64le"`.
- **Arch mismatch (AC#3):** (a) a bzImage boot member under `arch="ppc64le"` →
  `BUILD_FAILURE`; (b) an ELF boot member under `arch="x86_64"` → `BUILD_FAILURE` (existing
  "no bzImage member" path); (c) an ELF64-LE header with `e_machine = EM_X86_64 (62)` under
  `arch="ppc64le"` → `BUILD_FAILURE` (the `e_machine` pin — the leak the ELF-prefix alone
  would miss). Assert each message names the arch.
- **Unknown arch to the validator:** `validate_external_artifacts(..., arch="s390x")` →
  `CONFIGURATION_ERROR` (defensive fail-fast; caller does not trust its input).
- **Cap-reached message (AC#5):** **monkeypatch `_KERNEL_TAR_SCAN_MAX_BYTES` to a small
  value** for these two cases (precedent: `test_validate_external_artifacts.py:196` already
  does `monkeypatch.setattr(validation_module, "_KERNEL_TAR_SCAN_MAX_BYTES", 256)`) — do
  **not** build a real 128 MiB fixture. The patched cap must be **large enough** to hold the
  boot member's 512-byte tar header plus its magic extent (~`0x206` bytes for x86, ~`0x14` for
  ppc64le) so `boot_ok` becomes `True`, yet **smaller** than the padded boot-member content so
  `cap_reached` fires before any `lib/modules` header (256 is too small for `boot_ok`; size it
  to e.g. a few KB above the boot member's magic extent, below the padded content). Then: a
  small boot-only tar under the patched cap (valid boot member, **no** modules) → the plain
  `no lib/modules member within the scan bound`; a tar whose boot member is padded past the
  patched cap before any modules header → the oversized-boot-member message. Use a
  compressible pad so the gzip stays small.
- **Coverage invariant (AC#4a):** `set(BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES`.
- **ppc64le `vmlinux` build-id (AC#2):** `extract_build_id_ranged` already requires ELF64-LE;
  add a ppc64le-flavored case (a synthetic ELF64-LE with a GNU build-id note) to confirm it
  works and the declared-build-id match/mismatch paths hold.

**Code (`src/kdive/build_artifacts/validation.py`):**
- Add constants: `_ELF64LE_PREFIX = b"\x7fELF\x02\x01"`, `_EM_PPC64_LE16 = (21).to_bytes(2,
  "little")`.
- Add `BOOT_MEMBER_FORMATS: Mapping[str, FormatContract]` (spec §2): `x86_64` = the existing
  bzImage `FormatContract`; `ppc64le` = `FormatContract(container="ELF (vmlinux)",
  magic=(MagicPin(0, _ELF64LE_PREFIX.hex()), MagicPin(0x12, _EM_PPC64_LE16.hex())))`.
- Module-level coverage assert: `assert set(BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES` (a real
  check that raises at import if drifted; the AC#4a test also asserts it so `-O` cannot hide
  it).
- Thread `arch: str = "x86_64"` through `validate_external_artifacts` →
  `_validate_one_artifact` / `_check_artifact_content` → `_check_kernel_combined_tar` →
  `_verify_combined_tar_shape` → the boot-member check.
- Replace `_member_is_bzimage` with `_member_matches_format(archive, member, fmt)`: read
  `max(pin.offset + len(bytes.fromhex(pin.hex)) for pin in fmt.magic)` bytes and require
  **all** pins to match at their offsets.
- In `_check_kernel_combined_tar` / `_verify_combined_tar_shape`, resolve
  `fmt = BOOT_MEMBER_FORMATS.get(arch)`; if `None`, raise `CategorizedError(...,
  CONFIGURATION_ERROR)` naming the supported set. On boot-member mismatch, raise
  `BUILD_FAILURE` with an arch-naming message (e.g.
  `f"kernel combined tar boot/vmlinuz is not a {arch} kernel"`).
- Make `_decompress_bounded` return `(bytes, cap_reached)` where
  `cap_reached = len(out) >= max_out` (update the one caller). In
  `_verify_combined_tar_shape`, when `boot_ok and cap_reached and not modules_ok`, raise the
  oversized-boot-member message; else keep the existing `no lib/modules member within the scan
  bound`. (`_KERNEL_TAR_SCAN_MAX_BYTES` unchanged.)

**Acceptance:** all listed tests pass; the x86 behavior tests pass unmodified; the import-time
coverage assert holds; `just lint`/`just type`/`just test` green.

**Notes:** keep messages BUILD_FAILURE for payload-shape failures (matches the existing
taxonomy); reserve CONFIGURATION_ERROR for the unknown-arch defensive path. Do not change the
**production default** of `_KERNEL_TAR_SCAN_MAX_BYTES` (spec AC#5) — a per-test
`monkeypatch` of it is fine and is the established way to exercise the scan bound cheaply.

---

## Task 3 — thread arch from the persisted profile through `complete_build` + tests

**Where it fits:** spec §3. `runs.complete_build` reads `arch` from the Run's persisted
`build_profile` and passes it to the validator — **without** re-validating the whole profile
at finalize (decouples finalize-ability from `SUPPORTED_ARCHES` evolution).

**Test first:**
- `tests/mcp/lifecycle/test_complete_build_tool.py`: with a stub validator injected
  (`CompleteBuildHandlers(validate_complete_build=...)`), assert the handler passes
  `arch="ppc64le"` when the Run's persisted `build_profile` has `arch: ppc64le`, and
  `arch="x86_64"` when the profile omits `arch`. (Capture the arg in the stub.)
- Service-level (`tests/services/runs/` or the existing complete_build service test): a Run
  whose persisted `build_profile` carries `arch: ppc64le` drives
  `validate_external_artifacts(..., arch="ppc64le")`.

**Code:**
- `src/kdive/services/runs/complete_build.py`: extend the `CompleteBuildValidation` callable
  type and `CompleteBuildFinalizer._validate_complete_build` / `_validate_uploads` to take
  and forward `arch` as a **keyword-only argument with an `x86_64` default**
  (`arch: str = "x86_64"`, keyword-only) — so the default calls
  `validate_external_artifacts(..., arch=arch)` and every test double can accept it without a
  per-call edit. Read `arch` from the persisted profile in `_prepare` (or `complete`):
  `arch = str(run.build_profile.get("arch", "x86_64"))` — `run.build_profile` is the
  serialized jsonb mapping; **do not** call `BuildProfile.parse` here (spec §3).
- `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`: the injected-validator seam
  (`validate_complete_build`) signature gains the keyword-only `arch`; no new MCP parameter on
  the wrapper.
- **Update every `CompleteBuildValidation` double** (see the "Tests to touch" note):
  `tests/mcp/complete_build_support.py` `FakeValidator.__call__` gains `*, arch: str =
  "x86_64"`; the inline wrapper in `tests/adversarial/test_complete_build_concurrency.py:35`
  gains it on both its `__call__` and its forwarding call to `self._inner(...)`;
  `unexpected_validator` in `tests/services/runs/test_complete_build.py` gains it. Existing
  cases that ignore arch keep working unchanged.

**Acceptance:** the handler/service tests pass; a persisted ppc64le profile threads
`arch="ppc64le"`; an absent arch threads `x86_64`; `just test` green.

**Notes:** `Run.build_profile` is `SerializedBuildProfile` (a dict) — read the field
directly. The validator's own fail-fast (Task 2) is the backstop for a corrupt value.

---

## Task 4 — arch-aware advertisement (`formats_by_arch`) + no-drift test

**Where it fits:** spec §4. `artifacts.expected_uploads` advertises the per-arch boot-member
format from the same `BOOT_MEMBER_FORMATS` the validator enforces, so an agent learns both
arches from MCP alone.

**Test first:**
- `tests/build_artifacts/test_upload_contract.py`: the `kernel` contract's boot `LayoutMember`
  `to_json` has **no** `format` key and a `formats_by_arch` map with `x86_64` and `ppc64le`
  entries, each `{container, magic:[{offset,hex}]}`; assert the magics equal
  `BOOT_MEMBER_FORMATS[arch].to_json()` (the no-drift assertion, per ADR-0234 §5).
- `tests/mcp/catalog/test_expected_uploads_tool.py`: `artifacts.expected_uploads` run item's
  `contracts.kernel.layout[boot].formats_by_arch` carries both arches; update any snapshot of
  the kernel contract JSON deliberately.

**Code (`src/kdive/build_artifacts/validation.py`):**
- Give the `boot/vmlinuz` `LayoutMember` a per-arch representation. Add
  `formats_by_arch: Mapping[str, FormatContract] | None = None` to `LayoutMember`, set it to
  `BOOT_MEMBER_FORMATS` on the kernel boot member, and **drop** its single `format`. In
  `LayoutMember.to_json`, emit `formats_by_arch: {arch: fmt.to_json()}` when present (mutually
  exclusive with `format`). Keep `lib/modules` (no format) and all other contracts untouched.
- Update the `kernel` `ArtifactContract` `summary` and the boot member `note` to name both
  arches ("the bzImage for x86_64 or the ELF `vmlinux` for ppc64le; arch is declared in the
  build profile").

**Acceptance:** the no-drift test passes; `expected_uploads` shows both arches; the kernel
contract JSON snapshot is updated; `just test` green.

**Notes:** per "replace, don't deprecate," there is no dual `format`+`formats_by_arch` shim —
the boot member's `format` is gone. The "x86 unchanged" claim scopes to validator behavior
(Task 2), not this advertisement (spec AC#1).

---

## Task 5 — agent-facing docstrings + `external-build-upload.md` + regenerate reference

**Where it fits:** spec §4. The wrapper docstrings and the docs resource are the agent-facing
contract (AGENTS.md: the wrapper docstring is what the agent sees) and must name the per-arch
payload expectation in the same PR as the behavior.

**Test first:**
- `test_every_parameter_has_a_description` iterates only **top-level** params (`request`), so
  it does **not** guard the nested `build_profile.arch` description — Task 1 supplies that via
  an explicit `Field(description=...)`. Add a direct assertion (here or in `test_tool_docs.py`)
  that the runs.create schema's nested `build_profile.arch` carries a non-empty description and
  names the allowed values, so AC#6 discoverability is actually guarded.
- If a test asserts the `external-build-upload.md` content or the runs.create `Field` text,
  update it; otherwise the generated-reference `just docs-check` is the guard.

**Code:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py:51`: update the `build_profile` `Field`
  description — the profile now also carries `arch` (default `x86_64`; `ppc64le` for a POWER
  build), and the arch selects the boot-member payload format. (The nested `arch` `Field`
  description comes from Task 1's model.)
- `src/kdive/mcp/tools/lifecycle/runs/complete_build.py` wrapper docstring: name the per-arch
  boot-member expectation (the boot member format follows the build profile's `arch`:
  bzImage for x86_64, ELF `vmlinux` for ppc64le).
- `src/kdive/mcp/resources/_content/external-build-upload.md`: replace the x86-literal
  boot-member table row with a per-arch pair (bzImage `HdrS`@`0x202` for x86_64; ELF64-LE +
  `EM_PPC64` `e_machine`@`0x12` for ppc64le), add the stripped-ELF ppc64le `tar` recipe
  variant (spec §4 — `strip -s vmlinux -o …` then tar the stripped copy), and note the
  unstripped DWARF `vmlinux` belongs in the optional `vmlinux` artifact, not the boot member.
- Run `just docs` to regenerate `docs/guide/reference/runs.md` (build_profile now shows
  `arch`); commit the regenerated reference.

**Acceptance:** `test_tool_docs` green; `just docs-check` green (reference regenerated and
committed); `docs-links`/`docs-paths` green; the resource names both arches. `just ci` green.

---

## Final verification (before PR)

- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
- Existing `test_validate_external_artifacts.py` x86 behavior cases pass **unmodified**
  (proves the validator `arch` default).
- New coverage invariant holds (`set(BOOT_MEMBER_FORMATS) == SUPPORTED_ARCHES`) at import and
  in the unit test.
- `just docs-check` green — the generated reference reflects `build_profile.arch`.
- `git grep -n "the bzImage, NOT"` shows the x86-literal contract language is gone from the
  validator summary and the docs resource (replaced by the per-arch wording).

## Rollback / cleanup

Additive and backward compatible — no migration, no data backfill. Reverting the branch
restores the x86-only contract; a persisted `build_profile` with `arch` becomes an ignored
extra field only if `extra="forbid"` is also reverted (it is part of this change, so a full
revert is clean). No external-service or destructive operations.
