# Plan — drgn-live reads a staged DWARF vmlinux (#3121, part 1)

Goal: make every kdive surface say that drgn-live reads the Run's uploaded DWARF `vmlinux`, not
kernel BTF ([spec](../specs/2026-10-06-drgn-live-dwarf-truth-design.md),
[ADR-0723](../../adr/0723-drgn-live-reads-a-staged-dwarf-vmlinux.md)).

Architecture: a pure capability rule (`drgn_support`), a static warning (`kernel_config.gate`), a
runtime probe shared by `introspect.*` and `debug.start_session`, the in-guest `kdive-drgn`
helper, and the live-stack spine. No schema or migration change. The external-build-contract
resource loses `bpf_tracing.also_checked`, its only instance.

Tech stack: Python 3.14, pytest, Bash 4.4+ helper, Markdown docs.

Expected implementation size: 250–350 changed lines (M) — nine product files, about fifty test
assertions in ten test files, the helper, and the docs in the file map below.

## Global Constraints

- The `live_drgn` keys (`drgn_version`, `capability`, `min_drgn_required`, `note`, `basis`) and
  values (`capable | incapable | unverified | not_applicable`) do not change.
- Warning reasons stay `missing_debuginfo` and `debuginfo_unloadable`, with the
  `{reason, missing, remediation}` shape.
- `docs/guide/toolsets/*.md` and `docs/operating/external-build-upload.md` stay byte-identical
  to their `src/kdive/mcp/resources/_content/` copies. `docs/guide/reference/*.md` is generated:
  change the source docstring, run `just docs` (or `just config-docs`), never hand-edit.
- Gates: `just lint`, `just type`, `just records`, `just docs-links`, `just docs-check`. Red and
  green runs use `uv run pytest` on the working tree; `just test-linux HEAD <paths>` tests a
  commit, so run it only after committing. The pre-push hook runs `just ci`.

## File map

| File | Today | After |
|---|---|---|
| `src/kdive/images/drgn_support.py` | BTF floor 0.0.31 | no floor; DWARF-upload note |
| `src/kdive/images/cataloging/capability_signals.py`, `src/kdive/images/rootfs/catalog.py:59`, `fixtures/local-libvirt/rootfs_catalog.toml:22-23,56,237` | text says BTF floor | text says uploaded `vmlinux` |
| `src/kdive/mcp/tools/catalog/images.py:245,403` → generated `docs/guide/reference/images.md` | `capable` means BTF | `capable` means drgn can read the uploaded `vmlinux` |
| `src/kdive/kernel_config/gate.py` | warns on missing BTF | warns on missing `vmlinux` upload |
| `src/kdive/kernel_config/requirements.py` | BTF in `debuginfo` summary, `bpf_tracing.also_checked` | `vmlinux` in summary; no `also_checked` |
| `src/kdive/mcp/tools/debug/introspection/{gate,live}.py`, `.../sessions/lifecycle.py` | probe skipped on upload | probe whenever static is silent |
| `deploy/remote-libvirt-guest-helpers/{kdive-drgn,README.md}` | `-s /sys/kernel/btf/vmlinux` | `drgn -k` only |
| `src/kdive/config/external_env.py`, `docs/guide/reference/config.md` | `KDIVE_BTF_PATH` | removed |
| `tests/integration/live_stack/spine.py`, `tests/integration/test_live_stack.py` | BTF required, no upload | DWARF required, `with_vmlinux=True` |
| `docs/guide/toolsets/{introspect,images}.md`, `docs/operating/external-build-upload.md` (+ `_content` copies), `docs/operating/runbooks/live-testing.md:836` | BTF claims, `also_checked` text | ADR-0723 wording |
| ADR-0322/0328/0329/0335/0548 | Accepted | superseded / amended banners |

## Task 1 — `live_drgn` without the BTF floor

Files: `src/kdive/images/drgn_support.py`, `src/kdive/images/cataloging/capability_signals.py`;
`src/kdive/images/rootfs/catalog.py:59`, `fixtures/local-libvirt/rootfs_catalog.toml` comments;
tests `tests/images/test_drgn_support.py`, `tests/images/test_capability_signals.py:169-172`,
`tests/images/test_rootfs_catalog.py:205-240`, `tests/mcp/catalog/test_images_describe.py:308-342`.

