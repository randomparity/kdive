# Implementation plan — fadump opt-in with a QEMU feature gate on pseries (#1151)

Spec: `docs/design/2026-07-14-ppc64le-fadump-1151.md` · ADR: `docs/adr/0349-ppc64le-fadump-opt-in.md`
Epic: #1139 · Depends on #1148 (merged PR#1169, ADR-0346)
Branch: `feat/fadump-pseries-opt-in-1151` · Base: `main`

**No migration, no schema change, no new config.** The host fadump signal rides the existing
JSON Resource-capabilities column (ADR-0338 precedent); the opt-in rides the stored profile.
`fadump=on` is never persisted on the System row — it is re-derived from the profile.

**Guardrails.** TDD throughout: write the failing test, then the code. Per task run `just lint`
(`ruff check` + `ruff format --check`), `just type` (whole-tree `ty`), and the relevant
`uv run python -m pytest <path> -q`; run the full `just ci` before push. Task 1 touches the
`runs.install` agent-facing surface only indirectly (no `Field`/docstring change), so
`just docs-check` is not expected to drift — but run it if any wrapper docstring changes.
The live proof (Task 8) is gated behind the `live_stack` marker and skips cleanly without the
host/fixtures, so it never reddens CI.

## Ground truth (verified this session, file:line)

- `CaptureMethod` enum: `src/kdive/domain/capture.py:8-12` — add `FADUMP = "fadump"`.
- Profile: `LibvirtDebugOptions` `src/kdive/profiles/provisioning.py:86-95` (`preserve_on_crash`,
  `gdbstub`); `LibvirtProfile.crashkernel` `:125`; `ProvisioningProfile` model validators
  `:289-312` (pattern for the new fadump validator).
- Capture-method derivation: `src/kdive/providers/local_libvirt/profile_policy.py:41-49`;
  sibling policy readers `host_dump_provisioned`/`gdbstub_provisioned` `:51-55`.
- Cmdline: `src/kdive/services/runs/steps.py:342-376` (`system_required_cmdline`), token
  allowlist `:38` (`_PLATFORM_OWNED_CMDLINE_TOKENS`).
- Discovery: `src/kdive/providers/local_libvirt/discovery.py:186-208` (capabilities dict);
  guest-arch parser `src/kdive/providers/shared/libvirt_xml.py:47-94`.
- Capability readers: `src/kdive/domain/catalog/resource_capabilities.py` — `GUEST_ARCHES_KEY`
  `:23`, `_KNOWN_KEYS` `:88-98`, `guest_arches()` reader `:175-198`.
- Admission: `src/kdive/services/systems/admission.py:256-272` (`_resolve_new_system_accel`),
  mint sites `:846-849`, `:880-884`; `resolve_accel` `src/kdive/services/systems/validation.py:50-72`.
- KDUMP consumption sites (share/diverge/add per spec §6): `local_libvirt/lifecycle/install.py:283,339`;
  `mcp/tools/lifecycle/vmcore/handlers.py:55,255`; `mcp/tools/lifecycle/runs/steps.py:142`;
  `jobs/handlers/runs/install.py:137`; `jobs/handlers/runs/boot_evidence.py:243`;
  `local_libvirt/composition.py:158`; `mcp/tools/catalog/resources.py:258-285`.
- Retrieve (unchanged): `src/kdive/providers/local_libvirt/retrieve.py:144-153` — `capture()`
  dispatch is `if HOST_DUMP … else <overlay harvest>`; FADUMP falls through.
- Doctor: `src/kdive/diagnostics/checks.py:24-29`; `src/kdive/diagnostics/multiarch_gdb.py`
  (probe + `diagnostic_contribution`); `src/kdive/providers/assembly/diagnostics.py`;
  `scripts/check-local-libvirt.sh:74-102`.
- Live scaffold reused: `fedora-kdive-ready-44-ppc64le` rootfs; bundle at
  `/home/dave/kdive-ppc-proof`; driver sibling
  `tests/integration/test_live_stack.py::test_ppc64le_kdump_captures_a_vmcore_under_tcg`.

## Tasks

### Task 1 — `CaptureMethod.FADUMP` + profile opt-in + capture-method derivation

