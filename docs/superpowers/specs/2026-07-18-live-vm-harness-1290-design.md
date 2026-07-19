# `live_vm` harness + environment contract (epic #1289, sub-issue A)

- **Date:** 2026-07-18
- **Status:** Draft
- **Issue:** [#1290](https://github.com/randomparity/kdive/issues/1290)
- **Epic:** [#1289](https://github.com/randomparity/kdive/issues/1289) · epic spec
  [`docs/design/2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- **ADR:** [0386 — live-test framework and arch-additive runner topology](../../adr/0386-live-test-framework-runner-topology.md)
  (this sub-issue implements it; no new ADR)

## Problem

The `live_vm` tier — boot a real throwaway libvirt domain, run one provider op
against it, tear it down — has no shared harness. The epic spec catalogs the
duplication; this sub-issue removes it. Concretely, today:

- Throwaway-domain boot + overlay + teardown is copy-pasted between
  `tests/providers/local_libvirt/test_traffic_capture_live.py` and
  `test_snapshot_live.py` (same `qemu-img create -b` overlay, same inline
  `<domain>` XML hardcoded to `q35`/`x86_64`, same `defineXML → create() →
  sleep → isActive` and the same `finally` destroy/undefine/unlink).
- The "Kernel panic" console-wait loop exists in three independent copies
  (`tests/mcp/debug/test_debug_live_attach.py` inline, and `_await_panic` in
  `test_debug_gdbmi_live_smoke.py` and
  `tests/providers/local_libvirt/test_live_preserve_attach.py`).
- The `libvirt.open(uri)` connect dance and the session-mode short-`XDG` fix are
  re-derived per module.
- Env-var skip preflights (`KDIVE_LIVE_VM_ROOTFS`, `KDIVE_LIBVIRT_URI`,
  `KDIVE_LIVE_VM_SYSTEM_ID`) are re-implemented per module rather than sitting
  beside the existing `require_issuer` / `require_stack` / `require_guest_arch`
  gates.
- The inline throwaway XML is hardcoded to `x86_64`/`q35`/`ttyS0`, so the epic's
  primary target (ppc64le `pseries`/`hvc0`) cannot be expressed at all.

The environment knowledge that makes any of this work (`qemu:///session` to
dodge the ADR-0223 root-readback wall, short `XDG_CONFIG_HOME` for the QMP
socket-path limit, modular libvirt daemons, `virt_image_t` relabel for staged
images) lives only in one test file and in maintainer memory. Each new live test
relearns it.

## Goals

1. One reusable, arch-parameterized way to boot a throwaway domain, wait for a
   chosen condition (`active` / `panic` / `ssh`), and guarantee teardown.
2. The environment contract encoded once: resolved libvirt mode (per-test),
   env-var sets per family, session-mode short-`XDG` handling, the overlay
   staged beside its rootfs so it inherits the SELinux label.
3. A family-selection primitive — additive `live_vm_throwaway` /
   `live_vm_provisioned` sub-markers — so a later CI preflight (sub-issue D) can
   declare which family it runs.
4. Prove the harness against real libvirt now: migrate the two throwaway-domain
   tests onto it and live-run them on the maintainer's KVM host.

## Non-goals

- No product/runtime behavior change and no database migration; this is test
  infrastructure. The one new `src/` module is pytest-free test-support code
  (the `dev_harness.py` precedent), imported by tests, not by the running
  server/worker/reconciler.
- No new ADR — this implements ADR-0386.
- Migrating the debug/panic-family tests
  (`test_debug_live_attach.py`, `test_debug_gdbmi_live_smoke.py`,
  `test_live_preserve_attach.py`) and adding the stricter *completeness* marker
  guard are **sub-issue E**. This sub-issue provides the primitives those tests
  will consume; it does not touch them.
- The fail-loud per-family CI preflight is **sub-issue D**. A ships the marker +
  require-gate primitives D builds on, not the CI wiring.

## Architecture

### The src/tests split (the seam)

The harness has two layers with different dependency profiles, so they live in
two places — mirroring `src/kdive/mcp/dev_harness.py` (ships the live-stack
client mechanism, imports no pytest) versus `require_issuer` / `require_stack`
(the `pytest.skip` gates in `tests/integration/live_stack/conftest.py`):

- **`src/kdive/testing/live_vm.py`** — the pytest-free mechanism. `pytest` is a
  dev-only dependency (`[dependency-groups].dev`), and no `src/` module imports
  it today; keeping this module pytest-free preserves that and keeps
  `import kdive.testing.live_vm` valid in any install. `libvirt` /
  `libvirt_qemu` imports stay lazy (inside functions), so the module imports
  cleanly on a host without them and the skip decision is the gate's job.
- **`tests/live_vm/__init__.py`** — the thin `pytest.skip` gates
  (`require_live_vm_throwaway`, `require_live_vm_provisioned`) plus the family
  marker meta-test lives under `tests/live_vm/`.

Consumers import the mechanism from `kdive.testing.live_vm` and the gate from
`tests.live_vm`.

### `boot_throwaway_domain` (the mechanism)

```python
@dataclass(frozen=True, slots=True)
class LiveDomain:
    name: str
    domain: object            # the libvirt virDomain (opaque; libvirt ships no stubs)
    conn: object              # the owning connection
    uri: str
    ssh_port: int | None
    console_log: Path | None

@contextmanager
def boot_throwaway_domain(
    rootfs: Path,
    *,
    arch: str,
    name: str,
    mode: str = "qemu:///system",
    memory_mb: int = 1024,
    vcpu: int = 1,
    ssh_hostfwd_port: int | None = None,
    kernel_path: Path | None = None,
    cmdline: str | None = None,
    console_log: Path | None = None,
    wait_for: str = "active",
    wait_timeout_s: float = 30.0,
) -> Iterator[LiveDomain]: ...
```

Behavior:

1. `create_overlay(rootfs, dest)` writes a qcow2 overlay backed by `rootfs`
   **beside the rootfs** (`rootfs.with_name(f"{name}.qcow2")`), so it inherits
   that directory's libvirt access + SELinux label — the same placement the two
   existing tests already rely on and the reason a staged-path overlay is not
   `data_home_t`-blocked under system-mode SELinux.
2. `connect_libvirt(mode)` opens the connection. When `mode` is a
   `qemu:///session` URI it first redirects `XDG_CONFIG_HOME` to a short
   `/tmp/kdive-cl-<hex>` path (the QMP UNIX-socket 108-byte limit); system mode
   is untouched.
3. `throwaway_domain_xml(...)` renders the domain (below); `defineXML` +
   `create()`.
4. Wait on the chosen condition (below) up to `wait_timeout_s`, then `yield`
   the `LiveDomain`.
5. `finally`: `destroy()` if active, `undefineFlags(...SNAPSHOTS_METADATA)`,
   `conn.close()`, and `dest.unlink(missing_ok=True)` — each guarded so one
   teardown failure does not mask the others. Teardown runs whether the body
   raised or the wait timed out.

`create_overlay` and `connect_libvirt` are public so a test that needs a bespoke
domain (an E panic test with production `render_domain_xml`) can still reuse the
overlay + connect + teardown discipline without the full builder.

### `throwaway_domain_xml` (the arch builder)

Builds the domain with `xml.etree.ElementTree` (no string interpolation, so no
path injects XML), consuming `arch_traits(arch)` for the arch-varying facts:

- `<os><type arch machine>` — `machine` from `traits.machine` (`q35` / `pseries`).
- `<domain type>` — `kvm` (throwaway domains run natively on the KVM host; TCG
  throwaway domains are out of scope for A, per the epic spec).
- serial console `<log file=console_log append="off">` when `console_log` is
  given, so `wait_for="panic"` can read it.
- optional direct-kernel `<os><kernel>` + `<cmdline>` when `kernel_path` is
  given; the cmdline defaults to `root=/dev/vda console=<traits.console_device>
  rw` (`ttyS0` / `hvc0`) when `cmdline` is `None`. Kernel *format* (`vmlinux`
  vs `bzImage`) needs no XML branch — libvirt's `<kernel>` takes either; it is
  the operator's staged file.
- optional SSH-forward netdev via `<qemu:commandline>` when `ssh_hostfwd_port`
  is set, reusing `SYSTEM_SSH_NETDEV_ID` and the `traits.pin_nic_slot` slot
  rule (`addr=0x10` on q35, unpinned on pseries) so the traffic-capture netdev
  matches production exactly.

The builder deliberately does **not** require a `ProvisioningProfile` (unlike
production `render_domain_xml`), because a throwaway domain has no System, no
metadata row, and no mandatory SSH forward. Reusing `arch_traits` — not copying
its table — is the single source of truth for the arch branch.

### Wait predicates

Three module-level helpers, each pure enough to unit-test by injecting a fake
domain / a console file:

- `wait_for_active(domain, deadline_s)` — polls `domain.isActive()`.
- `wait_for_panic(console_log, deadline_s)` — polls for `"Kernel panic"` in the
  console file (the three copied loops collapse here). Reads with
  `errors="replace"`.
- `wait_for_ssh(host, port, deadline_s)` — reuses the production
  `_real_ssh_connect` banner probe idiom (an sshd `SSH-` identification read),
  so "wait until the guest sshd answers" is one implementation shared with the
  Connect plane's reachability semantics.

Each returns `bool` (reached before the deadline); `boot_throwaway_domain`
raises a clear `LiveVmBootTimeout` when the selected wait returns `False`, so a
domain that never reached its condition fails loud with the domain name and mode
rather than yielding a half-booted domain.

### The environment contract (resolution)

Env-var names are module constants (`KDIVE_LIVE_VM_ROOTFS`,
`KDIVE_LIVE_VM_SYSTEM_ID`, `KDIVE_LIBVIRT_URI`; the `KDIVE_S3_*` set already
exists in `kdive.config`). Resolution returns a typed status rather than calling
`pytest.skip`, so the pytest-free module stays pytest-free and the gate owns the
skip/fail decision:

```python
class LiveVmEnvState(Enum):
    AVAILABLE = "available"
    ABSENT = "absent"          # required env unset -> skip
    MISCONFIGURED = "misconfigured"  # env set but wrong (missing file, ...) -> fail loud

@dataclass(frozen=True, slots=True)
class ThrowawayContract:
    rootfs: Path
    libvirt_uri: str

@dataclass(frozen=True, slots=True)
class ProvisionedContract:
    system_id: str
    libvirt_uri: str
    # KDIVE_S3_* presence asserted here (fail-loud when partially set)

def resolve_throwaway_contract(default_uri: str) -> EnvResolution[ThrowawayContract]: ...
def resolve_provisioned_contract() -> EnvResolution[ProvisionedContract]: ...
```

`EnvResolution` carries the `LiveVmEnvState` and either the contract (AVAILABLE)
or a human reason (ABSENT / MISCONFIGURED). The **skip vs. fail** discipline
matches `test_introspect_ppc64le_live.py`: env unset → skip; env set but the
rootfs file is missing / the S3 set is partial → fail loud, because a
mis-provisioned runner must not masquerade as "no environment".

### The `require_live_vm_*` gates (tests side)

```python
def require_live_vm_throwaway(default_uri: str = "qemu:///system") -> ThrowawayContract:
    resolution = resolve_throwaway_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    return resolution.contract
```

`require_live_vm_provisioned()` is the same shape for the provisioned-System
family. These sit under `tests/live_vm/` and are the `live_vm` analogue of
`require_issuer` / `require_stack` / `require_guest_arch`.

### Family sub-markers (additive)

Register in `pyproject.toml` `[tool.pytest.ini_options].markers` (the tcg lane
runs under `--strict-markers`, so an unregistered marker errors):

- `live_vm_throwaway` — a throwaway-domain `live_vm` test served by
  `boot_throwaway_domain`.
- `live_vm_provisioned` — a `live_vm` test against an externally provisioned
  System (`KDIVE_LIVE_VM_SYSTEM_ID` + `KDIVE_S3_*`).

**Additive:** every marked test keeps the bare `@pytest.mark.live_vm` and adds
its family sub-marker, so `-m live_vm` still selects both families and the
shipped `test-live` recipe (`-m "live_vm and not live_vm_tcg"`) is unchanged.

A meta-test (`tests/live_vm/test_family_markers.py`) asserts **additivity**:
every test carrying `live_vm_throwaway` or `live_vm_provisioned` also carries
`live_vm`. It deliberately does **not** assert *completeness* (every `live_vm`
test has a family sub-marker) — the debug/panic tests are un-migrated until
sub-issue E, so a completeness guard would red-fail now. Completeness lands with
E once every `live_vm` test is tagged.

### Dogfood: migrate the two throwaway tests

- `test_traffic_capture_live.py` → `boot_throwaway_domain(..., mode="qemu:///session",
  ssh_hostfwd_port=port, wait_for="active")`, gated by `require_live_vm_throwaway`,
  marked `live_vm` + `live_vm_throwaway`. Its filter-dump attach/detach + pcap
  assertion is unchanged; only the boot/overlay/teardown/XDG boilerplate is
  replaced.
- `test_snapshot_live.py` → `boot_throwaway_domain(..., mode="qemu:///system",
  wait_for="active")` (plain disk, no netdev), same gate + markers. Its
  snapshot create/revert/delete assertions are unchanged.

Both are **live-run on the maintainer's KVM host** as an acceptance step (this
host runs `live_vm` directly). A no-behavior-change migration that still passes
live is the proof the harness is faithful to real libvirt.

## Error handling

- `boot_throwaway_domain` teardown guards each step
  (`contextlib.suppress(libvirt.libvirtError)` around destroy/undefine, `OSError`
  around unlink) so a partial failure still runs the rest — no leaked domain or
  overlay.
- A wait timeout raises `LiveVmBootTimeout` (a plain harness exception) naming
  the domain, mode, and condition; teardown still runs via the context manager.
- `throwaway_domain_xml` raises `CategorizedError(CONFIGURATION_ERROR)` for an
  unknown arch — it calls `arch_traits(arch)`, which already fails fast rather
  than silently defaulting to x86 `q35`/`ttyS0`.
- The env resolvers never raise for "unset" (that is ABSENT → skip); they return
  MISCONFIGURED for "set but wrong" so the gate fails loud.

## Testing

Unit tests (no KVM host — run in `just ci`):

- `throwaway_domain_xml`: parametrized over `x86_64` and `ppc64le`, assert the
  emitted `<os type machine>` is `q35`/`pseries`, the cmdline console is
  `ttyS0`/`hvc0`, the NIC slot is pinned only on q35, and the SSH netdev is
  present iff `ssh_hostfwd_port` is set. Assert an unknown arch raises
  `CONFIGURATION_ERROR`.
- `boot_throwaway_domain` teardown + overlay: inject a fake libvirt conn/domain
  (the `FakeLibvirtConn`/`FakeDomain` pattern already in the repo) and a fake
  `create_overlay`; assert define→create→wait→yield ordering, that teardown
  calls destroy (when active) then undefine then unlink, and that each runs even
  when an earlier one raises. Assert a wait that never succeeds raises
  `LiveVmBootTimeout` and still tears down.
- Wait predicates: `wait_for_panic` returns `True` once the console file
  contains `"Kernel panic"` and `False` at the deadline; `wait_for_active`
  polls `isActive`; `wait_for_ssh` accepts an `SSH-` banner and rejects a
  non-SSH listener (reuse the existing banner-verdict coverage shape).
- Env resolution: `resolve_throwaway_contract` returns ABSENT when
  `KDIVE_LIVE_VM_ROOTFS` is unset, MISCONFIGURED when it points at a missing
  file, AVAILABLE with the resolved URI otherwise; `resolve_provisioned_contract`
  returns MISCONFIGURED when the `KDIVE_S3_*` set is partially configured.
- Family-marker additivity meta-test (above).

Live acceptance (maintainer host, `just test-live`): the two migrated tests pass
unchanged in behavior.

## Acceptance criteria

- `boot_throwaway_domain`, `throwaway_domain_xml`, `connect_libvirt`,
  `create_overlay`, the three wait predicates, and the env resolvers exist in
  `src/kdive/testing/live_vm.py` and are unit-tested without a KVM host.
- `require_live_vm_throwaway` / `require_live_vm_provisioned` exist under
  `tests/live_vm/` beside the pattern of `require_issuer` / `require_stack`.
- `live_vm_throwaway` / `live_vm_provisioned` are registered in `pyproject.toml`
  markers; `-m live_vm` still selects both families; the additivity meta-test
  passes.
- `test_traffic_capture_live.py` and `test_snapshot_live.py` are migrated onto
  the harness, keep their assertions, and pass live on the maintainer host.
- `just ci` is green; the `test-live` recipe selection
  (`-m "live_vm and not live_vm_tcg"`) is unchanged.
- The contract is documented in the module docstring so a new live test consumes
  it without rediscovery.

## Rollout

Single PR on `feat/live-vm-harness-1290`. Order within the PR: markers +
env-contract resolvers + require gates (no behavior) → arch builder + wait
predicates + `boot_throwaway_domain` + unit tests → migrate the two throwaway
tests → live-prove. Each step keeps `just ci` green.