Interfaces: `live_drgn_capability(*, drgn_version: str | None, drgn_tooling: bool) ->
LiveDrgnCapability` (unchanged signature). `BTF_CAPABLE_DRGN` is deleted; no other module imports
it (`rg -n BTF_CAPABLE_DRGN src tests`).

Verification:
- Mode: focused-test. Contract: a parsed version is `capable` with `min_drgn_required is None`.
  Red: `test_drgn_support.py` asserts `"0.0.22"` gives `capable` and fails on today's `incapable`.
  Green: `uv run pytest tests/images/test_drgn_support.py tests/images/test_capability_signals.py
  tests/images/test_rootfs_catalog.py -q` passes.

Steps:
1. In `test_drgn_support.py`, replace the threshold tests with: `"0.0.22"`, `"0.0.31"` and
   `"0.2.0"` each give `status == "capable"`, `min_drgn_required is None`, and `"vmlinux"` in
   `note`. Keep the `not_applicable` and `unverified` tests. Run it; expect failures.
2. In `drgn_support.py`, delete `BTF_CAPABLE_DRGN`, rewrite the module docstring to the ADR-0723
   rule, and replace the version branch:

   ```python
   _VMLINUX_NOTE = (
       "drgn-live reads the Run's uploaded DWARF vmlinux staged in the guest; upload the "
       "kernel's matching vmlinux with the build (drgn does not read kernel BTF)"
   )
   ...
   try:
       DrgnVersion.parse(drgn_version)
   except ValueError:
       ...  # unchanged unverified branch
   return LiveDrgnCapability(
       status="capable", drgn_version=drgn_version, min_drgn_required=None, note=_VMLINUX_NOTE
   )
   ```

3. Update `test_capability_signals.py:172` to `block["min_drgn_required"] is None`, and the
   catalog test: delete `_LIVE_DRGN_BELOW_THRESHOLD_ROWS`, expect `capable` for every row with a
   version, and drop "BTF floor" from both docstrings. Rewrite the `render_live_drgn_signal`
   docstring, `catalog.py:59` and the BTF-threshold comments in `rootfs_catalog.toml`. In
   `test_images_describe.py:308-342`, the 0.0.22 row now expects `capable` and a null
   `min_drgn_required`. Run the green command.
4. Commit `feat(images): drop the BTF floor from the live_drgn signal (#3121)`, then run
   `just test-linux HEAD tests/mcp/catalog/test_images_describe.py`; expect pass.

## Task 2 — warnings and the always-on probe

Files: `src/kdive/kernel_config/gate.py`, `src/kdive/kernel_config/requirements.py`,
`src/kdive/mcp/tools/debug/introspection/gate.py`, `.../introspection/live.py:136-152`,
`.../sessions/lifecycle.py:404-416,433-446,504-518`; tests `tests/kernel_config/test_gate.py:70-140`,
`tests/kernel_config/test_requirements.py:255,1817-1838,1841,1871-1872,1942-1943`,
`tests/mcp/catalog/test_external_build_contract_resource.py:377,389,403`,
`tests/mcp/debug/test_introspect_tools.py:1296`, `tests/mcp/debug/test_debug_tools.py:1284`.

Interfaces: `debuginfo_warning(conn, run_id, *, has_uploaded_vmlinux: bool)` keeps its signature.
`augment_with_runtime_probe(static_warning, *, introspector, transport_handle, private_key)` loses
`has_uploaded_vmlinux`. `_runtime_probe(run, system, transport, static_warning, introspector)` keeps
its signature but no longer reads `run.debuginfo_ref`.

Verification:
- Mode: focused-test. Contract: static warning names `vmlinux` and ignores BTF. Red: new
  `test_external_build_without_vmlinux_warns_and_names_vmlinux` (BTF config, no upload, expects
  `missing == ["vmlinux"]`) fails today. Green: `uv run pytest tests/kernel_config -q`.
- Mode: focused-test. Contract: the probe runs with an uploaded `vmlinux`. Red: rename
  `test_run_live_uploaded_vmlinux_skips_runtime_probe` to `..._runs_runtime_probe` and
  `test_start_session_drgn_live_uploaded_vmlinux_skips_runtime_probe` likewise, asserting the fake
  introspector saw `RESOLUTION_PROBE_SCRIPT` and a failed probe yields `debuginfo_unloadable`.
  Green (after the step 5 commit): `just test-linux HEAD tests/mcp/debug/test_introspect_tools.py
  tests/mcp/debug/test_debug_tools.py`.