**What / where:** Spec §1/§2, ADR-0349 §1/§2; criterion 1. Add the enum member, the
`debug.fadump` field, the parse-time self-consistency invariant, and the FADUMP branch in the
local capture-method derivation.

**Files:**
- `src/kdive/domain/capture.py` — add `FADUMP = "fadump"`.
- `src/kdive/profiles/provisioning.py` — `LibvirtDebugOptions.fadump: bool = False` (extend the
  class docstring: fadump adds `fadump=on` to the boot cmdline, POWER-only, requires a
  `crashkernel` reservation); a new `model_validator(mode="after")` on `ProvisioningProfile`
  (`_require_ppc64le_and_reservation_for_fadump`): when
  `provider.local_libvirt_section` is present and `.debug.fadump` is `True`, require
  `arch == "ppc64le"` and `.crashkernel is not None`, else `ValueError` (mapped to
  `CONFIGURATION_ERROR` by `parse`). Skip when there is no local section.
- `src/kdive/providers/local_libvirt/profile_policy.py` — nest the FADUMP branch under the
  crashkernel check in `capture_method` (`if crashkernel is not None: if debug.fadump: return
  FADUMP; return KDUMP`); add `fadump_provisioned(profile) -> bool` returning
  `section.debug.fadump`.

**Do (tests first):**
1. `tests/profiles/test_provisioning.py` (or the existing profile-parse test module): a
   ppc64le profile with `debug.fadump=True` + `crashkernel="512M"` parses; `debug.fadump=True`
   with `arch="x86_64"` → `CONFIGURATION_ERROR`; `debug.fadump=True` with `crashkernel=None`
   → `CONFIGURATION_ERROR`; a profile with `debug.fadump=False` (default) is unaffected.
2. `tests/providers/local_libvirt/test_profile_policy.py`: `capture_method` returns `FADUMP`
   for fadump+crashkernel, `KDUMP` for crashkernel-only, and `fadump_provisioned` reflects the
   flag. Assert the default (no debug) stays `CONSOLE`.
3. Implement to green.

**Acceptance (criterion 1):** `just lint`, `just type`, `just test` green. The parse invariant
and the derivation are arch-parameterized and cover the reject paths.

**Rollback:** revert the commit; self-contained (frozen-model field addition, no persistence).

### Task 2 — Boot cmdline: `fadump=on`

**What / where:** Spec §3, ADR-0349 §2; criterion 1. Emit the reservation for kdump *and*
fadump and append `fadump=on` for fadump only; guard the token.

**Files:** `src/kdive/services/runs/steps.py` — `system_required_cmdline`: change the
`if method is CaptureMethod.KDUMP:` branch to `if method in (CaptureMethod.KDUMP,
CaptureMethod.FADUMP):` emitting `crashkernel=…`, then `if method is CaptureMethod.FADUMP:
tokens.append("fadump=on")`. Add `"fadump="` to `_PLATFORM_OWNED_CMDLINE_TOKENS`. Update the
docstring to name the fadump token order (reservation then `fadump=on`, last).

**Do (tests first):**
1. `tests/services/runs/test_steps.py` (arch-parameterized): `system_required_cmdline(FADUMP,
   "root=/dev/vda", arch="ppc64le")` → `console=hvc0 root=/dev/vda crashkernel=512M fadump=on`
   (reservation then flag, `fadump=on` last); an explicit `crashkernel="1G"` →
   `… crashkernel=1G fadump=on`; the `KDUMP` and `GDBSTUB`/`CONSOLE` outputs are **byte-identical
   to today** (regression guard). `platform_owned_cmdline_token("… fadump=on")` returns
   `"fadump="`.
2. Implement to green.

**Acceptance (criterion 1):** `just lint`, `just type`, `just test` green; kdump/x86 cmdline
byte-identical.

**Rollback:** revert; pure composition function, no state.

### Task 3 — Discovery signal: `detect_pseries_fadump` + capability reader

**What / where:** Spec §4, ADR-0349 §3; criterion 3. Probe the ppc64le emulator's QEMU
version against the 10.2 floor at discovery; record it fail-closed; add the defensive reader.

**Files:**
- `src/kdive/domain/catalog/resource_capabilities.py` — `PSERIES_FADUMP_KEY = "pseries_fadump"`;
  add it to `_KNOWN_KEYS`; `ResourceCapabilities.pseries_fadump() -> bool` returning the value
  only when it is a `bool` (else `False`).
