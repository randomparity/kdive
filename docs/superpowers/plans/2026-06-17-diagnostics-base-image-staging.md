# Plan — Diagnostics base-image-staging check (#513)

- **Spec:** [`../../specs/2026-06-17-diagnostics-base-image-staging.md`](../../specs/2026-06-17-diagnostics-base-image-staging.md)
- **ADR:** [`0150`](../../adr/0150-diagnostics-base-image-staging-check.md)
- **Branch:** `feat/diagnostics-base-image-staging-513`
- **Guardrails (run before every commit):** `just lint`, `just type` (whole-tree),
  focused `uv run python -m pytest <path> -q`; before first push `just ci`.

Tasks are tightly coupled (one new check threaded through three files + tests), so this is
implemented directly in-session with TDD per task, not fanned out to subagents.

## Task 1 — `lookup_volume_staged` + `VolumeStaging` + shared fix constant (`storage.py`)

**Where it fits:** the single "is volume X staged?" path the diagnostic probe and the
companion #511 read both reuse; also de-duplicates the ADR-0080 staging-remediation string.

**Files:** `src/kdive/providers/remote_libvirt/lifecycle/storage.py`,
`tests/providers/remote_libvirt/lifecycle/test_storage.py` (or the existing storage test file).

**Steps (TDD):**
1. Write failing tests: a fake `StorageConn`/`Pool` whose lookups raise
   `libvirt.libvirtError` carrying `VIR_ERR_NO_STORAGE_POOL` / `VIR_ERR_NO_STORAGE_VOL` (and a
   generic code), plus a present-volume case. Assert `lookup_volume_staged` returns
   `VolumeStaging.POOL_ABSENT` / `ABSENT` / `STAGED`, and **re-raises** for the generic code.
2. Add `class VolumeStaging(StrEnum)` (`STAGED`/`ABSENT`/`POOL_ABSENT`).
3. Extract the verbatim "base image volume … is not staged on the remote host's storage pool (an
   operator prerequisite, ADR-0080)" message body into a **`storage.py`-local** constant that
   `ensure_named_overlay`'s `CategorizedError` uses (a refactor with no observable change; keep
   the `{base_image_volume: ...}` detail). Do **not** export it for cross-module reuse — the
   diagnostic `fix` constant lives in `checks.py` per Task 3 step 3 (the
   `diagnostics → providers` dependency direction forbids `storage.py` importing diagnostics, and
   the reverse import is unnecessary). The two remediation sentences are independent literals,
   each test-asserted.
4. Implement `lookup_volume_staged(conn, pool_name, volume_name) -> VolumeStaging`: lookup pool
   (map `NO_STORAGE_POOL`→`POOL_ABSENT`, re-raise others), lookup volume (map
   `NO_STORAGE_VOL`→`ABSENT`, re-raise others), else `STAGED`. No connection open/close.
5. `just lint && just type` + focused test green. Verify `ensure_named_overlay`'s existing tests
   still pass (the message refactor must not change its observable error).

**Acceptance:** three states returned for the three codes; generic `libvirtError` re-raised;
the provision-time error message is unchanged; constants exported.

## Task 2 — `resolve_base_image_staged_volume()` public resolver (`config.py`)

**Where it fits:** the one seam that turns the inventory into the staged volume name, reusing
`_resolve_instance` so the zero/multi-instance guard has a single home.

**Files:** `src/kdive/providers/remote_libvirt/config.py`,
`tests/providers/remote_libvirt/test_config.py` (existing).

