# Implementation plan — debuginfo advice + install-path performance (#1350)

Spec: [design](../../specs/2026-07-20-debuginfo-advice-install-perf-1350-design.md)
ADR: [0399](../../adr/0399-single-pass-kernel-bundle-and-scratch-staging.md)
Branch: `feat/debuginfo-advice-single-pass-tar-1350` · Base: `main`

Guardrails (run before every commit): `just lint` · `just type` (whole-tree) ·
`just test`. Full gate before push: `just ci`. Config docs after a Setting change:
`just config-docs`. Single test:
`uv run python -m pytest <path>::<name> -q`.

TDD throughout: write the failing test first, then the code. Each task is
independently committable and leaves guardrails green.

---

## Task 1 — Rewrite the `debuginfo` feature advice

**Fits:** Part 1 of the spec — make the agent-facing advice name when/cost.

**Files:** `src/kdive/kernel_config/requirements.py`;
`tests/kernel_config/test_requirements.py` (or the existing manifest test — locate
with `rg -l feature_manifest tests/`).

**Steps:**
1. Test first: assert the `debuginfo` entry from `feature_manifest()` has a
   `summary` naming both the use case (contains "drgn" or "vmcore") and the cost
   (contains ".ko" and a growth factor such as "10" / "50" or "module tree"), and
   that it still advises omission for console-log/boot-time reproducers
   (contains "omit" case-insensitively). Assert the `requirements`/`gated` fields
   are unchanged (still advisory, `gated=false`).
2. Rewrite the `debuginfo` `summary` string to the spec's text. Keep it one
   string literal; do not touch `advertised`/`gate_required`.
3. Confirm no other test pins the old summary substring (`rg "Resolve symbols for"
   tests/`).

**Acceptance:** the new test passes; `just test` green; no doc-guard violation
(keep the summary plain and factual per the project prose rule).

---

## Task 2 — Single-pass `extract_kernel_bundle` (replace the two-pass functions)

**Fits:** Part 2a — one decompression instead of two.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py`;
`tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py`;
`src/kdive/providers/local_libvirt/lifecycle/install.py` (imports, `__all__`,
call sites); check `tests/providers/local_libvirt/test_install.py` and
`tests/integration/test_live_stack.py` for references to the old names
(`rg -n "extract_boot_vmlinuz|repack_modules_subtree"`).

**Design of the new function:**
```
def extract_kernel_bundle(
    combined_tar: Path, kernel_dest: Path, modules_dest: Path | None
) -> bool:
    """One decompression pass over the combined kernel tar.

    Always extracts the first ``boot/vmlinuz`` member to ``kernel_dest`` (verbatim,
    arch-opaque, temp-then-rename). When ``modules_dest`` is given, repacks the
    ``lib/modules/`` subtree into it and returns whether a subtree was found;
    returns False when ``modules_dest`` is None.
    """
```
- One `with tarfile.open(combined_tar, "r:gz") as archive:` and, when
  `modules_dest` is set, a nested `tarfile.open(modules_tmp, "w:gz")`.
- Single `for member in capped_tar_members(archive)` loop:
  - first `boot/vmlinuz` match (normalized via `_tar_member_path`): call
    `reject_oversize_member(member.size, dest=str(kernel_dest))`, read via
    `archive.extractfile(member)`, hold the bytes.
  - else when an output tar is open and the normalized name starts with
    `_MODULES_MEMBER_PREFIX`: skip `..`-containing members; accumulate
    `member.size if member.isfile()`; `reject_oversize_member(total, ...)`;
    `out.addfile(member.replace(name=normalized), archive.extractfile(member) if
    member.isfile() else None)`; mark found.
- Consume each member fully before the loop advances (the pattern
  `repack_modules_subtree` already uses under `r:gz` — proven single forward pass,
  no backward seek).
- After the loop: if boot bytes are None → raise `INFRASTRUCTURE_FAILURE`
  ("no boot/vmlinuz member"); else `write_staged_bytes(kernel_dest, data)`. If
  modules found → `modules_tmp.replace(modules_dest)`, else unlink the tmp.
- Error contract, unchanged categories: wrap `(OSError, tarfile.TarError)` →
  `INFRASTRUCTURE_FAILURE`; let `CategorizedError` (oversize/member-count) escape
  after cleaning the `.part` tmp. Keep helper functions small so the orchestrator
  stays ≤100 lines / complexity ≤8 — factor the boot branch and the modules
  branch into small module-level helpers or a private scan dataclass if needed.
- Remove `extract_boot_vmlinuz` and `repack_modules_subtree`; update `install.py`
  imports and `__all__` (`extract_kernel_bundle` replaces both entries).

**Tests (port the existing cases to the merged function, keep parametrization):**
1. boot member round-trips byte-identically (x86_64 + ppc64le, incl. `./`-prefixed),
   with `modules_dest=None` — asserts no modules tar is written.
2. with `modules_dest` set: modules subtree present → returns True and the tar
   contains `lib/modules/<ver>/...`; `_read_release` still returns the version.
3. boot-only tar with `modules_dest` set → returns False, no modules tar left.
4. missing boot member → `INFRASTRUCTURE_FAILURE`.
5. corrupt (non-gzip) tar → `INFRASTRUCTURE_FAILURE`.
6. member-count bomb → `CONFIGURATION_ERROR` (monkeypatch `MAX_KERNEL_TAR_MEMBERS`).
7. oversize boot member → `CONFIGURATION_ERROR`.
8. oversize cumulative module tree → `CONFIGURATION_ERROR`, `.part` cleaned.
9. **single-open assertion:** wrap/patch `tarfile.open` (or inject an open
   counter) and assert the combined tar is opened exactly once for a
   modules-needed install.

**Acceptance:** new tests pass; `just test` green; `just type` green;
`rg extract_boot_vmlinuz|repack_modules_subtree src tests` returns nothing.

---

## Task 3 — Wire the single-pass call into the install flow

**Fits:** Part 2a call-site — `_stage_install_artifacts` uses the merged function.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/install.py`;
`tests/providers/local_libvirt/test_install.py`.

