# Implementation plan — direct_kernel capability signal (#954)

**Spec:** [../specs/2026-07-02-direct-kernel-provisionable-signal-954.md](../specs/2026-07-02-direct-kernel-provisionable-signal-954.md)
**ADR:** [../../adr/0295-direct-kernel-provisionable-signal.md](../../adr/0295-direct-kernel-provisionable-signal.md)

Execution mode: tasks are tightly coupled (one signal, shared test files, one build path),
implemented directly in-session with TDD (failing test → minimal impl → guardrails green). Every
task runs `just lint && just type` and its focused `pytest` before commit; the full suite + `just
docs-check` run once before push.

Guardrail commands (CI gates these individually):
`just lint`, `just type`, `just test`, `just docs-check` (after `just docs`), `just adr-status-check`.

## Task 1 — Extract the baseline-kernel classifier (anti-drift)

**Where it fits:** the recorded `boot_kernel_count` must classify kernels with the *same* rule
`select_kernel_and_initrd` uses at provision, or the signal could say `provisionable` while
provision fails. One shared pure function guarantees agreement.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/baseline_kernel.py`,
`tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py` (or the existing baseline-kernel
test module).

**Do:**
- Add `baseline_kernel_names(boot_entries: list[str]) -> list[str]`: `os.path.basename` each entry,
  keep those starting with `vmlinuz-` and not containing `rescue`. Google-style docstring naming
  that both provision selection and the build count classify with it.
- Rewrite `select_kernel_and_initrd` to call `baseline_kernel_names` for its `kernels` list; keep
  the existing `names` local for the initrd lookup and the error `details`. No behavior change.

**TDD:** first a test that `baseline_kernel_names` accepts both `/boot/vmlinuz-6.11.0` full paths
and bare `vmlinuz-6.11.0` basenames, drops `vmlinuz-0-rescue-*` and non-`vmlinuz` entries, and that
its length equals what `select_kernel_and_initrd` accepts (1) / rejects (0, ≥2) on the same input.

**Acceptance:** existing `select_kernel_and_initrd` tests stay green; new classifier test passes;
`len(baseline_kernel_names(x)) == 1` iff `select_kernel_and_initrd(x)` succeeds.

## Task 2 — Capture `boot_kernel_count` at build time

**Where it fits:** the operand. Advisory capture beside `makedumpfile_version` /
`package_versions`; any failure omits the key so a degraded build's row is byte-identical to a
pre-feature one.

**Files:** `src/kdive/images/planes/_build_common.py`,
`src/kdive/providers/local_libvirt/rootfs_build.py`,
`tests/providers/local_libvirt/test_rootfs_build.py` (mirror the makedumpfile-capture tests).

**Do:**
- `_build_common.py`: add `type BootEntriesProbeSeam = Callable[[Path], list[str] | None]` and
  `probe_boot_entries(qcow2_path) -> list[str] | None` — read-only `guestfish --ro -a <qcow2> -i
  ls /boot`, splitlines/strip, mirroring `probe_makedumpfile_marker`'s error mapping
  (`MISSING_DEPENDENCY` when guestfish absent, `INFRASTRUCTURE_FAILURE` on timeout). Export
  `DEFAULT_BOOT_ENTRIES_PROBE`. Mark the real function `# pragma: no cover - live_vm`.
- `rootfs_build.py`: add `probe_boot_entries: BootEntriesProbeSeam = DEFAULT_BOOT_ENTRIES_PROBE`
  to `RootfsBuildTools`; add `_capture_boot_kernel_count(scratch) -> int | None` that calls the
  seam (catch `CategorizedError` → `None`), returns `None` when the seam returns `None`, else
  `len(baseline_kernel_names(entries))`. Call it in `build()` and thread the result into
  `_provenance` as `boot_kernel_count`, writing `record["boot_kernel_count"] = count` **only when
  `count is not None`** (an `is not None` test — a `0` is recorded).
- `probe_boot_entries` must map an absent guestfish to `MISSING_DEPENDENCY`, a timeout to
  `INFRASTRUCTURE_FAILURE`, **and any non-zero exit (e.g. a non-qcow2 path) to a `CategorizedError`
  too** — never a bare exception — so a stray real invocation is always caught by
  `_capture_boot_kernel_count` and degrades to `None` rather than failing a build.