- `src/kdive/providers/local_libvirt/discovery.py` (or a small sibling module
  `providers/local_libvirt/fadump_probe.py` for unit isolation) — `detect_pseries_fadump(
  guest_arches, *, run_version=<subprocess seam>) -> bool`: read
  `guest_arches.get("ppc64le", {}).get("emulator")`; if absent → `False`; run
  `<emulator> --version`, parse the first `QEMU emulator version <maj>.<min>...` line, return
  `(maj, min) >= (10, 2)`; any failure (missing binary, non-zero exit, unparseable) → `False`.
  Define `PSERIES_FADUMP_QEMU_FLOOR = (10, 2)` as a named constant. Wire the key into the
  `list_resources` capabilities dict: `PSERIES_FADUMP_KEY: detect_pseries_fadump(guest_arches)`.

**Do (tests first):**
1. `tests/domain/catalog/test_resource_capabilities.py`: `pseries_fadump()` returns `True`
   for `{"pseries_fadump": True}`, `False` for `False`/absent/`"yes"`/non-bool.
2. `tests/providers/local_libvirt/test_fadump_probe.py`: `detect_pseries_fadump` with a faked
   `run_version` returns `True` for `"QEMU emulator version 10.2.2 (…)"`, `True` for `10.3.0`,
   `False` for `9.2.1`, `False` for `10.1.0`; `False` when no ppc64le emulator; `False` when
   the seam raises `FileNotFoundError`/`CalledProcessError` or returns garbage.
3. Implement to green. Keep the subprocess call bounded (short timeout) and swallow all
   exceptions into `False` (fail-closed).

**Acceptance (criterion 3):** `just lint`, `just type`, `just test` green; the probe is
fail-closed on every error path; no live libvirt/subprocess in the unit tests (seam faked).

**Rollback:** revert; the capability key simply stops being written (readers default `False`).

### Task 4 — Admission host gate

**What / where:** Spec §5, ADR-0349 §3; criterion 2. Reject a fadump-opted provision against
a Resource that does not advertise `pseries_fadump`, at mint, before the granted→active flip.

**Files:** `src/kdive/services/systems/admission.py` — `_validate_fadump_supported(conn,
resource_id, profile, profile_policy)`: if not `profile_policy.fadump_provisioned(profile)` →
return (no-op); if `resource_id is None` → raise `CONFIGURATION_ERROR` (a fadump System needs a
bound resource to gate against); load the Resource, and if
`resource.capability_view.pseries_fadump()` is `False` → raise `CONFIGURATION_ERROR` naming the
QEMU 10.2 floor, the host, and a "re-run discovery if you recently upgraded QEMU" hint. Call it
at the same two mint points the accel resolution uses (`_insert_defined_system`,
`_insert_provisioning_system`), inside the existing `try/except CategorizedError ->
_failure_from_error` wrap so a rejection consumes no capacity. It is a method on
`SystemAdmission` (or takes `profile_policy` explicitly): it reads `self.profile_policy`
(the admission service already holds it) and the parsed profile being admitted — **not** the
accel-resolution path, which threads only `arch`.

**Do (tests first):**
1. `tests/services/systems/test_admission.py` (or the admission unit module): a fadump-opted
   profile against a `capability_view` with `pseries_fadump=True` admits (reaches the flip);
   against `pseries_fadump=False`/absent → `CONFIGURATION_ERROR`, **no** state transition and
   **no** capacity debit (assert the granted allocation is untouched); a non-fadump profile is
   unaffected regardless of the resource signal; `resource_id=None` + fadump → `CONFIGURATION_ERROR`.
2. Implement to green.

**Acceptance (criterion 2):** `just lint`, `just type`, `just test` green; the rejection is
pre-flip, capacity-neutral, and returns a typed envelope (asserted via `_failure_from_error`
path), never a hang.

**Rollback:** revert; admission stops gating fadump (the parse-time invariant still holds).

### Task 5 — KDUMP-site share/diverge/add audit (the enum-member safety)

**What / where:** Spec §6, ADR-0349 §4. Apply the audited decision at every remaining
in-scope `CaptureMethod.KDUMP` site so FADUMP is handled consciously.