- Mode: focused-test. Contract: `bpf_tracing` has no `also_checked`; the `debuginfo` summary names
  the uploaded `vmlinux`. Red/green: rewrite `test_requirements.py:255,1841`; delete the
  `_BTF_SYMBOL` pin at `:1817-1838` and the `also_checked` unpacks at `:1871-1872,1942-1943`
  (assert instead that no entry carries `also_checked`); update
  `test_external_build_contract_resource.py:377,389,403`; `uv run pytest tests/kernel_config
  tests/mcp/catalog/test_external_build_contract_resource.py -q`.

Steps:
1. Rewrite the gate tests at `test_gate.py:74-140`: BTF-only, DWARF-only and empty configs with no
   upload each warn with `missing == ["vmlinux"]`; any config with an upload is `None`; an absent
   config is `None`; the unloadable payload names `vmlinux`. Run; expect failures.
2. In `gate.py`, delete `_BTF_SYMBOL` and its comments and set:

   ```python
   _DEBUGINFO_REMEDIATION = (
       "upload the kernel's matching vmlinux (built with CONFIG_DEBUG_INFO_DWARF5, "
       "CONFIG_DEBUG_INFO_DWARF4 or CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT) with the build; "
       "drgn-live reads it from /usr/lib/debug/lib/modules/<release>/vmlinux in the guest and "
       f"does not read kernel BTF (see {_EXTERNAL_BUILD_CONTRACT_URI})"
   )
   _DEBUGINFO_UNLOADABLE_REMEDIATION = (
       "the in-guest drgn could not resolve kernel symbols: no matching DWARF vmlinux is "
       "readable at /usr/lib/debug/lib/modules/<release>/vmlinux; upload the matching vmlinux "
       f"and use an install path that stages it (see {_EXTERNAL_BUILD_CONTRACT_URI})"
   )
   ```

   In `debuginfo_warning`, keep the upload and absent-config early returns, drop the BTF test,
   and return `missing: ["vmlinux"]`. Set `missing: ["vmlinux"]` in
   `debuginfo_unloadable_warning`. Update both docstrings to ADR-0723.
3. In `introspection/gate.py`, remove the `has_uploaded_vmlinux` parameter; the guard becomes
   `if static_warning is not None: return static_warning`. Remove the argument at `live.py:150`
   and `lifecycle.py:415`, and the `run.debuginfo_ref is not None` clause at `lifecycle.py:516`.
   Update the three docstrings and the comment at `live.py:137-140`.
4. In `requirements.py`, rewrite the `debuginfo` summary sentence that sends drgn-live to BTF so
   it says drgn-live reads the uploaded `vmlinux`. Delete `bpf_tracing`'s `also_checked` block and
   the comment above it, and the `bpf_tracing` sentences that say kdive reads `DEBUG_INFO_BTF`.
   Keep `_BTF_CLAUSE` only if `bpf_tracing` still references it.
5. Update the requirement and resource tests; run the `uv run pytest` green commands and
   `just type`. Commit `fix(debug): key drgn-live warnings on the vmlinux upload, probe always
   (#3121)`.
6. Run the `just test-linux HEAD` green command; fix forward in a new commit if it fails.

## Task 3 — helper without `-s`

Files: `deploy/remote-libvirt-guest-helpers/kdive-drgn:15-42,173`, its `README.md:61-65`,
`src/kdive/config/external_env.py:984-990`, `docs/guide/reference/config.md`;
test `tests/scripts/test_kdive_drgn_helper.py`.

Verification:
- Mode: focused-test. Contract: every mode runs `drgn -k -q` with no `-s`. Red: replace the BTF
  tests with `test_helper_never_passes_symbols_flag` (fake `drgn` records argv for `run-script`
  and `sysinfo`; asserts `argv=-k -q` and no `-s`), which fails today when a fake BTF file
  exists. Green: `uv run pytest tests/scripts/test_kdive_drgn_helper.py -q`.

Steps:
1. Write the new test with `KDIVE_BTF_PATH` still pointing at a fake BTF file, so the unchanged
   helper passes `-s` on any host; run; expect red.
2. Remove the `btf_present`/`KDIVE_BTF_PATH` fixture code. In the helper, delete `btf_path` and
   the `-s` block; set `drgn_args=(-k)`; rewrite the header
   comment: drgn's default search reads `/usr/lib/debug/lib/modules/$(uname -r)/vmlinux`, which
   the install path stages (ADR-0221, ADR-0723). Update the README paragraph.
