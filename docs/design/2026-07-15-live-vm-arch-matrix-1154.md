# A `live_vm_tcg` test tier and a discovery-driven guest-arch gate (#1154)

Date: 2026-07-15
Status: approved (design)
Issue: #1154 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0353-live-vm-tcg-tier.md`
Depends on: #1148 (kdump on ppc64le — per-arch crashkernel defaults + capture proof, ADR-0346, merged) — CLOSED

## Problem

The epic (`2026-07-13-ppc64le-full-support.md` §Diagnostics, docs, tests) calls for a
guest-arch dimension in the live-VM tests, with foreign-arch (TCG-emulated) runs held to a
**separate marker** so the native suite stays fast: a ppc64le guest under TCG boots an order
of magnitude slower than a native KVM guest, so mixing the two tiers into one marker would
make the fast native tier unusable.

Three ppc64le TCG proofs from earlier sub-issues already exist, but only as **one-off,
manually-selected runs**:

- #1144 — `test_ppc64le_guest_is_ssh_reachable_over_the_wire`: provision→boot a Fedora
  ppc64le guest under TCG and prove SSH reachability.
- #1146 — `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`: install and
  direct-kernel-boot an *uploaded* ppc64le kernel bundle on pseries.
- #1148 — `test_ppc64le_kdump_captures_a_vmcore_under_tcg`: force-crash → kdump capture →
  retrieve a ppc64le vmcore under TCG.

A fourth, #1151 `test_ppc64le_fadump_captures_a_vmcore_under_tcg`, is the same class of
emulated-arch spine proof. All four live in `tests/integration/test_live_stack.py` and carry
only `@pytest.mark.live_stack`. There is no way to select "the emulated foreign-arch spine"
as a repeatable tier, and each gates the emulator with an ad-hoc `shutil.which(
"qemu-system-ppc64")` string that duplicates the authoritative qemu-binary map added for the
operator diagnostic in #1153 (`kdive.diagnostics.guest_arch_accel.qemu_system_binary`).

## Constraints and ground truth

- **Two distinct live-test vehicles, not interchangeable.** `live_vm`-marked tests drive
  provider ports directly (`LocalLibvirtControl.from_env()`, …) against a System the operator
  has *already* provisioned; they do not allocate/provision/boot. `live_stack`-marked tests
  drive the full MCP HTTP transport and are the repo's **only** end-to-end
  provision→boot→crash→retrieve spine. A foreign-arch provision→boot→crash proof therefore
  can only be a `live_stack` test — there is no stack-free path that provisions.
- **`SUPPORTED_ARCHES` / the qemu-binary map are single-sourced.** `qemu_system_binary(arch)`
  (`diagnostics/guest_arch_accel.py`, ADR-0352) maps each supported arch to its system
  emulator (`x86_64→qemu-system-x86_64`, `ppc64le→qemu-system-ppc64`; note the asymmetry:
  POWER has no `-ppc64le` binary). The test gate must reuse it, not re-declare it.
- **`just test-live` runtime is an acceptance invariant.** It must not gain any TCG test.

## Goal

- A `live_vm_tcg` pytest marker, registered in `pyproject.toml`, tagging emulated
  foreign-arch guest proofs.
- The four ppc64le TCG spine proofs selectable as one repeatable tier under that marker.
- A discovery-driven skip gate `require_guest_arch(arch)` that reuses the #1153 qemu-binary
  map, skips cleanly when the host cannot boot the arch, and returns the resolved accelerator.
- A `just test-live-tcg` recipe running that tier; `just test-live` stays native-only.
- The three tiers documented in AGENTS.md and a live-VM test-tier operator note.

## Design

### The marker is an orthogonal tier tag, not a new vehicle

