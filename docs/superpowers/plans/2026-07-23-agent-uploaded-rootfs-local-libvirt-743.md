# Implementation plan — agent-uploaded rootfs for local-libvirt (#743)

- **Issue:** #743  **ADR:** [ADR-0434](../../adr/0434-local-libvirt-agent-uploaded-rootfs-staging.md)
- **Spec:** [../../specs/2026-07-23-agent-uploaded-rootfs-local-libvirt-743-design.md](../../specs/2026-07-23-agent-uploaded-rootfs-local-libvirt-743-design.md)
- **Branch:** `feat/agent-rootfs-image-743`  **Base:** `main`
- **Guardrails:** `just lint` · `just type` (whole tree) · `just test` · `just ci` (full PR gate;
  CI runs sub-recipes individually). No migration; no MCP-surface/doc regen expected.

## Shape & ordering

All tasks edit an overlapping cluster (`materialize.py`, `provisioning.py`, `storage.py`,
`jobs/handlers/systems.py`) → **serial, single implementer, TDD** (red → green per task). No
parallelism. Each task: write failing test(s) first, implement, run the task's guardrail subset,
commit. Run full `just ci` before the final push.

Conventions: `CategorizedError(msg, category=ErrorCategory.X, details={...})` for all failures;
absolute imports only; ≤100 lines/function; Google-style docstrings on new public functions;
mirror the existing catalog-fetch / overlay-reclaim patterns rather than inventing new ones.

---

## Task 1 — Stage the uploaded rootfs outside `allowed_roots`

**Fits:** ADR-0434 §3 (no-escape invariant). Prerequisite for everything else (path move).

**Do:**
- In `src/kdive/providers/local_libvirt/lifecycle/storage.py`, add a module constant
  `UPLOADS_DIR = str(Path(ROOTFS_DIR).parent / "rootfs-uploads")` (sibling of the catalog
  `rootfs-cache`, outside the default `allowed_roots = [ROOTFS_DIR]`).
- **Do NOT change `upload_rootfs_path`'s body** — it already joins the caller-supplied
  `upload_dir` (that injectable seam is what Task 4/tests rely on). Change only the caller:
  `provisioning.py::_materialize_rootfs_base` passes `Path(UPLOADS_DIR)` as the
  `RootfsUploadContext` `upload_dir` (was `Path(ROOTFS_DIR)`) — verify that context construction
  at `provisioning.py:570` is the only such caller.
- **Widen `upload_rootfs_path`'s `system_id` param to `UUID | str`** (matching `overlay_path` /
  `baseline_dir`) so Task 4 can pass `domain_name.removeprefix("kdive-")` (a `str`) without a
  `ty` failure.

**Tests (`tests/providers/local_libvirt/`):**
- `upload_rootfs_path(...)` resolves under `rootfs-uploads`, and that dir is **not** under any
  path in the provisioner default `allowed_roots` (`[ROOTFS_DIR]`). (AC6)
- **Migrate any existing test asserting the old staging location** (e.g. in `test_materialize.py`,
  see Task 2) to the `rootfs-uploads` path.