3. Delete the `KDIVE_BTF_PATH` entry, run `just config-docs`, then the green command and
   `just lint`. Commit `fix(helpers): let kdive-drgn use drgn's default debug-info search (#3121)`.

## Task 4 — spine preflight and live-script upload

Files: `tests/integration/live_stack/spine.py:590-594`, `tests/integration/test_live_stack.py:733-768`;
tests `tests/integration/live_stack/test_spine.py:131-146,180`.

Verification:
- Mode: focused-test. Contract: `require_live_debug` needs a DWARF member and not BTF. Red: rename
  the test to `test_spine_config_live_debug_requires_dwarf_not_btf`; a DWARF5-only config must
  pass and fails today. Green: `uv run pytest tests/integration/live_stack/test_spine.py -q`.
- Mode: task-test-not-applicable. Surface: `with_vmlinux=True` in the live-script test. Reason:
  only a live stack can observe the upload; Task 5 runs it.

Steps: update both tests (`:180` matches `CONFIG_DEBUG_INFO_BTF=y` today and must match the DWARF
message); delete the `DEBUG_INFO_BTF` check in `spine.py`; add `with_vmlinux=True` to the
`build_and_upload_kernel` call at `test_live_stack.py:733`, and assert that the attach and
`introspect.script` responses carry no `missing_debuginfo` key in `data`; run green; commit
`test(live-stack): upload vmlinux for the live-script spine (#3121)`.

## Task 5 — guest proof, docs, ADR banners, live spine

Files: `src/kdive/mcp/tools/catalog/images.py:245,403`; generated `docs/guide/reference/images.md`;
`docs/guide/toolsets/{introspect,images}.md`, `docs/operating/external-build-upload.md` and their
`_content` copies; `docs/operating/runbooks/live-testing.md:836`; ADR-0723; status lines on
ADR-0322 (`- **Status:** Superseded by [ADR-0723](...)`) and an `> **Amended by [ADR-0723](...)**
(#3121): ...` line under the header of ADR-0328, 0329, 0335 and 0548.

Verification:
- Mode: focused-test. Contract: served copies match and generated pages are current. Green:
  `uv run pytest tests/mcp/resources -q`, then `just docs-check`, `just docs-links` and
  `just records`, each exit 0.
- Mode: focused-test. Contract: spec Success 4. Green: `rg -n -i 'KDIVE_BTF_PATH|reads BTF|in-guest
  BTF' src deploy docs/guide docs/operating` prints nothing (exit 1).
- Mode: task-test-not-applicable. Surface: spec Success 5 and 6. Reason: they need a native KVM
  guest; steps 1 and 5 record the observed results.

Steps:
1. Success 5 first, on the native POWER host: boot a guest whose image has the old helper, copy
   the Run's `vmlinux` to `/usr/lib/debug/lib/modules/$(uname -r)/vmlinux` over SSH, and run
   `echo "print(prog['init_uts_ns'].name.release)" | /usr/local/sbin/kdive-drgn run-script 30`.
   Expect the release string and exit 0. If it fails, stop and return to scope: images with the
   old helper would need a rebuild.
2. Write the observed result (image, drgn version, kernel release, exit code, output) into the
   last Consequences item of ADR-0723 in place of "is added to this item before this record is
   accepted".
3. Change the two `images.py` docstrings to the ADR-0723 rule and run `just docs`. Replace each
   BTF claim in the toolset pages, `live-testing.md:836`, and the `also_checked` sentences at
   `external-build-upload.md:224-225`; copy each page to `_content` and `cmp` the pair.
4. Add the banners. ADR-0723 stays `Proposed`; the merging PR sets `Accepted (<date>)`.
5. Success 6 on a native x86_64 local-libvirt host whose install uses the legacy path: confirm
   `KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE` is unset in the worker environment and record that
   check, then run `uv run pytest
   tests/integration/test_live_stack.py::test_spine_live_script_over_the_wire -m live_stack`.
   Expect `1 passed`, 0 skipped. Record the build stamps and the probe latency on #3121.
6. Commit `docs: state that drgn-live reads the uploaded vmlinux (#3121)`.

Deferrals: #3123, #3124, #3125, #3122, #2763 (spec Out of scope).