**Files & edits (each is one hunk):**
- `src/kdive/providers/local_libvirt/lifecycle/install.py:283` — `request.method in (KDUMP,
  FADUMP)` (kdump env absent check); `:339` — `request.method in (KDUMP, FADUMP) or
  debuginfo_ref is not None` (needs_modules).
- `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py:54-56` — add `FADUMP` to `_VMCORE_METHODS`;
  `:255` — `capture_method in (KDUMP, FADUMP)` (ADR-0318 gate) — **kept kdump-symbol-only**, no
  `CONFIG_FA_DUMP` check (spec §8a / ADR-0349 §5: the runtime signal, not a static config
  check, is the fadump safeguard).
- `src/kdive/mcp/tools/lifecycle/runs/steps.py:142` — `method not in (KDUMP, FADUMP)`
  (reject-crashkernel-on-non-kdump).
- `src/kdive/jobs/handlers/runs/install.py:137` — `method not in (KDUMP, FADUMP)`
  (crashkernel backstop).
- `src/kdive/jobs/handlers/runs/boot_evidence.py:243-244` — resolve once
  (`resolved = capture_method(profile)`); `if resolved in (KDUMP, FADUMP):
  methods.append(resolved.value)` so a fadump System reports `"fadump"` as an inert method.
- `src/kdive/providers/local_libvirt/composition.py:158` — add `CaptureMethod.FADUMP` to the
  `capture_methods` frozenset.
- `src/kdive/mcp/tools/catalog/resources.py` — **only** `_VMCORE_METHODS` in the vmcore
  handler (above); `supported_capture_methods` in `_project_capabilities` (`:258-259`) picks up
  `"fadump"` automatically from the runtime support set (Task 5 composition edit), no change
  here. **No per-host `pseries_fadump` surfacing in `resources.get`** — verified this session:
  `_project_capabilities(:246-266)` receives only `runtime` (the provider descriptor), not the
  Resource row, so the per-host signal (on the Resource capabilities column) cannot be surfaced
  there without threading resource state into a descriptor-only projection (a layer violation),
  and it is **not an AC** (AC 3 requires the `pseries_fadump()` reader + the doctor signal, not
  the `resources.get` description). The host fadump signal is exposed to agents/operators via
  the doctor check (Task 7) and enforced at admission (Task 4); a provision that would not be
  admitted fails fast there with a clear category. Descoped deliberately.
- **Untouched (assert in tests, do not edit):** `providers/local_libvirt/retrieve.py`
  (`capture()` falls through), `remote_libvirt/*`, `fault_inject/*`, `observability/labels.py`,
  `console/capture_telemetry.py`.

**Do (tests first):**
1. Extend each site's existing unit test with a FADUMP case asserting the shared/diverged/added
   behavior: install fires module injection + kdump-env check for FADUMP; `_VMCORE_METHODS`
   admits FADUMP (`vmcore.fetch` resolves it for a fadump System — see Task 6); a crashkernel
   override is **accepted** on a FADUMP System (both guard sites); `inert_capture` returns
   `"fadump"`; the local runtime's `capture_methods` contains FADUMP.
2. A retrieve test asserting `LocalLibvirtRetrieve.capture(..., FADUMP)` takes the overlay
   harvest path (not host_dump) and stores under `vmcore-fadump` (fake the `wait_for_vmcore`
   seam; assert the stored `name` suffix).
3. Implement to green.

**Acceptance:** `just lint`, `just type`, `just test` green. Every in-scope KDUMP site has an
explicit FADUMP test; the untouched sites are asserted unchanged (remote/fault-inject
capture-method sets do **not** contain FADUMP).

**Rollback:** revert; each hunk is independent and additive.

### Task 6 — `vmcore.fetch` resolves FADUMP end-to-end + resource-description surfacing

**What / where:** Spec §5/§6; criterion 4. Confirm the omitted-method resolution path admits a
fadump System's core and the resource description shows the per-host signal.

**Files:** covered by Tasks 3/5 edits (`_VMCORE_METHODS`, support set, `pseries_fadump()`
reader, resource description). This task is the integration test binding them.