**Guardrail:** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt -q`

---

## Task 2 — Connectionless upload fetch with checksum verify

**Fits:** ADR-0434 §1–§2. The core download.

**Do:**
- In `materialize.py`: add `type UploadFetch = Callable[[RootfsUploadContext], Path]`; add
  `upload_fetch: UploadFetch | None = None` to `RootfsMaterializationContext`; make
  `_materialize_uploaded_rootfs` keep its `context.upload is None` guard, then delegate to
  `context.upload_fetch(context.upload)`, raising `CONFIGURATION_ERROR` ("upload rootfs
  materialization is not wired for this lane") when `upload_fetch is None` (mirror the catalog
  unwired branch).
- **Migrate the existing stub tests** in `tests/providers/local_libvirt/test_materialize.py`
  (`test_materialize_uploaded_rootfs_uses_system_keyed_path:112`,
  `test_materialize_uploaded_rootfs_requires_system_context:126`) — they assert the old no-I/O
  stub. Update them to the new delegate/raise behavior (wired fake fetch → path; `upload_fetch
  None` → CONFIG error; `upload None` → CONFIG error). Do this in the same task so Task 2's
  guardrail is green.
- New module `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`:
  `rootfs_upload_fetch_from_env() -> UploadFetch`. The returned `_fetch(upload)`:
  1. `dest = upload_rootfs_path(upload.tenant, upload.system_id, upload_dir=upload.upload_dir)`;
     if `dest.is_file()`, return it (idempotent reuse of a verified file).
  2. Build store lazily via `object_store_from_env()`; `key = artifact_key(upload.tenant,
     "systems", str(upload.system_id), "rootfs")`.
  3. `head = store.head(key)`; `None` → `CONFIGURATION_ERROR` ("upload-kind rootfs was never
     uploaded"); `head.checksum_sha256 is None` → `INFRASTRUCTURE_FAILURE`/`CONFIGURATION_ERROR`
     ("uploaded rootfs object has no stored checksum").
  4. `data = store.get_artifact(key, None).data`; recompute
     `base64.b64encode(hashlib.sha256(data).digest()).decode()`; on mismatch raise
     `INFRASTRUCTURE_FAILURE` ("uploaded rootfs object failed checksum verification").
  5. `mkdir(parents=True, exist_ok=True)` the uploads dir; write to
     `dest.with_suffix(".qcow2.partial")`; `os.replace` into `dest`; unlink the temp on OSError
     (mirror `_materialize_s3_rootfs`); return `dest`.
- Keep the fetch **connectionless** (no psycopg). Introduce a narrow `Protocol` for the store
  capability it needs (`head`, `get_artifact`) so tests inject a fake, mirroring
  `RootfsObjectStore` in `images/rootfs/fetch.py`.

**Tests (`tests/providers/local_libvirt/`, fake store):**
- happy path writes bytes to `dest` and returns it (AC1); missing object → CONFIG error (AC2);
  checksum mismatch → INFRA error; `checksum_sha256 None` → reject (AC3); present `dest` → no
  HEAD/GET (assert fake store methods not called) (AC4); a GET/replace failure leaves no `dest`
  (AC5); `materialize_rootfs_base(_UploadRootfs)` with wired fetch returns its path, `None` →
  CONFIG error (AC7).

**Guardrail:** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt -q`

---

## Task 3 — Wire the fetch into the provisioner + defensive validate short-circuit

**Fits:** ADR-0434 §1, §6.

**Do:**
- `provisioning.py`: `LocalLibvirtProvisioning.__init__` gains `upload_fetch: UploadFetch |
  None = None`; store as `self._upload_fetch`. `from_env` wires
  `upload_fetch=rootfs_upload_fetch_from_env()`. `_materialize_rootfs_base` passes
  `upload_fetch=self._upload_fetch` into `RootfsMaterializationContext`.
- Add `if isinstance(rootfs, _UploadRootfs): return` at the top of `validate_rootfs_ref` (defer
  upload validation to provision, as `catalog` already is); update its docstring.

**Tests (`tests/providers/local_libvirt/test_provisioning.py`):**
- `provision` on an upload profile invokes the injected `upload_fetch` and passes its returned
  path to `make_overlay` as the base (fake `connect`, fake `ProvisioningFiles`, fake
  `free_port`, fake `extract_baseline_kernel`, injected fake `upload_fetch`) (AC8).
- `validate_rootfs_ref(_UploadRootfs(kind="upload"))` returns without invoking `upload_fetch`
  (assert not called) (AC11).

**Guardrail:** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt -q`

---

## Task 4 — Teardown: local file (fail-loud) removal

**Fits:** ADR-0434 §4 (local half).

**Do:**
- `storage.py`: add `_real_remove_uploaded_rootfs(path: str)` (`Path(path).unlink(missing_ok=True)`;
  raise `INFRASTRUCTURE_FAILURE` on other `OSError`, mirroring `_real_remove_overlay`). Add
  `remove_uploaded_rootfs: RemoveOverlay = _real_remove_uploaded_rootfs` to `ProvisioningFiles`
  and a `remove_uploaded_rootfs_for_domain(domain_name)` method computing the per-System uploaded
  path via `upload_rootfs_path("local", domain_name.removeprefix("kdive-"),
  upload_dir=Path(UPLOADS_DIR))` (relies on the Task 1 `UUID | str` signature widening).
- `provisioning.py::teardown`: call `self._files.remove_uploaded_rootfs_for_domain(domain_name)`
  after `remove_overlay_for_domain` / `remove_baseline_for_domain`.

**Tests:**
- `teardown` unlinks the per-System uploaded file (fake files records the call); absent file →
  no-op; a real `OSError` → `INFRASTRUCTURE_FAILURE` (AC9).

**Guardrail:** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt -q`

