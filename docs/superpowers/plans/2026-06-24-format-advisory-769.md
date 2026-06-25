# Plan — kdive advises the required external-build artifact format (#769)

- **Spec:** [`../specs/2026-06-24-format-advisory-769.md`](../specs/2026-06-24-format-advisory-769.md)
- **Decision of record:** ADR-0234 §5 (no new ADR).
- **Branch:** `feat/format-advisory-769`.
- **Guardrails (run before every commit):** `just lint`, `just type`, `just test` (CI runs each
  individually). For response/tool changes also `just docs-check` (regenerate with `just docs` if it
  reports drift) and `just resources-docs-check`. `just docs-links` for the spec/plan.
- **Conventions:** absolute imports only; ≤100 lines/function; Google-style docstrings on public
  APIs; pick the most specific `ErrorCategory`; tests mirror the package tree under `tests/`; follow
  the doc-style guard (plain factual prose, no inflated adjectives; "Milestone" not the s-word).

All tasks are TDD: failing test first, confirm it fails for the right reason, minimal implementation,
re-run focused test + guardrails, refactor green.

## Task 1 — Public upload-contract description in `build_artifacts/validation.py`

**Fits:** spec §A — the single source of truth the advisory projects.

**Files:** `src/kdive/build_artifacts/validation.py`,
`tests/build_artifacts/test_upload_contract.py` (new).

**Do:**
- Add frozen dataclasses `FormatContract`, `LayoutMember`, `ArtifactContract` (all with a
  `to_json() -> dict[str, JsonValue]`). Keep them data-only.
  - `FormatContract`: `container: str`, `magic: tuple[MagicPin, ...]` (a `MagicPin` =
    `{offset:int, hex:str}` view), `max_bytes: int | None = None`. Build the gzip/bzImage/ELF magic
    hex **from the existing constants** `_GZIP_MAGIC`, `_BZIMAGE_MAGIC` (+ `_BZIMAGE_MAGIC_OFFSET`),
    `_ELF_MAGIC` (`.hex()`), so the strings are not re-typed.
  - `LayoutMember`: `path: str`, `required: bool`, `note: str`, `format: FormatContract | None`.
  - `ArtifactContract`: `name`, `requirement: Literal["required","optional","conditional"]`,
    `summary`, `format: FormatContract`, `layout: tuple[LayoutMember, ...] = ()`,
    `notes: tuple[str, ...] = ()`.
- Add `EXTERNAL_BUILD_CONTRACTS: Mapping[str, ArtifactContract]` for `kernel`, `vmlinux`, `initrd`,
  `effective_config`:
  - `kernel`: required; container `"gzip tar"` magic gzip@0; layout `boot/vmlinuz`
    (required, nested bzImage `HdrS`@`_BZIMAGE_MAGIC_OFFSET` magic) and `lib/modules/`
    (`_MODULES_MEMBER_PREFIX`, required, note names the `<release>/` subdir + drop `build`/`source`
    symlinks); notes: gzip-only and "put boot/vmlinuz first (128 MiB decompress scan bound)".
  - `vmlinux`: optional; container `"ELF (uncompressed)"` magic ELF@0; notes: "requires a matching
    `build_id` argument in runs.complete_build (GNU build-id note)".
  - `initrd`: optional; container `"initramfs image"`; no magic enforced (empty magic tuple).
  - `effective_config`: conditional; container `"kernel .config (text)"`; `max_bytes` =
    validation.py's own public `EFFECTIVE_CONFIG_MAX_BYTES` (the constant this task promotes — **not**
    an import from the mcp uploads module; see the import-direction note); notes: "required when the
    Run's build profile carries `profile_requirements`; validated against that profile's required
    Kconfig symbols".
