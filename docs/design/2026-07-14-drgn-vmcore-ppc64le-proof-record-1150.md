# Proof record — drgn opens a ppc64le vmcore (#1150)

Date: 2026-07-14
Issue: #1150 · Epic: #1139 · Spec: `2026-07-14-drgn-vmcore-ppc64le-1150.md` · ADR-0348

> **Status: PASS (2026-07-14).** drgn opened the real #1148 ppc64le vmcore on the x86_64 host and
> identified it as ppc64le specifically (`Architecture.PPC64` + little-endian), and its VMCOREINFO
> `BUILD-ID=` note read. #1150 acceptance criterion AC1a is met. The full structural read (task
> list / by-name symbols from real DWARF) is deferred — AC1b — see Known limitation below.

## Environment

- Host: x86_64 dev host (this repo's live host), drgn **0.2.0** in the worker venv.
- Core: the real ppc64le vmcore captured by **#1148** (PR#1169) under TCG, from Run
  `9359253e-017a-4740-bb2a-3f008bae520c`. Object-store key
  `local/runs/9359253e-017a-4740-bb2a-3f008bae520c/vmcore-kdump` (bucket `kdive-artifacts`).
- Retained at: `/home/dave/kdive-ppc-proof/vmcore-kdump-ppc64le`.
- **SHA-256: `bd322c68c540542484cde32df94d3e074874374a1eb2ca50551e808f4c7190fa`**  <!-- pragma: allowlist secret (vmcore digest) -->
- **Size: 90463884 bytes** — matches #1148's own recorded captured-core size
  (`docs/design/2026-07-13-ppc64le-kdump-proof-record-1148.md`), corroborating the pin against
  the artifact's birth record.
- Driver: `tests/providers/local_libvirt/test_introspect_ppc64le_live.py::
  test_ppc64le_vmcore_opens_and_is_identified_as_ppc64le` (`@pytest.mark.live_vm`), run with
  `KDIVE_PPC64LE_VMCORE=/home/dave/kdive-ppc-proof/vmcore-kdump-ppc64le`.

## Result — a ppc64le vmcore opens and drgn identifies it as ppc64le (PASS)

Asserted against real bytes, no debuginfo loaded:

- `prog.platform.arch == drgn.Architecture.PPC64` → **True**.
- `drgn.PlatformFlags.IS_LITTLE_ENDIAN in prog.platform.flags` → **True**
  (`prog.platform.flags == PlatformFlags.IS_64_BIT|IS_LITTLE_ENDIAN`). The arch enum has no
  LE/BE variant, so this flag is what discriminates ppc64le from big-endian ppc64 (out of scope).
- `read_vmcoreinfo_build_id(bytes(prog["VMCOREINFO"].value_()))` →
  **`06466f9617cff9e5a762af9216bfc23837310b9c`** — the production helper (which raises on absence)
  returned rather than raising, confirming the core carries a parseable `BUILD-ID=` note.
- File size == `90463884` and SHA-256 == the pin above (both asserted before the open), so the
  guard ran against this exact #1148 artifact.

The three-way skip/fail/pass discipline was verified on 2026-07-14:

| condition                             | behavior |
|---------------------------------------|----------|
| `KDIVE_PPC64LE_VMCORE` unset          | **skip** (actionable message) |
| env set, file missing                 | **fail** loudly (not skip) |
| env set → the retained core           | **pass** (assertions above) |

## Durability & re-proof

- **Live-suite, not CI.** The 86 MiB core cannot ship to CI, so this guard runs under
  `just test-live` on the host holding the retained core. A green PR does **not** assert AC1a.
- **Re-run-on-drgn-bump trigger.** When the worker/live-host drgn version changes, re-run this
  `live_vm` test against the retained core; a drgn that regresses real ppc64le-core opening fails
  it loudly. Last verified: drgn **0.2.0**.
- **Re-pin on re-capture.** The pinned digest/size live authoritatively in the test constants
  (`_PINNED_SHA256` / `_PINNED_SIZE`); this record holds a human-readable copy. If the core is
  lost, re-capture via the #1148 `live_stack` test
  (`test_ppc64le_kdump_captures_a_vmcore_under_tcg`), recompute the SHA-256 **and** size, and
  update both the test constants and this record in one commit. The digest-mismatch failure
  message distinguishes "just re-captured — re-pin" from "core swapped/corrupt".

## Known limitation — full structural read deferred (AC1b)

Reading the **task list** and **by-name symbols** out of a real ppc64le core requires a
DWARF-bearing `vmlinux`. The epic ships only stripped `vmlinuz` boot images and no ppc64le
`kernel-debuginfo` (a Fedora *secondary*-arch package), so the structural decode on real ppc64le
bytes is **not proven here** — the same real-DWARF scope ADR-0344 put out of bounds. The
arch-parameterized unit tests
(`test_introspect_drgn.py`, `test_drgn_program.py`, remote `test_introspect.py`) prove the offline
**orchestration** is arch-blind (the adapter uses only arch-general drgn helpers per ADR-0348),
which is what CI can guard; they are **not** claimed as proof of real DWARF decoding. Follow-up:
obtain ppc64le `kernel-debuginfo` and drive `LocalLibvirtVmcoreIntrospect.from_vmcore` end-to-end
(task list + `machine="ppc64le"` sysinfo) on the real core. The debuginfo prerequisite is itself
arch-neutral — the x86_64 offline path needs the identical `load_debug_info`.