**Do (tests first):**
1. `tests/mcp/tools/lifecycle/vmcore/test_handlers.py`: for a `CRASHED` fadump System (profile
   resolves `capture_method → FADUMP`, runtime support contains FADUMP), `vmcore.fetch` with an
   **omitted** method resolves FADUMP and enqueues `CaptureVmcorePayload(method=FADUMP)`; an
   explicit `method="fadump"` is accepted; the ADR-0318 gate fires (kdump-symbol refusal path)
   for FADUMP the same as KDUMP.
2. `tests/mcp/tools/catalog/test_resources.py`: `supported_capture_methods` lists `"fadump"`
   for the local runtime (from the Task 5 support-set edit). No per-host `pseries_fadump` field
   in `resources.get` (descoped — see Task 5).
3. Implement any missing wiring to green.

**Acceptance (criterion 4):** `just lint`, `just type`, `just test` green; a fadump System's
`vmcore.fetch` resolves and enqueues FADUMP; `supported_capture_methods` lists fadump.

**Rollback:** revert; falls back to Task 5 state.

### Task 7 — Doctor check + shell advisory

**What / where:** Spec §7, ADR-0349 §3; criterion 3. A worker-vantage diagnostic reporting the
host fadump signal, reusing the Task 3 probe.

**Files:**
- `src/kdive/diagnostics/checks.py` — `PSERIES_FADUMP_ID = "pseries_fadump"`.
- `src/kdive/diagnostics/pseries_fadump_check.py` (new, mirroring `multiarch_gdb.py`) — a
  worker-vantage `Check`/probe reusing `detect_pseries_fadump`: `PASS` when a ppc64le emulator
  reports QEMU ≥10.2; `FAIL`/`MISSING_DEPENDENCY` with a fix hint (upgrade QEMU to ≥10.2, or
  fadump is native-POWER-only on this host) when a ppc64le emulator is below the floor;
  undeterminable (no `CheckResult` failure — informational) when there is no ppc64le emulator
  (fadump is simply N/A on an x86-only host). A `diagnostic_contribution()` registered via
  `src/kdive/providers/assembly/diagnostics.py` (`local_diagnostics()`).
- `scripts/check-local-libvirt.sh` — a matching advisory line near the per-arch qemu probe
  (`:74-102`): if `qemu-system-ppc64` is present, note whether its version clears the fadump
  floor (report-only, `note_warn`/`note_pass`). Keep it shellcheck/shfmt-clean.

**Do (tests first):**
1. `tests/diagnostics/test_pseries_fadump_check.py`: `PASS` for a faked ≥10.2 ppc64le emulator,
   `FAIL` with the fix hint for <10.2, informational/skip for no ppc64le emulator. Fake the
   probe seam (no subprocess).
2. Implement to green. `shellcheck scripts/check-local-libvirt.sh` + `shfmt -i 2 -d` clean.

**Acceptance (criterion 3):** `just lint`, `just type`, `just test`, `just lint-shell` green;
the doctor check reports the signal; the shell advisory is report-only and clean.

**Rollback:** revert; the check contribution is additive (removing it drops the signal).

### Task 8 — Live proof (attempt first; documented-verdict fallback) — BLOCKING AC

**What / where:** Spec §8/§8a, ADR-0349 §5; criterion 5. Attempt a real fadump capture under
TCG; prove the *mechanism* (not just the outcome); or document the native-POWER verdict.

**Step 0 — confirm-first preconditions (named, established before capture):**
- `CONFIG_FA_DUMP=y` in the baseline/uploaded ppc64le kernel — grep the Fedora ppc64le kernel
  config (`/boot/config-<ver>` in the rootfs, or the packaged config). **If unset, `fadump=on`
  is silently ignored and the kernel kdump-falls-back** — close the rootfs/kernel gap first (or
  record it as the blocking finding), do not run an indeterminate capture.
- The #1148 kdump-enabled `fedora-kdive-ready-44-ppc64le` rootfs (fadump reuses the kdump
  userspace) and ≥2 GB guest RAM — reuse the #1148 fixture; record both met.

