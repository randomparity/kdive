# Implementation plan — Verify drgn vmcore analysis for ppc64le (#1150)

- **Spec:** `docs/design/2026-07-14-drgn-vmcore-ppc64le-1150.md`
- **ADR:** `docs/adr/0348-drgn-vmcore-ppc64le-verification.md`
- **Branch:** `feat/drgn-vmcore-ppc64le-1150` (off `origin/main`)
- **Guardrails:** `just ci` (lint, `type` whole-tree, lint-shell, lint-workflows, check-mermaid,
  test). Single test: `uv run python -m pytest <path>::<name> -q`. Live: `just test-live`.
- **Language:** Python 3.14, `uv`-managed. Read `~/.claude/languages/python.md` before editing.

## Design summary (what and why)

The offline vmcore-analysis path (`shared/debug_common/drgn_program.py::open_vmcore_program`
→ `DrgnProgramAdapter` → `local_libvirt/debug/introspect.py::LocalLibvirtVmcoreIntrospect.
from_vmcore` + the remote mirror) is **already arch-opaque**: drgn reads the target arch from
the core's ELF header + DWARF, and the only arch-observable value in the contract is
`sysinfo.machine`. **No production change.** The deliverable is verification:

1. arch-parameterized unit tests proving the orchestration is arch-blind (the fakes cannot
   prove real DWARF decoding — that is deferred, see spec AC1b);
2. a `live_vm`-gated real-bytes test proving drgn opens the retained real #1148 ppc64le core
   and identifies it as ppc64le (durable within the live suite, not CI);
3. a catalog-row-hygiene assertion for the ppc64le `drgn_version`;
4. a proof record for the live run.

**Live facts already confirmed on-host (2026-07-14, drgn 0.2.0):**
- Retained core: `/home/dave/kdive-ppc-proof/vmcore-kdump-ppc64le` (from #1148 run
  `9359253e-017a-4740-bb2a-3f008bae520c`, MinIO key
  `local/runs/9359253e…/vmcore-kdump`, 90463884 bytes).
- SHA-256: `bd322c68c540542484cde32df94d3e074874374a1eb2ca50551e808f4c7190fa`.
- `prog.platform.arch == drgn.Architecture.PPC64` → True;
  `prog.platform.flags == PlatformFlags.IS_64_BIT|IS_LITTLE_ENDIAN`.
- VMCOREINFO `BUILD-ID` = `06466f9617cff9e5a762af9216bfc23837310b9c`.
- No DWARF `vmlinux` available → full structural read (task list) DEFERRED (AC1b).

## Tasks

### Task 1 — Confirm production code needs no change (audit gate; TDD anchor)

**What:** Assert-by-search that the offline path carries no `x86_64` literal that would make a
ppc64le core misbehave. This is the ADR-0348 audit made executable; it produces no code change
but pins the "no production change" decision.

**Where:** `src/kdive/providers/shared/debug_common/drgn_program.py`,
`src/kdive/providers/shared/debug_common/core_file.py`,
`src/kdive/providers/shared/debug_common/introspect.py`,
`src/kdive/providers/local_libvirt/debug/introspect.py`,
`src/kdive/providers/remote_libvirt/debug/introspect.py`.

**Steps:** `rg -n 'x86_64|EM_X86|amd64' <those files>`. Expect zero arch-branching hits
(the only match should be none, or a comment). If a real arch assumption is found, STOP —
the "no production change" premise is falsified; fix it and record in ADR-0348.

**Acceptance:** No arch-branch found in the offline path; documented in the PR body.

**Rollback:** N/A (read-only).

### Task 2 — Arch knob on the local-libvirt offline fakes + parameterized contract tests

**Where:** `tests/providers/local_libvirt/test_introspect_drgn.py`.

**What & how (TDD):**
- Add `arch: str = "x86_64"` to `_FakeProgram.__init__`; in the default `uts` dict use
  `"machine": arch`. The default keeps every existing assertion byte-identical (guard against
  a default-arg regression).
