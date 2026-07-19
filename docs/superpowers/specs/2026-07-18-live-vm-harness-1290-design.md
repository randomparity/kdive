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
    settle_s: float = 0.0,
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
   `/tmp/kdive-cl-<hex>` path (the QMP UNIX-socket 108-byte limit) and
   **records the prior value and the short dir on the returned handle**; system
   mode is untouched and records nothing. The restore/cleanup happens in
   teardown (step 6) — the module is pytest-free, so it cannot lean on
   `monkeypatch` and must save/restore the process env explicitly.
3. `throwaway_domain_xml(...)` renders the domain (below); `defineXML` +
   `create()`.
4. Wait on the chosen condition (below) up to `wait_timeout_s`.
5. If `settle_s > 0`, sleep `settle_s` after the condition is reached, then
   `yield` the `LiveDomain`. `settle_s` preserves the exact
   `create(); sleep(2)` settle window the two existing tests use before their
   provider op touches the domain (see the dogfood section) — `wait_for`
   yields as soon as `isActive()` is true, which is earlier than `sleep(2)`, so
   a caller that needs the SLIRP netdev fully wired passes `settle_s=2.0`. It
   defaults to `0.0` (no settle).
6. `finally`, each step guarded so one failure does not mask the rest and
   teardown runs whether the body raised or the wait timed out: `destroy()` if
   active, `undefineFlags(...SNAPSHOTS_METADATA)`, `conn.close()`,
   `dest.unlink(missing_ok=True)`, then — for a session-mode boot — **restore
   `XDG_CONFIG_HOME` to its recorded prior value (or unset it if it was unset)
   and remove the short `/tmp/kdive-cl-<hex>` dir**.

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
- `wait_for_ssh(host, port, deadline_s, *, probe=_real_ssh_connect)` — polls a
  single-shot banner probe until the guest sshd answers or the deadline passes.
  The production `_real_ssh_connect` (connect.py) is one connect+banner attempt
  with its own short internal timeout; it is **not** a boot-waiter. `wait_for_ssh`
  is the missing outer loop: it calls `probe(host, port)` (default
  `_real_ssh_connect`), and on `False`/`OSError` (the port may refuse before the
  netdev listener is up, or accept-then-hang before sshd speaks) sleeps a short
  fixed interval (0.5s) and retries, until `probe` returns `True` (→ `True`) or
  `time.monotonic()` passes `deadline_s` (→ `False`). The inner probe timeout
  bounds each attempt; `deadline_s` bounds the whole wait. `probe` is an
  injected seam so the loop/deadline path is unit-testable without a live guest
  (inject a callable that returns `False` N times then `True`, or always
  `False`).

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
rootfs file is missing / **its parent directory is not writable** (a read-only
warm-store mount fails `qemu-img create` deep inside the boot; the resolver
catches it at resolve time) / the S3 set is partial → fail loud, because a
mis-provisioned runner must not masquerade as "no environment". The reason
string for a system-mode throwaway also names the `virt_image_t` SELinux
requirement — the label is not machine-checked here (it needs privileged
introspection), but naming it turns the otherwise-cryptic `defineXML`/`create`
denial into an actionable message.

#### The libvirt URI has one source of truth

`contract.libvirt_uri` is **the** resolved URI: it is `KDIVE_LIBVIRT_URI` when
set, else the `default_uri` the calling gate passes. A test threads
`contract.libvirt_uri` into `boot_throwaway_domain(..., mode=contract.libvirt_uri)`
— the `mode` parameter's own default (`qemu:///system`) applies only when a
caller boots a domain **without** going through the gate (e.g. a future test
with a bespoke URI). There are not two competing sources: the gate resolves the
URI, the test passes it as `mode`, `boot_throwaway_domain` obeys it.

A family-specific invariant still needs protection. The traffic-capture test
requires `qemu:///session` (unprivileged, dodges the ADR-0223 root-readback
wall, #1258); an operator who sets `KDIVE_LIBVIRT_URI=qemu:///system` must not
silently move it to system mode and re-expose that wall as a cryptic
root-owned-pcap failure. So the throwaway gate takes a `session_required: bool`:

```python
def require_live_vm_throwaway(
    default_uri: str = "qemu:///system", *, session_required: bool = False
) -> ThrowawayContract: ...
```

When `session_required` is `True` and the resolved `contract.libvirt_uri` is not
a `qemu:///session` URI, the gate **fails loud** (`pytest.fail`) naming the
conflict, rather than skipping (a skip would hide the coverage loss) or booting
into the wrong mode. The traffic test calls
`require_live_vm_throwaway("qemu:///session", session_required=True)`; the
snapshot test calls `require_live_vm_throwaway("qemu:///system")` and honors an
override freely (it is mode-flexible).

### The `require_live_vm_*` gates (tests side)

```python
def require_live_vm_throwaway(
    default_uri: str = "qemu:///system", *, session_required: bool = False
) -> ThrowawayContract:
    resolution = resolve_throwaway_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    contract = resolution.contract
    if session_required and not contract.libvirt_uri.startswith("qemu:///session"):
        pytest.fail(
            f"this test requires a qemu:///session URI (#1258 root-readback); "
            f"KDIVE_LIBVIRT_URI resolved to {contract.libvirt_uri!r}"
        )
    return contract
```

`require_live_vm_provisioned()` is the same shape for the provisioned-System
family (without `session_required` — it has no session-only invariant). These
sit under `tests/live_vm/` and are the `live_vm` analogue of `require_issuer` /
`require_stack` / `require_guest_arch`.

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
test has exactly one family sub-marker) — the debug/panic tests are un-migrated
until sub-issue E, and several `live_vm` tests fit neither throwaway nor
provisioned-System cleanly (e.g. `test_introspect_ppc64le_live.py` reads a
retained vmcore file and boots no domain), so bucketing every `live_vm` test is
part of E's migration audit, not A. A completeness guard now would red-fail.