**Files:**
- `tests/integration/test_live_stack.py` — a new `live_stack`-marked driver
  `test_ppc64le_fadump_captures_a_vmcore_under_tcg`, mirroring the #1148 kdump driver's
  structure (skip cleanly without `qemu-system-ppc64` / `KDIVE_GUEST_IMAGE_PPC64LE` and the
  bundle). Provision with a **fadump profile** (`crashkernel="512M"` + `debug.fadump=True`),
  install the bundle, read the fadump `enabled`/`registered` sysfs values (probe the modern
  `/sys/kernel/fadump/{enabled,registered}` directory first, fall back to the legacy flat
  `/sys/kernel/fadump_{enabled,registered}` — the path is kernel-version-dependent) and
  `/sys/kernel/kexec_crash_loaded` **pre-crash** over the guest SSH forward, `control.force_crash`,
  harvest via `vmcore.fetch`/`vmcore.list`.
- `docs/design/2026-07-14-ppc64le-fadump-proof-record-1151.md` (new) — the proof record.

**Do — the run:**
1. Provision → install → assert the running domain `<cmdline>` carries `fadump=on` and the
   guest `/proc/cmdline` shows `crashkernel=512M fadump=on`.
2. **Discriminating mechanism check (the finding-1 safeguard):** assert fadump `enabled==1`
   and `registered==1` (whichever sysfs interface the kernel exposes) **and**
   `kexec_crash_loaded==0` on the pre-crash guest — fadump is active and registered, and the
   kdump kexec path is *not* loaded, ruling out a silent kdump fallback that would otherwise
   masquerade as a fadump success (`registered==1` is primary; `kexec_crash_loaded==1`
   alongside it is a misconfiguration to record).
3. `force_crash` → harvest → assert a non-empty `EM_PPC64` core under the `vmcore-fadump` key,
   record makedumpfile fields. Record the domain-settle behavior (fadump reboot-to-production
   vs poweroff) and whether the core was written either way.

**Do — the record:** Write the proof record with the console evidence, the pre-crash fadump
sysfs signals, the cmdline, the `EM_PPC64`/makedumpfile fields, and the domain-settle finding.
Then update ADR-0349 with a "Live-proof outcome (date)" section (mirroring ADR-0346), recording
PASS (real fadump capture) **or** the documented verdict (QEMU 10.2 floor + native-POWER
validation deferred to #1152).

**Feasibility-gate rule (per issue):** fadump-under-TCG **may** legitimately prove unusable.
If, after honest iteration, the mechanism check cannot pass (fadump never registers, or the
capture cannot complete), ship the **documented verdict** — not a false-positive that a
kdump-fallback capture would produce. A capture that only passes the *outcome* check
(`vmcore-fadump` present) but fails the *mechanism* check (`fadump_registered==0`) is a
**kdump fallback, recorded as such**, not a fadump PASS.

**Acceptance (criterion 5):** the `live_stack` test passes on the dev host (mechanism +
outcome) **or** the proof record + ADR document the native-POWER verdict with the QEMU floor.
The test skips cleanly without the harness, so it cannot redden CI.

**Rollback:** the `live_stack` test skips without the harness; the proof record/ADR verdict is
documentation. No production code depends on Task 8.

## Task ordering & prerequisites

`1 → 2 → 3 → 4 → 5 → 6 → 7 → 8`.

- Tasks 1–2 (enum + opt-in + cmdline) are the foundation; every later task references `FADUMP`.
- Task 3 (probe + reader) is a prerequisite for Task 4 (admission gate reads
  `pseries_fadump()`) and Task 7 (doctor reuses `detect_pseries_fadump`).
- Task 5 (the KDUMP-site audit) depends on Task 1's enum; Task 6 integration-tests Tasks 3+5.
- Tasks 1–7 are context-free, implementer-ready, CI-green unit/service work — **no migration,
  schema, or config change**. They can be built and merged independently of the host.
- Task 8 is host-bound (the dev host runs KVM/libvirt + `qemu-system-ppc64` 10.2.2 with the
  fadump RTAS) and blocking for the AC; it lands last, after 1–7 are green, reusing the #1148
  live scaffold. Its single biggest risk is the mechanism check (finding 1): a capture that
  succeeds via silent kdump-fallback must be recorded as a fallback, not a fadump PASS.

## Out of scope (spec §Scope)

No migration/persistence; no remote-libvirt/fault-inject fadump; no new reservation/arch_traits
field; no POWER10-native validation (#1152); no `live_vm_tcg` marker (epic issue 15); no
big-endian ppc64; no fadump-specific dracut/rootfs tooling.