**Steps:**
1. In `_stage_install_artifacts`, compute
   `needs_modules = request.method in KDUMP_FAMILY or request.debuginfo_ref is not
   None` up front, call
   `extract_kernel_bundle(combined_tar, kernel_path, modules_tar if needs_modules
   else None)`, and inject when it returns True (fold `_inject_modules_if_needed`
   into this flow or keep it as a thin wrapper that no longer re-opens the tar).
2. Preserve the `modules_injected` bool feeding the kdump-env-absent check and
   `_StagedInstallArtifacts`.
3. Keep `_delete_install_intermediates` for the combined + modules tars (Task 4
   extends it for `vmlinux`).

**Acceptance:** existing `test_install.py` behavior tests pass unchanged (kdump
env-absent, modules-injected, debuginfo path); `just test` green. No change to the
libvirt redefine / XML path.

---

## Task 4 — Configurable scratch staging (`KDIVE_INSTALL_SCRATCH`)

**Fits:** Part 2c — route transient intermediates to an opt-in scratch root.

**Files:** `src/kdive/config/core_settings.py` (new Setting, register in the
export tuple); `src/kdive/providers/local_libvirt/lifecycle/install.py`
(`from_env`, `LocalLibvirtInstaller` scratch_root wiring, `_stage_install_artifacts`,
`_inject_built_modules`, `_delete_install_intermediates`,
`_unwritable_staging_error` sibling for scratch); `tests/providers/local_libvirt/test_install.py`;
config-docs snapshot (regenerated).

**Steps:**
1. Add `INSTALL_SCRATCH = Setting(name="KDIVE_INSTALL_SCRATCH", parse=_str,
   group="install", processes=_WORKER, help=...)` with **no default** and help
   text that: (a) says it defaults to the `KDIVE_INSTALL_STAGING` root when unset;
   (b) states it holds only transient intermediates; (c) states the tmpfs/RAM
   tradeoff and the deferred streaming follow-up (paired memory story). Register it
   in the settings export tuple near `INSTALL_STAGING`.
2. In `from_env`, resolve `scratch_root = Path(config.get(INSTALL_SCRATCH) or
   str(staging_root))` (confirm the optional-get API — `config.get` vs a
   `default`; if the framework needs a default, default to the staging value at
   resolve time, never a hardcoded path). Thread `scratch_root` into
   `LocalLibvirtInstaller`.
3. `_stage_install_artifacts`: derive `scratch_dir = scratch_root / system_id /
   run_id`; mkdir it with the same error contract as staging (add a
   `_unwritable_scratch_error` naming `KDIVE_INSTALL_SCRATCH`, or parameterize the
   existing helper). Route `combined_tar`, `modules_tar` to `scratch_dir`; keep
   `kernel_path`, `initrd_path` in `staging_dir`. When `scratch_dir == staging_dir`
   (default) the mkdir is idempotent — no double-create fault.
4. `_inject_built_modules`: fetch `vmlinux` into `scratch_dir` (passed in), not the
   staging dir.
5. Extend `_delete_install_intermediates` to also unlink the `vmlinux` intermediate
   (best-effort, `missing_ok=True`).
6. `just config-docs` to regenerate the generated setting docs; commit the snapshot.

**Tests:**
- default (unset): monkeypatch/patch config so scratch resolves to staging;
  intermediates written under staging dir; behavior unchanged.
- set to a distinct tmp dir: `kernel`/`initrd` under staging; `kernel.tar.gz`/
  `modules.tar.gz`/`vmlinux` under scratch and removed after use.
- unwritable scratch root → `CONFIGURATION_ERROR` naming `KDIVE_INSTALL_SCRATCH`
  (mirror the existing staging permission test, monkeypatch `mkdir` to raise
  `PermissionError`).

**Acceptance:** `just test` green; `just type` green; `just config-docs` leaves no
uncommitted drift; the new setting appears in the generated docs.

---

## Task 5 — Operator docs + follow-up issues

**Fits:** documentation of the memory tradeoff; capture deferred 2b.

**Files:** the install/staging operator doc (locate with
`rg -l INSTALL_STAGING docs/`), e.g. `docs/operating/…`; CHANGELOG is
bot-regenerated — do **not** hand-edit `[Unreleased]`.

**Steps:**
1. Document `KDIVE_INSTALL_SCRATCH`: what it holds, the default, and the
   tmpfs/RAM sizing guidance (host RAM vs. worker concurrency; the ~4 GB resident
   worst case on a 2 GB tar), cross-referencing the deferred streaming work.
2. File a follow-up issue for **2b streaming fetch-and-extract** (rework
   `store.get_artifact` to stream the S3 body into the tar extractor; own ADR).
   Reference #1350 and ADR-0399.

**Acceptance:** doc guards pass; follow-up issue filed and linked.

---

## Ordering & rollback

- Order: 1 → 2 → 3 → 4 → 5. Task 3 depends on 2 (the merged function must exist);
  Task 4 builds on 3's flow. Tasks 1 and 5's doc bits are independent but cheap to
  keep last-but-one.
- Each task is a standalone commit; reverting any one leaves a coherent tree
  (Task 1 is pure advice; Tasks 2–3 are a behavior-preserving refactor with the
  single-open test as the guard; Task 4 is default-off).
- No DB migration, no schema change, no external-service contract change.