**Required test-harness edits (regression flags — same commit):**
- `tests/providers/local_libvirt/test_rootfs_build.py`: the shared `_tools(rec, ...)` helper
  currently stubs only `inspect_versions`/`probe_makedumpfile`. The new `probe_boot_entries` field
  otherwise defaults to the **real guestfish seam**, so every `build()`-driving test would shell
  out on the fake `b"scratch"` bytes (non-hermetic; this host has libguestfs). Add a
  `probe_boot_entries` parameter to `_tools`, defaulting to a hermetic stub `_no_boot_entries`
  returning `None` (so the default build path omits the key and stays hermetic), and pass explicit
  stubs in the new positive-path tests.
- `test_provenance_source_digest_for_virt_builder_entry` (asserts an **exact** `out.provenance`
  dict, ~line 261) is a named required edit: with the default `_no_boot_entries` stub it stays
  green **because the key is omitted** — keep that assertion as the omitted-path guard (do not add
  `boot_kernel_count` to it), and cover the recorded-key path in the dedicated new tests below.

**TDD (unit, injected seam — no libguestfs):**
- seam yields two non-rescue kernels → `provenance["boot_kernel_count"] == 2`.
- seam yields one → `1`; seam yields `["vmlinuz-6.11.0", "vmlinuz-0-rescue-abc"]` → `1`.
- seam yields `[]` (or only a rescue kernel) → `0` and the key **is present**.
- seam raising `CategorizedError` → key absent (`"boot_kernel_count" not in provenance`).
- default `_no_boot_entries` (returns `None`) → key absent (the omitted-path default the
  exact-dict test relies on).

**Acceptance:** the five cases above pass; `test_provenance_source_digest_for_virt_builder_entry`
stays green with the key omitted (its exact-dict assertion unchanged); no unit test invokes real
guestfish.

**Rollback/cleanup:** pure additive; no migration, no persisted state beyond the JSONB field.

## Task 3 — Register the `direct_kernel` signal + surface it

**Where it fits:** the computed answer an agent reads.

**Files:** `src/kdive/images/capability_signals.py`,
`src/kdive/mcp/tools/catalog/images.py`, `tests/images/test_capability_signals.py`,
`tests/mcp/catalog/test_images_describe.py`.

**Do:**
- `capability_signals.py`: add `render_direct_kernel_signal(entry, target_kernel) ->
  dict[str, JsonValue]` (kernel-agnostic — ignores `target_kernel`). Read
  `entry.provenance.get("boot_kernel_count")`; treat non-`int`/`bool` as absent
  (`isinstance(raw, int) and not isinstance(raw, bool)`). Return
  `{"boot_kernel_count": <int|None>, "status": <str>, "note": <str>}` per the spec table
  (`unverified` absent, `provisionable` for 1, `not_provisionable` for 0/`>1`). Notes carry no ADR
  reference. Add `DIRECT_KERNEL_SIGNAL = CapabilitySignal("direct_kernel", ("boot_kernel_count",),
  render_direct_kernel_signal)`; append to `REGISTERED_SIGNALS`. Remove the
  `direct_kernel_bootable` entry from `PLANNED_SIGNALS`. Add `render_direct_kernel_signal` /
  `DIRECT_KERNEL_SIGNAL` to `__all__`.
- `images.py`: update the `_capability_signals` / `_describe_envelope` docstrings and the
  `images_describe` wrapper docstring to name `direct_kernel` alongside `kdump` (drop "today only
  `kdump`"). No logic change (the iteration already renders it).
- Run `just docs` to regenerate `docs/guide/reference/images.md`.

**TDD:**
- `test_capability_signals.py`: `render_direct_kernel_signal` on `boot_kernel_count: 1` →
  `provisionable`; `2` → `not_provisionable`; `0` → `not_provisionable`; absent → `unverified`
  with `boot_kernel_count is None`; `True` (bool) → `unverified` (not treated as 1). The existing
  guard `test_registered_signals_have_names_and_operands` now covers `direct_kernel`; the existing
  `test_planned_disjoint_from_registered_and_not_capabilities` stays green.
- `test_images_describe.py`: describe a row with `provenance={"boot_kernel_count": 1}` →
  `data.capability_signals["direct_kernel"].status == "provisionable"`, block keys ==
  `{"boot_kernel_count","status","note"}`; a row with no provenance → `direct_kernel` present and
  `unverified`.

**Acceptance:** `direct_kernel` block appears in `images.describe`; all guard tests green;
`just docs-check` clean after `just docs`.

## Verification gate (before push)

Full `just lint && just type && just test && just docs-check && just adr-status-check` all green;
then adversarial-review the branch diff (step 6) and security-review before opening the PR.