`live_vm_tcg` does not replace or fork the `live_stack` vehicle. The four proofs keep
`@pytest.mark.live_stack` (the vehicle that actually provisions) and **add**
`@pytest.mark.live_vm_tcg` (the tier tag meaning "this proof boots an emulated foreign-arch
guest"). Selection is then orthogonal:

| Recipe | Selector | Collects |
|--------|----------|----------|
| `just test-live` | `-m "live_vm and not live_vm_tcg"` | native `live_vm` tier (11 tests on this branch), collection set unchanged |
| `just test-live-tcg` | `-m live_vm_tcg` | the four emulated-arch spine proofs |
| `just test-live-stack` | `-m live_stack` | all wire-transport tests (still includes the four) |
| `just test` | `-m "not live_vm and not live_stack"` | CI suite; excludes the four via `live_stack` |

The four proofs are **not** `live_vm`-marked, so `just test-live` never collects them; the
explicit `and not live_vm_tcg` is a cheap invariant guard so a future dual-marked test cannot
silently leak a slow TCG boot into the native tier (protecting the `test-live` acceptance
invariant). No change to `just test` — the proofs remain excluded through their `live_stack`
marker.

### `require_guest_arch(arch)` — the discovery-driven gate

Add to `tests/integration/live_stack/conftest.py`, beside `require_issuer` / `require_stack`
(the ADR-0035 §4 skip idiom), with injection seams mirroring the #1153 probe
(`default_guest_arch_accel_probe`) so its branches are unit-testable with no real host:

```python
def require_guest_arch(
    arch: str,
    *,
    host_arch: str | None = None,               # default platform.machine()
    which: Callable[[str], str | None] = shutil.which,
    kvm_present: Callable[[], bool] | None = None,  # default: the #1153 URI-selected probe
) -> str:
    """Skip unless this host can boot ``arch`` guests; return the resolved accelerator.

    Reuses the #1153 ``qemu_system_binary`` map (single source). Returns ``"kvm"`` when ``arch``
    is the host's native arch and the host KVM probe passes, else ``"tcg"``. Skips (never errors)
    when the arch's system emulator is not on PATH — the acceptance "skips cleanly when the host
    lacks the foreign qemu binary" gate.
    """
```

Behavior:

1. `binary = qemu_system_binary(arch)`; if `binary is None` or `which(binary) is None`
   → `pytest.skip(...)` naming the missing emulator and the install hint. This bootability
   decision is deliberately **URI-blind** (a foreign emulator on PATH is bootable regardless of
   the libvirt URI).
2. accel = `"kvm"` if `arch == (host_arch or platform.machine())` **and** the host KVM probe
   passes, else `"tcg"`. When `kvm_present` is not injected, it resolves at call time to the
   **#1153 URI-selected probe** — `kvm_probe_for_uri(resolved_libvirt_uri())` from
   `guest_arch_accel.py` — which is the *identical* signal the provider path uses to persist the
   System's accel (`os.access` R+W under `qemu:///session`, `os.path.exists` otherwise). Reusing
   it (not a bare `os.path.exists`) is what makes the gate's accel and the persisted accel
   genuinely single-sourced, so AC9's `persisted == expected` equality holds under any URI —
   including a native host under session-mode libvirt. For the four foreign-arch proofs on the
   x86_64 validation host, `arch != host_arch`, so this branch never runs the KVM probe and
   always yields `"tcg"`.
3. return the accel string.

**The returned accel is load-bearing, not cosmetic.** The four proofs' shared preflight
(`_ppc64le_reachability_preflight`, through which `_ppc64le_bundle_preflight` and the
kdump/fadump preflights already funnel) replaces its `shutil.which(_PPC64LE_EMULATOR)` block
with `expected_accel = require_guest_arch("ppc64le")` and adds `expected_accel` to its return
tuple. `_ppc64le_bundle_preflight`'s unpack of the reachability tuple is updated to drop the
slot it does not consume. The #1144 reachability proof — which already reads the **persisted**
accel from `systems.get` — asserts `persisted_accel == expected_accel` instead of the current
hardcoded `== "tcg"`. This makes the return value consumed, gives the tier a falsifiable "the
guest booted under the accelerator this host implies" check, and corrects a latent defect: the
hardcoded `== "tcg"` would fail on a native POWER host, where the gate correctly resolves
`"kvm"`. Because the gate is a single chokepoint, all four tests inherit the discovery-driven
skip from one edit; the now-unused `_PPC64LE_EMULATOR` constant is removed.

The gate is a **skip**, never a hard failure: an x86_64-only host without `qemu-system-ppc64`
skips the whole tier cleanly (acceptance criterion 2). The stack/issuer/image/kernel-tree
gates already present in the preflight are unchanged, so running the tier without the stack up
still skips cleanly via `require_stack()`.

### Tier-membership guard (CI, non-gated)

`just test-live-tcg` skips cleanly when its prerequisites are absent, and `test-live-stack`'s
exit-5-is-a-clean-skip idiom (which `test-live-tcg` mirrors) means an *empty* `-m live_vm_tcg`
selection reads green. That is correct for a bring-up recipe but dangerous for a tier of four
**known** proofs: a refactor that drops `@pytest.mark.live_vm_tcg` from all four would empty
the tier undetected. So a **non-gated** meta-test (running in the ordinary `just test` suite,
alongside the existing `tests/images/test_exit_criteria.py` marker pins) asserts that exactly
the four named proof functions carry **both** `live_stack` and `live_vm_tcg`, and that no other
test carries `live_vm_tcg`. It inspects the collected markers (via `pytest`'s collection or an
AST/`own_markers` walk of `test_live_stack.py`), so a dropped or stray marker fails CI at the
source — not only under a manual `--collect-only`.

### `just test-live-tcg`

Modeled on `test-live-stack` (the tier needs the same stack + fixtures, and these proofs are
`live_stack` tests): run `-m live_vm_tcg --strict-markers -q`, tolerating pytest exit 5 ("no
tests collected") as a clean skip so the recipe is safe before the marked drivers exist, with
other exit codes propagating. `just test-live` changes its selector to
`-m "live_vm and not live_vm_tcg"`.

### Documentation

- **AGENTS.md** — the commands table already lists `test-live`/`test-live-stack`; add
  `test-live-tcg` and a short "three live tiers" note (native `live_vm` / emulated
  `live_vm_tcg` / wire `live_stack`), noting the TCG tier needs the foreign qemu emulator and
  the stack, and skips cleanly without either.
- **Live-VM operator note** — the epic's operator docs (`docs/operating/install.md` /
  image-lifecycle runbook, extended in #1153) gain the tier table and the
  `qemu-system-ppc64` + stack prerequisites for `just test-live-tcg`, cross-referencing the
  per-arch accel doctor check.

## Acceptance criteria

- **AC1.** `just test-live` collects the **same set** of node ids as before this change (11 on
  this branch; the check is a set-diff, not a magic count) — no `live_vm_tcg` test appears in
  its collection; runtime unchanged on an x86_64-only host. Verifiable by diffing
  `pytest -m "live_vm and not live_vm_tcg" --collect-only -q` against `main`'s `-m live_vm`
  collection. (One collected test, `test_introspect_ppc64le_live.py`, is a ppc64le-specific
  *offline* vmcore read — it opens a retained core file and boots nothing, so it neither slows
  the native tier nor contradicts the rejected "parametrize the native tests" alternative,
  which is about *booting* foreign arches, not reading their cores.)
- **AC2.** `just test-live-tcg` collects exactly the four ppc64le spine proofs and skips the
  whole tier cleanly on a host without `qemu-system-ppc64` (no error, no failure). Verifiable
  with `pytest -m live_vm_tcg --collect-only` and by running the recipe on a host lacking the
  emulator.
- **AC3.** `live_vm_tcg` is registered in `pyproject.toml` `markers`, so `--strict-markers`
  collection does not warn/error on it.
- **AC4.** The four proofs carry both `live_stack` and `live_vm_tcg`; `just test` collection is
  unchanged (still excludes them via `live_stack`). Verifiable with
  `pytest -m "not live_vm and not live_stack" --collect-only` diffing the collected set.
- **AC5.** The gate reuses `qemu_system_binary` (no second qemu-binary literal in the test
  tree). Verifiable by grep: no new `"qemu-system-ppc64"` string literal outside the #1153 map.
- **AC6.** AGENTS.md and the operator doc name all three tiers and the `test-live-tcg`
  prerequisites.
- **AC7.** A non-gated meta-test (in the ordinary `just test` suite) asserts exactly the four
  named proofs carry both `live_stack` and `live_vm_tcg`, and no other test carries
  `live_vm_tcg`; dropping either marker from any of the four fails CI. Verifiable by deleting a
  marker and running `just test`.
- **AC8.** `require_guest_arch` has unit coverage for all four branches — unsupported/absent
  emulator → skip, native+`/dev/kvm` present → `"kvm"`, native+absent → `"tcg"`, foreign →
  `"tcg"` — via injected `host_arch`/`which`/`kvm_present`, mirroring
  `tests/diagnostics/test_guest_arch_accel.py`. The default (un-injected) KVM signal reuses the
  #1153 URI-selected probe (`kvm_probe_for_uri(resolved_libvirt_uri())`), so it matches the
  provider's persisted-accel signal under any libvirt URI.
- **AC9.** The #1144 reachability proof asserts the persisted `systems.get` accel equals the
  host-resolved `expected_accel` from the gate (not a hardcoded `"tcg"`), so it is correct on an
  x86_64 host (`tcg`) and on a POWER host under any URI (`kvm` under `qemu:///system`; `tcg`
  under `qemu:///session` when the worker uid cannot open `/dev/kvm` — matching the provider,
  because the gate reuses the same URI-selected probe).

## Non-goals

- Parametrizing the native `live_vm` boot tests with an arch fixture (they operate on one
  pre-provisioned System; a per-arch matrix would emit instances that cannot boot — see ADR
  rejected alternatives).
- A stack-free foreign-arch provision→boot harness (the stack is the only provision path).
- ppc64le CI runners; big-endian ppc64; POWER10-native (KVM-HV) validation (epic issue 17).

## Known unverified

- On a POWER host, `require_guest_arch("ppc64le")` returns `"kvm"`; the four proofs would then
  run natively rather than under TCG. This is correct (the marker names the emulated *class*,
  and the arch gate resolves the actual accel), but native-POWER execution of these proofs is
  gated on hardware (epic issue 17) and not exercised here.