---

## Task 5 — Teardown handler: reclaim the S3 object (object best-effort, row fail-loud)

**Fits:** ADR-0434 §4 (S3 half).

**Do:**
- `src/kdive/jobs/handlers/systems.py`: add a helper that, for `system_id`, computes
  `key = artifact_key("local","systems",str(system_id),"rootfs")`, deletes the **object**
  best-effort (inside the existing `try/except` reclaim block with console/sysrq), and deletes
  the **`artifacts` row** for that exact key in its own transaction **outside** the best-effort
  block (fail-loud, placed with/near `delete_system_bootstrap_key`). A `DELETE ... WHERE
  owner_id=%s AND object_key=%s` matching nothing (non-upload System) is a no-op.
- Order in `teardown_handler`: object delete alongside `_reclaim_console_artifacts` /
  `_reclaim_sysrq_artifacts` (best-effort); row delete with the bootstrap-key transaction.

**Tests (`tests/jobs/` or `tests/integration/`):**
- object + row deleted for an upload System (AC10); a store fault on the object delete does not
  block teardown but the row is still deleted (AC13); a non-upload System → no-op, no raise.

**Guardrail:** `just lint && just type && uv run python -m pytest tests/jobs -q` (+ the relevant
integration test in Task 7).

---

## Task 6 — Remove dead `reject_rootfs_without_upload_window`

**Fits:** ADR-0434 §6. Do after Task 3 (nothing depends on it).

**Do:**
- Delete `reject_rootfs_without_upload_window` from `provisioning.py` and its `__all__` entry.
- Delete its tests in `tests/providers/local_libvirt/test_rootfs_resolve.py`
  (`test_reject_rootfs_without_upload_window_*`) and the now-unused import.
- Grep-verify no production reference remains: `rg -n "reject_rootfs_without_upload_window"`
  returns nothing outside git history.

**Tests:** the grep guard is the check (AC12); full suite stays green.

**Guardrail:** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt -q`

---

## Task 7 — Staging integration test (close the "does NOT boot / staging deferred" gap)

**Fits:** AC14. Depends on Tasks 1–5.

**Do:**
- Extend `tests/integration/test_systems_define_upload_provision.py` (or add a sibling) to run
  `provision_handler` against a **real `LocalLibvirtProvisioning`** with fakes for every
  non-rootfs seam (libvirt `connect`/`defineXML`/`create`/`getCapabilities`; `ProvisioningFiles`
  with fake `make_overlay`/`resize`/baseline; fake `free_port`) and the **real** upload fetch
  over the test's `minio_store`. **Stage the rootfs object via `store.put_stream(
  ArtifactStreamRequest(..., sha256_b64=...))`, NOT `put_artifact`** — only `put_stream` signs a
  `ChecksumSHA256` that `head().checksum_sha256` reads back (see `tests/store/test_objectstore.py`
  `_sha256_b64` helper + `test_put_stream_*`). Write the bytes to a `tmp_path` file, compute
  `sha256_b64`, `put_stream` it; otherwise `head.checksum_sha256` is `None` and AC3 rejects.
- Assert: the object is staged under `rootfs-uploads`, `make_overlay` receives that path as base,
  the System reaches `ready`, then `teardown_handler` removes the staged file and reclaims the
  object+row.

**Guardrail:** `uv run python -m pytest tests/integration/test_systems_define_upload_provision.py -q`

---

## Final verification

- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
- `rg -n "reject_rootfs_without_upload_window"` → no production hits.
- Confirm parity guard unaffected: `uv run python -m pytest tests/providers/test_capability_parity.py -q`.
- Confirm the ADR-status guard: `just adr-status-check`.

## Rollback / cleanup

Pure additive feature + one dead-code removal; no migration, no data changes. Revert the branch
to roll back. The `rootfs-uploads` staging dir is created lazily and reclaimed at teardown; no
persistent host state is introduced beyond per-lease files that teardown removes.

## Out of scope (flag, do not build)

- Remote-libvirt supplied rootfs (#1433, parity-blocked — this change is its precondition).
- `artifact`-kind rootfs (still `not wired yet`).
- A CI-gated live boot proof (manual runbook only; see spec).