- Parameterize the sysinfo contract test over `("x86_64", "ppc64le")`: rename/duplicate
  `test_sysinfo_returns_uts_and_counters` into a parameterized form asserting
  `out["machine"] == arch` and that the other fields (release/boot_cmdline/cpus/mem) are
  unchanged across arches. Keep an x86_64 case byte-identical to the current assertions.
- Parameterize the offline `from_vmcore` happy path over `("x86_64", "ppc64le")`: assert the
  **four-section contract shape** (tasks/modules/sysinfo/truncated), same keys, with
  `out.sysinfo["machine"] == arch`. Reuse `_introspector(program=_FakeProgram(arch=arch))`.

**Why:** Proves the offline orchestration is arch-blind — the ADR audit made falsifiable.
These fakes do **not** prove real DWARF decoding (see spec AC1b); do not claim they do.

**Acceptance:** `uv run python -m pytest tests/providers/local_libvirt/test_introspect_drgn.py -q`
green; the ppc64le params exercise `machine == "ppc64le"`; the x86_64 assertions are unchanged.

**Guardrails:** `just lint`, `just type`, that test file.

### Task 3 — Arch-parameterize the shared debug_common drgn tests

**Where:** `tests/providers/debug_common/test_drgn_program.py`.

**What & how:** The module-level `_FakeProgram.uts()` hardcodes `machine="x86_64"`, and
`_ProgramFromLists` already accepts a `uts` dict. Add a parameterized test over
`("x86_64", "ppc64le")` that drives `run_introspection_helper(prog, "sysinfo")` (or
`helper_sysinfo`) with a `_ProgramFromLists(uts={..., "machine": arch})` and asserts
`out["machine"] == arch` and the fixed dispatch (`tasks`/`modules`) is unaffected. Keep the
existing `test_helper_sysinfo_maps_uts_and_counters` x86_64 assertion intact (or fold it into
the x86_64 param, byte-identical).

**Acceptance:** `uv run python -m pytest tests/providers/debug_common/test_drgn_program.py -q`
green; ppc64le sysinfo round-trips `machine`.

### Task 4 — Arch-parameterize the remote-libvirt offline mirror

**Where:** `tests/providers/remote_libvirt/debug/test_introspect.py`.

**What & how:** The offline `from_vmcore` path is mirrored here. Add a parameterized offline
sysinfo test over `("x86_64", "ppc64le")` using `_ProgramFromLists(uts={"machine": arch, ...})`
via `helper_sysinfo`, asserting `machine == arch` and the same contract shape. (The live-agent
`introspect_live` tests are the in-guest/SSH path — out of scope; do not touch them.)

**Acceptance:** `uv run python -m pytest tests/providers/remote_libvirt/debug/test_introspect.py -q`
green; ppc64le offline sysinfo mirrors x86_64.

### Task 5 — Catalog `drgn_version` row-hygiene assertion for the ppc64le row

**Where:** `tests/images/test_rootfs_catalog.py`.

**What & how:** Add a test that loads the catalog, takes the `fedora-kdive-ready-44-ppc64le`
row, and asserts its `drgn_version` **parses via `DrgnVersion.parse`**
(`kdive.images.drgn_support`) into a real version that **clears `BTF_CAPABLE_DRGN` (0.0.31)** —
catching a placeholder/empty/malformed value. Frame it in a comment as *catalog-row hygiene of
the guest-baked drgn*, explicitly **not** offline-path proof, and **not** a cross-arch equality
pin (Fedora secondary-arch may diverge). Leave the existing snapshot test
(`test_catalog_drgn_versions_match_snapshot`) unchanged.

**Acceptance:** `uv run python -m pytest tests/images/test_rootfs_catalog.py -q` green.

### Task 6 — `live_vm`-gated real-bytes ppc64le open test (durable guard)

**Where:** new module `tests/providers/local_libvirt/test_introspect_ppc64le_live.py` (or an
existing `live_vm` provider module).

**What & how:**
- Module constants `_PINNED_SHA256 = "bd322c68c540542484cde32df94d3e074874374a1eb2ca50551e808f4c7190fa"`  <!-- pragma: allowlist secret (vmcore digest, not a credential) -->
  and `_PINNED_SIZE = 90463884` — **these are the authoritative pins** (spec Risks). The size
  matches #1148's own recorded core size (birth-record corroboration).