**Steps (TDD):**
1. Failing tests via a tmp `systems.toml` (KDIVE_SYSTEMS_TOML): a `staged` `[[image]]` →
   returns its `.volume`; a `[[image]]` with a `build`/`s3` source → `CONFIGURATION_ERROR`;
   zero instances → `CONFIGURATION_ERROR`; two instances → `CONFIGURATION_ERROR` (reusing
   `_resolve_instance`'s message); a `base_image` naming an absent `[[image]]` →
   `CONFIGURATION_ERROR`.
2. Implement: `instance = _resolve_instance()`; load the doc (`_load_remote_instances` already
   parsed it — but to get `[[image]]` entries, load the doc once via `load_inventory_optional`),
   find `img` where `img.name == instance.base_image`, require `isinstance(img.source,
   StagedSource)`, return `img.source.volume`. Each failure raises `CategorizedError(
   CONFIGURATION_ERROR)` with an operator-legible message (no value interpolation beyond the
   operator-owned name).
3. `just lint && just type` + focused test green.

**Acceptance:** returns the staged volume for a valid inventory; the four error paths each raise
`CONFIGURATION_ERROR`; the multi-instance guard message matches `_resolve_instance`.

**Note:** confirm the cleanest doc load. If `_resolve_instance` and the image lookup would parse
twice, factor a tiny private helper returning `(instance, doc)` rather than duplicating the
multi-instance guard. Do not make the guard live in two places.

## Task 3 — `BaseImageStagingOutcome` + `BaseImageStagingCheck` + id constant (`checks.py`)

**Where it fits:** the three-state policy layer, mirroring `RemoteLibvirtReachabilityCheck`.

**Files:** `src/kdive/diagnostics/checks.py`, `tests/diagnostics/test_base_image_staging.py`.

**Steps (TDD):**
1. Failing check-logic tests (probe injected, no libvirt): each `BaseImageStagingOutcome` →
   expected `status`/`failure_category`/`fix`-presence/`provider`; `id` == new
   `BASE_IMAGE_STAGING_ID` == `"remote_libvirt_base_image_staging"`; `vantage` == `SERVER`; the
   `NOT_STAGED` `fix` equals `BASE_VOLUME_NOT_STAGED_FIX` (imported from storage).
2. Add `BASE_IMAGE_STAGING_ID` constant, `class BaseImageStagingOutcome(StrEnum)`,
   `BaseImageStagingProbe = Callable[[], Awaitable[BaseImageStagingOutcome]]`,
   `class BaseImageStagingCheck(Check)`. Map outcomes per spec §1. Reuse `_CONFIGURATION_ERROR`/
   `_TRANSPORT_FAILURE` module constants.
3. **Import boundary (settled):** `checks.py` is free of provider/`libvirt` imports by design,
   and `diagnostics` → `providers` is the only legal direction (a provider importing diagnostics
   would be an upward dependency). Therefore the diagnostic `fix` constant
   `BASE_VOLUME_NOT_STAGED_FIX` is **owned by `checks.py`** (it is diagnostic-output policy, not
   storage logic). `storage.py` keeps its own provision-time `CategorizedError` message unchanged
   (Task 1 only extracts that message into a `storage.py`-local constant for its own reuse, not a
   cross-module shared literal). The two sentences describe the same operator remediation but are
   independent literals, each asserted by its own test — this accepts a small, low-risk
   duplication (two short fixed strings) in exchange for keeping the dependency direction clean.
   The Task 1 reference to "share one literal" is **superseded by this**: do not introduce a
   neutral shared-constant module just to dedupe two sentences; the import-direction rule wins.
   `checks.py` gains **no** new import. Update Task 1 step 3 accordingly (a `storage.py`-local
   constant for the provision message; no cross-module export of the fix text).
4. `just lint && just type` + focused test green.

**Acceptance:** all four outcomes map correctly; `checks.py` has no new provider/libvirt import;
the `__post_init__` invariants are satisfied (fix only on fail, no category on pass).

## Task 4 — production probe adapter (`diagnostics/base_image_staging.py`)

**Where it fits:** the libvirt boundary for the check, mirroring `reachability.py`.

**Files:** `src/kdive/diagnostics/base_image_staging.py`,
`tests/diagnostics/test_base_image_staging.py` (probe section).

**Steps (TDD):**
1. Failing probe-adapter tests (libvirt faked): fake conn with
   `storagePoolLookupByName`/`storageVolLookupByName`/`close`; drive `STAGED`/`NOT_STAGED`
   (`NO_STORAGE_VOL`)/`INDETERMINATE` (`NO_STORAGE_POOL` and a generic libvirtError);
   connect-error (`open_connection` raises `libvirt.libvirtError`) → `UNREACHABLE`; bad config
   (`config_factory`/`volume_factory` raises `CONFIGURATION_ERROR`) → `INDETERMINATE` with the
   opener never called; assert `close()` called on the success path.
2. Implement `base_image_staging_probe(...)` per spec §3: resolve config + volume (catch
   `CategorizedError`→ `INDETERMINATE`), `asyncio.to_thread(_probe_sync, ...)`, inside use
   `remote_connection[_StorageProbeConn]`, catch `CategorizedError` (`TRANSPORT_FAILURE`→
   `UNREACHABLE`, else `INDETERMINATE`) and `libvirt.libvirtError` (storage RPC after open →
   `INDETERMINATE`, logged). Define `_StorageProbeConn(Protocol)` = `StorageConn` + `close()`;
   the default opener narrows `libvirt.open` to it (cast at the seam, like `open_libvirt_protocol`).
   Default `secret_backend_factory` = fresh per-probe `SecretRegistry` backend.
3. `just lint && just type` + focused test green.

**Acceptance:** every outcome reachable from the faked boundary; bad config never opens a
connection; the success path closes the connection; no secret value is logged.

## Task 5 — wiring + default-factory tests (`service.py`, `test_default_factory.py`)

**Files:** `src/kdive/diagnostics/service.py`, `tests/diagnostics/test_default_factory.py`.

**Steps (TDD):**
1. Extend `test_factory_includes_reachability_and_tls_acl_metadata_when_remote_configured` (or a
   new test) to assert `BASE_IMAGE_STAGING_ID` is in the assembled runnable set when remote is
   configured, and absent when not configured (`test_factory_omits_remote_checks_when_not_configured`
   currently asserts `== {SECRET_REF_ID}`; update to include the new id under the remote-configured
   path only — the not-configured assertion stays `{SECRET_REF_ID}`).
2. Add a run-level test: force the staging probe (monkeypatch
   `base_image_staging.base_image_staging_probe`) to a `STAGED` and a `NOT_STAGED` outcome and
   assert the aggregated report's item for `BASE_IMAGE_STAGING_ID` is `pass` / `fail` with the
   shared fix, alongside the still-passing reachability + secret_ref.
3. Import `base_image_staging` at `service.py` module top (like `reachability`); append the check
   in `_remote_libvirt_checks()`.
4. `just lint && just type` + `uv run python -m pytest tests/diagnostics -q` green.

**Acceptance:** the check is assembled iff remote is configured; the report carries its verdict;
existing reachability/secret_ref/worker-substitution assertions still pass.

## Task 6 — runbook sync + ADR-cited-in-src guard

**Files:** `docs/operating/runbooks/doctor-exit-criterion.md`, (verify) ADR status guard.

**Steps:**
1. Add a row to the doctor-exit-criterion check table: fault = unstaged base-image volume,
   check = `remote_libvirt_base_image_staging`, fix = the shared remediation, doctor exit = `1`.
   Only assert an integration-test row if `test_doctor_exit_criterion.py`'s fault-injection seam
   supports seeding a missing-volume condition without a live host; otherwise extend the runbook
   table only and note the unit/probe coverage. (Inspect the integration test before editing it.)
2. Run `uv run python scripts/check_adr_status.py` — ADR-0150 is now cited in `src/` (via the
   docstring/constant reference), so the guard's "no shipped-but-Proposed drift" invariant
   requires Status `Accepted` (already set). Confirm green.
3. `just docs-links && just docs-paths` green.

**Acceptance:** runbook table lists the new check; `check_adr_status.py` green with 0150 Accepted.

## Final — full gate + ship

1. `just ci` fully green (lint, type, docs-check, config-docs-check, config-guard,
   env-docs-check, adr-status-check, doc-links/paths, mermaid, test). `docs-check`/
   `config-docs-check` should be unaffected (no new tool/param/config), but run them to confirm.
2. Fold any fixups into their logical commits before the first push.
3. `/challenge --base main` review loop; `security-review` of the diff.
4. Push, open PR vs `main`, drive to green + `MERGEABLE`/`CLEAN`. Hand off (no self-merge).

## Rollback / cleanup

Pure-additive: the check, probe, resolver, and helper are new; the only edit to existing behavior
is the `storage.py` message-constant refactor (observable error unchanged — covered by Task 1's
regression assertion). Reverting the branch removes the check cleanly. No migration, DDL, or
config change to roll back.