**Window risk (A → D before E).** The epic order is A → (B ∥ C) → D → E, so
when D (the fail-loud per-family CI preflight) ships, the un-migrated `live_vm`
tests still carry only the bare marker. Under `-m live_vm_throwaway` they are
excluded; under `-m live_vm` they run unattributed. Either way a run could
declare a family, pass D's preflight, and still drop or mis-bucket real
coverage — the "green run that is no coverage" failure the primitive exists to
kill. A ships the enforceable *primitive* (markers + additivity) but cannot
close this window without E's full audit. The obligation is therefore recorded
for D: **D's preflight must count bare-`live_vm`-but-family-less tests and fail
loud when its declared-family selection would drop any**, until E's completeness
guard supersedes it. This spec records that so D does not treat the additivity
guard as if it were completeness.

### Dogfood: migrate the two throwaway tests

- `test_traffic_capture_live.py`: `contract = require_live_vm_throwaway(
  "qemu:///session", session_required=True)` then
  `boot_throwaway_domain(rootfs, mode=contract.libvirt_uri, ssh_hostfwd_port=port,
  wait_for="active", settle_s=2.0)`, marked `live_vm` + `live_vm_throwaway`. The
  `settle_s=2.0` preserves the existing `create(); sleep(2)` window before the
  filter-dump attach, so the SLIRP netdev is wired exactly as before. Its
  filter-dump attach/detach + pcap assertion is unchanged; only the
  boot/overlay/teardown/XDG boilerplate is replaced.
- `test_snapshot_live.py`: `contract = require_live_vm_throwaway("qemu:///system")`
  then `boot_throwaway_domain(rootfs, mode=contract.libvirt_uri, wait_for="active",
  settle_s=2.0)` (plain disk, no netdev), marked `live_vm` + `live_vm_throwaway`.
  Its snapshot create/revert/delete assertions are unchanged.

Both are **live-run on the maintainer's KVM host** as an acceptance step (this
host runs `live_vm` directly). Preserving `settle_s=2.0` keeps the migration a
true no-behavior-change: `wait_for="active"` alone yields as soon as
`isActive()` is true — earlier than the old `sleep(2)` — so without the settle
the traffic filter-dump could race the netdev it did not race before. A
no-behavior-change migration that still passes live is the proof the harness is
faithful to real libvirt.

## Error handling

- `boot_throwaway_domain` teardown guards each step
  (`contextlib.suppress(libvirt.libvirtError)` around destroy/undefine, `OSError`
  around unlink and the XDG-dir removal) so a partial failure still runs the rest
  — no leaked domain, overlay, or short-XDG dir, and `XDG_CONFIG_HOME` is
  restored.
- **Hard-kill orphans are out of scope for A.** Teardown runs only via the
  context manager; a `SIGKILL` or CI job-timeout kill bypasses it and leaks the
  `{name}.qcow2` overlay (and a session-mode `/tmp/kdive-cl-*` dir) into the
  warm-store rootfs directory. Sweeping those is the warm-store owner's job
  (sub-issue C keeps that directory warm across runs and owns its lifecycle);
  this spec records the obligation rather than sweeping by filename prefix here,
  which could race a concurrent run's overlay.
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
- Session-mode XDG round-trip: after a session-mode context exits — **and after
  it raises inside the body** — `XDG_CONFIG_HOME` equals its prior value (or is
  unset if it was unset) and the short `/tmp/kdive-cl-*` dir is gone. This is
  the finding-1 contamination guard.
- Wait predicates: `wait_for_panic` returns `True` once the console file
  contains `"Kernel panic"` and `False` at the deadline; `wait_for_active`
  polls `isActive`; `wait_for_ssh` with an injected `probe` returning `False`
  then `True` returns `True`, and with a probe that always returns `False`/raises
  `OSError` returns `False` at the deadline (the finding-4 loop/deadline
  coverage) — plus the borrowed banner-verdict shape for the real probe.
- Env resolution: `resolve_throwaway_contract` returns ABSENT when
  `KDIVE_LIVE_VM_ROOTFS` is unset, MISCONFIGURED when it points at a missing
  file **or a non-writable parent directory**, AVAILABLE with the resolved URI
  (honoring `KDIVE_LIBVIRT_URI` over the default) otherwise;
  `resolve_provisioned_contract` returns MISCONFIGURED when the `KDIVE_S3_*` set
  is partially configured. Assert the `session_required` gate fails loud when a
  resolved URI is not a session URI.
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