- **Import-direction note (one cap, owned low):** today the cap is duplicated —
  `validation.py` defines `_MAX_EFFECTIVE_CONFIG_BYTES = 1 MiB` (used at validation.py:188,195) and
  `uploads.py` defines its own `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES = 1 MiB` (used at uploads.py:234).
  `build_artifacts/validation.py` is the low-level module and must **not** import any `mcp` module, so
  validation.py is the single owner: rename its constant to a public `EFFECTIVE_CONFIG_MAX_BYTES`,
  update its two internal uses, and have `uploads.py` import that public name (replacing — not
  aliasing — its local `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES`). Imports flow mcp → build_artifacts only.
  No alias is kept (repo rule: replace, don't deprecate); one name, one source, so no lockstep assert
  is needed (spec §A finding 3 resolves by construction).

**Tests (`test_upload_contract.py`):**
- Byte-contract drift: `EXTERNAL_BUILD_CONTRACTS["kernel"].format.magic` hex == `_GZIP_MAGIC.hex()`
  at offset 0; the `boot/vmlinuz` member's nested magic hex == `_BZIMAGE_MAGIC.hex()` at
  `_BZIMAGE_MAGIC_OFFSET`; `vmlinux` magic == `_ELF_MAGIC.hex()`; `kernel` layout member paths ==
  `_KERNEL_BOOT_MEMBER` and `_MODULES_MEMBER_PREFIX` (literal); `effective_config.format.max_bytes`
  == `EFFECTIVE_CONFIG_MAX_BYTES`.
- `to_json()` round-trips to JSON-safe primitives (json.dumps succeeds; no dataclass leaks).
- Requirement-semantics drift (behavioral): a manifest missing `kernel` →
  `validate_external_artifacts` raises (proves `kernel` `requirement=="required"`). This is the cheap
  case — the validator early-returns at the kernel check (validation.py:129) before touching the
  store, so a trivial stub store suffices; no combined-tar/ELF fixture needed. The vmlinux→build_id
  and kernel-only-accepted behaviors are already proven by the existing validator suite
  (`tests/providers/local_libvirt/test_validate_external_artifacts.py`, which owns `_FakeStore`,
  `_combined_kernel_tar`, `_elf_with_build_id`); do not duplicate those heavy fixtures here — assert
  the `vmlinux` contract's note mentions `build_id` and reference that suite in a comment.

**Acceptance:** new dataclasses + `EXTERNAL_BUILD_CONTRACTS` exist; both drift tests pass; `uploads.py`
uses the shared `EFFECTIVE_CONFIG_MAX_BYTES`; `just lint type test` green.

**Rollback:** revert the file + delete the test; `uploads.py` cap import reverts to its local constant.

## Task 2 — Project the contracts from `artifacts.expected_uploads`

**Fits:** spec §B — the discoverable advisory.

**Files:** `src/kdive/mcp/tools/catalog/artifacts/expected_uploads.py`,
`tests/mcp/catalog/test_expected_uploads_tool.py` (update).

**Do:**
- Replace the flat `_NAME_DESCRIPTIONS`/`descriptions` projection with a `contracts` map:
  `{name → ArtifactContract.to_json()}` built from `EXTERNAL_BUILD_CONTRACTS` for the run owner kind.
  For the `system` owner kind, build a single minimal `rootfs` contract inline (required; summary;
  `FormatContract(container="filesystem image", magic=())`; no layout) — keep it in
  `expected_uploads.py`, not in the build-artifact validator (rootfs is not that validator's concern).
- `_owner_item` `data` gains `contracts`; the run item also gains `provider_neutral: True` and
  `doc: "resource://kdive/docs/operating/external-build-upload.md"`. `accepted_names` and
  `create_tool` unchanged. `contracts` keys must equal `accepted_names` (set equality).
- Keep the tool's docstring/maturity/auth posture unchanged so the generated tool reference does not
  move (run `just docs-check`; regenerate with `just docs` only if it reports drift).

**Tests (update existing):**
- Update `test_expected_uploads_projects_both_owner_vocabularies` to assert `contracts` keys ==
  `accepted_names` for both owner kinds (drop the old `descriptions` assertions).
- New: run item carries `provider_neutral is True` and the `doc` resource URI; `kernel` contract is
  `required` with gzip magic + both layout members; `vmlinux`/`initrd` `optional`;
  `effective_config` `conditional` with `max_bytes` and the Kconfig-source note; `vmlinux` note
  mentions `build_id`.
- New: `system` item's `contracts` == `{"rootfs": ...}`, required, `container=="filesystem image"`.
- Keep the read-only/auth-only registrar test.

**Acceptance:** `expected_uploads` returns the structured contracts; updated tests pass;
`just lint type test docs-check resources-docs-check` green.

**Rollback:** revert both files.

## Task 3 — `runs.create(source="external")` next-action wiring

**Fits:** spec §C bullet 1 — make the create response point into the upload loop.

**Files:** `src/kdive/services/runs/admission.py`,
`src/kdive/mcp/tools/lifecycle/runs/create.py`, and **extend** the existing
`tests/mcp/lifecycle/test_runs_tools.py` (it already asserts the created-run next actions at
~line 611: `["runs.get", "runs.build"]`). Do not create a new test directory.

**Do:**
- `admission.py`: add `is_external: bool = False` to `RunCreateResult`; set it at both
  `_created_result` call sites (`_create_locked` ~520, `_create_unbound` ~679) from
  `isinstance(build_profile, ExternalBuildProfile)` (add the import; `build_profile:
  ParsedBuildProfile` is in scope at both). Thread it via a new `is_external` param on
  `_created_result` — do not re-parse.
- `create.py`: in `_created_response`, branch on `result.is_external`:
  external → `suggested_next_actions=["runs.get", "artifacts.expected_uploads",
  "artifacts.create_run_upload"]`; server → `["runs.get", "runs.build"]` (unchanged). Use the
  literal tool-name constants `CREATE_RUN_UPLOAD_TOOL` / the expected-uploads tool name where they
  already exist, to avoid free-typed strings.

**Tests:**
- A `runs.create` with an external build profile returns the expected-uploads next actions (cover
  both bound and unbound paths if the existing tests already split them).
- A server build profile create still returns `["runs.get", "runs.build"]` (regression).
- Idempotency replay: the recorded envelope for an external create replays the external next actions
  (the envelope is built from `_created_response`, so this falls out — assert it if a create-keyed
  test fixture exists).

**Acceptance:** external create chains to the upload loop; server lane unchanged; `just lint type
test` green.

**Rollback:** revert both files; `RunCreateResult` default `is_external=False` keeps callers compiling.

## Task 4 — `runs.complete_build` validation-failure advisory

**Fits:** spec §C bullet 2 — a format rejection points back at the contract.

**Files:** `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`, and **extend** the existing
`tests/mcp/lifecycle/test_complete_build_tool.py` (shared setup in `tests/mcp/complete_build_support.py`).
Do not create a new test directory.

**Do:**
- In `_validate_external_build_upload`, the `except CategorizedError` branch returns
  `ToolResponse.failure_from_error(run_id, exc, suggested_next_actions=["artifacts.expected_uploads",
  "artifacts.create_run_upload"])`. Use the existing literal tool-name constants. Leave non-validation
  failure paths (wrong state, no manifest, expired window) unchanged — they have their own reasons.

**Tests:**
- A complete_build whose `validate_complete_build` seam raises a `CategorizedError` (inject via the
  `CompleteBuildHandlers.validate_complete_build` seam) returns a failure envelope whose
  `suggested_next_actions` include `artifacts.expected_uploads`.
- A successful complete_build does **not** carry those next actions (it chains to `runs.get` as today).

**Acceptance:** validation failure is self-correcting; success path unchanged; `just lint type test`
green.

**Rollback:** revert the one handler change + test.

## Order & integration

1 → 2 (2 imports from 1) → 3, 4 (independent of 2, can follow in any order). Run the **full** suite
(`just test`) + `just docs-check`/`resources-docs-check` once after Task 2 and again before push.

## Verification gaps / risks

- The requirement-semantics behavioral test depends on what validation fixtures already exist; the
  kernel-absent case is the guaranteed-cheap one (no store). Confirm fixtures during Task 1; if absent,
  scope to kernel-absent and note it (do not build a heavy fixture just for this).
- Moving the effective-config cap constant (Task 1) touches `uploads.py`; grep for every reference to
  `_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES` before the move so no caller is missed.