- `@pytest.mark.live_vm`. Read `os.environ.get("KDIVE_PPC64LE_VMCORE")`.
  - env **unset** → `pytest.skip("KDIVE_PPC64LE_VMCORE unset; set it to the retained #1148 ppc64le core — see docs/design/2026-07-14-drgn-vmcore-ppc64le-proof-record-1150.md")`.
  - env **set but file missing/unreadable** → **fail** (`pytest.fail`/assert), not skip.
  - assert file size == `_PINNED_SIZE` (a truncated core is caught even if the digest were
    mis-pinned), then compute the file SHA-256; if it != `_PINNED_SHA256` → **fail** with the
    two-case message: *"unexpected digest: if you just re-captured the core, recompute and update
    `_PINNED_SHA256`/`_PINNED_SIZE`; otherwise the core at this path is swapped or corrupt."*
- `import drgn`; `prog = drgn.Program(); prog.set_core_dump(path)`; assert
  `prog.platform.arch == drgn.Architecture.PPC64` **and**
  `drgn.PlatformFlags.IS_LITTLE_ENDIAN in prog.platform.flags`.
- Read VMCOREINFO and assert its `BUILD-ID=` line reads via
  `read_vmcoreinfo_build_id(bytes(prog["VMCOREINFO"].value_()))` (reuse the production helper);
  assert it equals `06466f9617cff9e5a762af9216bfc23837310b9c`.

**Why:** The only check that exercises drgn's *real* ppc64le decoding; the durable regression
guard the fakes cannot be. Fail-loud/skip-only-when-unset per spec AC1a.

**Verify live now:** run `KDIVE_PPC64LE_VMCORE=/home/dave/kdive-ppc-proof/vmcore-kdump-ppc64le
uv run python -m pytest tests/providers/local_libvirt/test_introspect_ppc64le_live.py -q -m live_vm`
→ must pass. Also run it with the env **unset** → must **skip** (not fail), and with the env
pointing at a missing path → must **fail**. Record all three in the proof record.

**Acceptance:** live run passes; unset→skip; set-but-missing→fail; `just lint`/`just type` clean
on the new module.

### Task 7 — Proof record

**Where:** `docs/design/2026-07-14-drgn-vmcore-ppc64le-proof-record-1150.md`.

**What:** Record the live AC1a run: host (this x86_64 dev host), drgn version (0.2.0), core
retained path + SHA-256 (`bd322c68…`) + size (`90463884` bytes, matches #1148's record) +
source #1148 run id, the asserted values
(`Architecture.PPC64`, `IS_LITTLE_ENDIAN`, build-id `06466f96…`), the three-way skip/fail/pass
behavior observed, the **re-run-on-drgn-bump trigger**, the **DWARF-deferred (AC1b)
known-limitation** with the follow-up (obtain ppc64le `kernel-debuginfo`, drive `from_vmcore`),
and the re-pin runbook. Link it from the spec.

**Acceptance:** `just docs-links` + `just check-mermaid` clean; the record states PASS with the
recorded values.

### Task 8 — Full guardrail suite + commit hygiene

**What:** Run `just ci`. Fix every warning/failure. Commit in small logical units
(one task ≈ one commit) with conventional-commit subjects ≤72 chars and the `Co-Authored-By`
trailer. Stage explicit paths (never `git add -A`). Do not commit the retained core or the
scratch findings file.

**Acceptance:** `just ci` green; working tree clean except intended files.

## Ordering & prerequisites

Tasks 2–5 are independent unit-test edits (any order). Task 6 depends on the retained core
existing (already downloaded) and can run in parallel with 2–5. Task 7 depends on Task 6's live
run. Task 8 is last. Task 1 is a gate that must pass before claiming "no production change."

## Rollback / cleanup

All changes are additive tests + docs; revert the commits to roll back. The retained core lives
outside the repo (`/home/dave/kdive-ppc-proof/`) and is never committed. No migration, no schema,
no dependency, no production-code change.
