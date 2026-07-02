# Remote-libvirt SSH bootstrap-key injection + agent SSH parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give remote-libvirt SSH parity with local — inject the per-System bootstrap key into a remote guest over the guest agent and expose a reachable SSH endpoint — so `ssh_info`/`authorize_ssh_key` work on remote Systems (#966).

**Architecture:** Config-gated (`ssh_addr`+`ssh_range` on the `[[remote_libvirt]]` instance). When active: render a QEMU user-mode `hostfwd` NIC into the domain XML with a per-System port allocated via the gdbstub-registry pattern; inject the bootstrap pubkey after `wait_for_agent` via one fixed `/bin/sh -c` guest-exec hop (key on stdin); `recorded_ssh_endpoint` reads the port from live XML over TLS; `authorize_ssh_key` targets the recorded endpoint host.

**Tech Stack:** Python 3.14, `uv`, `pytest`, libvirt-python, `ty`, `ruff`. Spec: `docs/superpowers/specs/2026-07-01-remote-ssh-key-injection-design.md`. Decision: [ADR-0291](../../adr/0291-remote-ssh-bootstrap-injection.md).

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict, whole tree (`just type` covers `src`+`tests`).
- Absolute imports only (`kdive.…`); no relative imports.
- `≤100` lines/function, cyclomatic `≤8`, `≤5` positional params, Google-style docstrings on non-trivial public APIs.
- Return `CategorizedError` with the most specific existing `ErrorCategory`; never invent categories.
- Every guest/console output passes the redactor before persistence or a response snippet.
- `live_vm` tests stay gated (marker in `pyproject.toml`); do not un-gate.
- Guardrail before each commit: `just lint && just type && uv run python -m pytest <focused> -q`. Full `just ci` before push.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- No DB migration. No new RBAC/error-category/tool.

## File structure

- `src/kdive/inventory/model.py` — `RemoteLibvirtInstance` gains optional `ssh_addr`, `ssh_range`.
- `src/kdive/providers/remote_libvirt/config.py` — `RemoteLibvirtConfig` gains `ssh_addr`/`ssh_port_min`/`ssh_port_max` + `ssh_parity_active`; `_parse_ssh_range`; half-config + overlap guards.
- `src/kdive/providers/shared/libvirt_xml.py` — generalize `_SSH_HOSTFWD_RE` to any bind address.
- `src/kdive/providers/remote_libvirt/lifecycle/xml.py` — render the ssh `hostfwd` NIC; re-export `recorded_ssh_port(_strict)`.
- `src/kdive/providers/remote_libvirt/lifecycle/gdb.py` — add `used_ssh_ports` (reuse `allocate_gdb_port`).
- `src/kdive/providers/remote_libvirt/guest/bootstrap_key.py` — new `RemoteBootstrapKeyInjector`.
- `src/kdive/providers/ports/lifecycle.py` — `bootstrap_pubkey` on `Provisioner.provision`/`reprovision`.
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`, `src/kdive/providers/fault_inject/…` — accept + ignore `bootstrap_pubkey`.
- `src/kdive/jobs/handlers/systems.py` — thread `bootstrap_pubkey` to provision/reprovision.
- `src/kdive/providers/remote_libvirt/lifecycle/provisioning.py` — allocate + render ssh port; inject key post-`wait_for_agent`; injector seam.
- `src/kdive/providers/remote_libvirt/lifecycle/connect.py` — real `recorded_ssh_endpoint`.
- `src/kdive/providers/remote_libvirt/composition.py` — wire `RemoteLibvirtConnect.from_env(secret_registry=…)`.
- `src/kdive/jobs/handlers/ssh_authorize.py` — `build_authorize_argv(host, port, key_path)`.
- Docs: a `[[remote_libvirt]]` `ssh_addr`/`ssh_range` example + operator ACL note.

---

### Task 1: Config — optional `ssh_addr`/`ssh_range` + validation

**Files:**
- Modify: `src/kdive/inventory/model.py` (`RemoteLibvirtInstance`)
- Modify: `src/kdive/providers/remote_libvirt/config.py`
- Test: `tests/providers/remote_libvirt/test_config.py` (existing; add cases)

**Interfaces:**
- Produces: `RemoteLibvirtConfig.ssh_addr: str | None`, `.ssh_port_min: int | None`, `.ssh_port_max: int | None`, `.ssh_parity_active: bool` (property, `True` iff `ssh_addr` and both ports set).

- [ ] **Step 1: Failing tests.** Add to `tests/providers/remote_libvirt/test_config.py`:

```python
def _instance(**over):
    base = dict(
        name="rl", cost_class="c", uri="qemu+tls://h/system", gdb_addr="10.0.0.1",
        gdbstub_range="47000:47099", client_cert_ref="c", client_key_ref="k",
        ca_cert_ref="a", base_image="img", vcpus=2, memory_mb=2048,
    )
    base.update(over)
    return RemoteLibvirtInstance(**base)

def test_ssh_parity_inactive_when_unset():
    cfg = _build_config(_instance())
    assert cfg.ssh_parity_active is False
    assert cfg.ssh_addr is None

def test_ssh_parity_active_parses_range():
    cfg = _build_config(_instance(ssh_addr="10.0.0.1", ssh_range="47100:47199"))
    assert cfg.ssh_parity_active is True
    assert (cfg.ssh_port_min, cfg.ssh_port_max) == (47100, 47199)

def test_half_configured_ssh_is_error():
    with pytest.raises(CategorizedError) as ei:
        _build_config(_instance(ssh_addr="10.0.0.1"))
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR

def test_ssh_range_inverted_is_error():
    with pytest.raises(CategorizedError):
        _build_config(_instance(ssh_addr="10.0.0.1", ssh_range="500:400"))

def test_overlap_with_gdb_range_on_same_addr_is_error():
    with pytest.raises(CategorizedError) as ei:
        _build_config(_instance(ssh_addr="10.0.0.1", ssh_range="47050:47150"))
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR

def test_overlap_allowed_on_distinct_addr():
    cfg = _build_config(_instance(ssh_addr="10.0.0.2", ssh_range="47050:47150"))
    assert cfg.ssh_parity_active is True
```

(Import `_build_config`, `RemoteLibvirtInstance`, `CategorizedError`, `ErrorCategory`, `pytest` as the file already does; match existing fixture style if the file has one.)

- [ ] **Step 2: Run — expect FAIL** (`ssh_addr` unknown kwarg / attribute missing).

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q`

- [ ] **Step 3: Model.** In `src/kdive/inventory/model.py`, add to `RemoteLibvirtInstance` (after `gdbstub_range`):

```python
    ssh_addr: str | None = None
    ssh_range: str | None = None
```

- [ ] **Step 4: Config dataclass + property.** In `config.py` `RemoteLibvirtConfig` add fields after `gdb_port_max`:

```python
    ssh_addr: str | None = None
    ssh_port_min: int | None = None
    ssh_port_max: int | None = None

    @property
    def ssh_parity_active(self) -> bool:
        """True when the operator declared an SSH forward (ssh_addr + a parsed range)."""
        return (
            self.ssh_addr is not None
            and self.ssh_port_min is not None
            and self.ssh_port_max is not None
        )
```

- [ ] **Step 5: Parse + validate.** Add `_parse_ssh_range` (mirror `_parse_gdbstub_range` but a single port may span 1 — no reserved probe), and in `_build_config` after the gdb range parse:

```python
    ssh_addr, ssh_port_min, ssh_port_max = _resolve_ssh_forward(instance, gdb_port_min, gdb_port_max)
```

with a helper:

```python
def _resolve_ssh_forward(
    instance: RemoteLibvirtInstance, gdb_port_min: int, gdb_port_max: int
) -> tuple[str | None, int | None, int | None]:
    """Resolve the optional SSH forward: (ssh_addr, ssh_port_min, ssh_port_max) or all None.

    Raises CONFIGURATION_ERROR for a half-configured pair, a malformed/inverted range, or a
    range that overlaps the gdbstub range when the SSH and gdbstub bind addresses are equal.
    """
    if instance.ssh_addr is None and instance.ssh_range is None:
        return None, None, None
    if instance.ssh_addr is None or instance.ssh_range is None:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}] sets only one of ssh_addr/ssh_range; both are "
            "required to expose an SSH forward",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    low, high = _parse_ssh_range(instance)
    if instance.ssh_addr == instance.gdb_addr and low <= gdb_port_max and gdb_port_min <= high:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].ssh_range {low}:{high} overlaps gdbstub_range "
            f"{gdb_port_min}:{gdb_port_max} on the shared bind address {instance.ssh_addr!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return instance.ssh_addr, low, high
```

Pass the three values into the `RemoteLibvirtConfig(...)` constructor. (`_parse_ssh_range` reuses the `_parse_gdbstub_range` body with `ssh_range`/`ssh_range` field names and drops the `low == high` rejection — a one-port SSH range is valid.)

- [ ] **Step 6: Run — expect PASS.**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q`
Then: `just lint && just type`

- [ ] **Step 7: Commit.**

```bash
git add src/kdive/inventory/model.py src/kdive/providers/remote_libvirt/config.py tests/providers/remote_libvirt/test_config.py
git commit -m "feat(remote): config-gated ssh_addr/ssh_range for SSH parity"
```

---

### Task 2: XML — render the SSH `hostfwd` NIC + generalize the recorded-port parser

**Files:**
- Modify: `src/kdive/providers/shared/libvirt_xml.py` (`_SSH_HOSTFWD_RE`)
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/xml.py` (`render_domain_xml`; re-export `recorded_ssh_port(_strict)`)
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/gdb.py` (`used_ssh_ports`)
- Test: `tests/providers/remote_libvirt/lifecycle/test_xml.py`, `tests/providers/shared/test_libvirt_xml.py`, `tests/providers/remote_libvirt/lifecycle/test_gdb.py`

**Interfaces:**
- Consumes: `RemoteLibvirtConfig.ssh_addr/ssh_port_min/ssh_port_max` (Task 1).
- Produces: `render_domain_xml(..., ssh_addr: str | None = None, ssh_port: int | None = None)`; `recorded_ssh_port(domain_xml) -> int | None`; `recorded_ssh_port_strict(domain_xml, *, operation, domain) -> int | None`; `used_ssh_ports(conn) -> dict[str, int]`.

- [ ] **Step 1: Failing tests.** In `tests/providers/remote_libvirt/lifecycle/test_xml.py`:

```python
def test_render_appends_ssh_hostfwd_when_set():
    xml = render_domain_xml(SID, profile, pool="p", volume="v", gdb_addr="10.0.0.1",
                            gdb_port=47001, ssh_addr="10.0.0.1", ssh_port=47101)
    assert "-netdev" in xml
    assert "hostfwd=tcp:10.0.0.1:47101-:22" in xml
    assert "virtio-net-pci,netdev=kdivessh" in xml
    assert recorded_ssh_port(xml) == 47101

def test_render_omits_ssh_hostfwd_when_unset():
    xml = render_domain_xml(SID, profile, pool="p", volume="v", gdb_addr="10.0.0.1", gdb_port=47001)
    assert "kdivessh" not in xml
    assert recorded_ssh_port(xml) is None
```

In `tests/providers/shared/test_libvirt_xml.py` add:

```python
def test_recorded_ssh_port_parses_nonloopback_addr():
    xml = f'<domain><cmdline><arg value="hostfwd=tcp:10.0.0.5:47101-:22"/></cmdline></domain>'
    # use the project's existing helper to build a qemu:commandline arg if one exists
    assert recorded_ssh_port(xml) == 47101  # generalized regex, any bind addr
```

(Adjust the XML fixture to the namespaced `<qemu:commandline><qemu:arg>` shape the existing tests use.)

In `tests/providers/remote_libvirt/lifecycle/test_gdb.py`:

```python
def test_used_ssh_ports_enumerates_recorded_forwards():
    conn = _FakeConn([_FakeDomain("kdive-a", ssh_port=47101), _FakeDomain("other", ssh_port=None)])
    assert used_ssh_ports(conn) == {"kdive-a": 47101}
```

- [ ] **Step 2: Run — expect FAIL.**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_xml.py tests/providers/remote_libvirt/lifecycle/test_gdb.py -q`

- [ ] **Step 3: Generalize the shared regex.** In `providers/shared/libvirt_xml.py` change:

```python
_SSH_HOSTFWD_RE = re.compile(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22")
```

to (matches any non-`:` bind host — IPv4/hostname; IPv6 bracket form is a known follow-up):

```python
_SSH_HOSTFWD_RE = re.compile(r"hostfwd=tcp:[^:]+:(\d+)-:22")
```

- [ ] **Step 4: Render + parse in remote xml.py.** Add params to `render_domain_xml` and, after the `-gdb` args, append when both provided:

```python
    if ssh_addr is not None and ssh_port is not None:
        netdev = f"user,id=kdivessh,restrict=on,hostfwd=tcp:{ssh_addr}:{ssh_port}-:22"
        ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
        ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
        ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
        ET.SubElement(
            commandline, f"{{{QEMU_NS}}}arg", value="virtio-net-pci,netdev=kdivessh,addr=0x10"
        )
```

Re-export the shared parsers (mirroring the existing `recorded_gdb_port` re-export):

```python
from kdive.providers.shared.libvirt_xml import (
    recorded_ssh_port as recorded_ssh_port,
    recorded_ssh_port_from_root as recorded_ssh_port_from_root,
)
```

and add a strict wrapper next to `recorded_gdb_port_strict`:

```python
def recorded_ssh_port_strict(domain_xml: str, *, operation: str, domain: str) -> int | None:
    """The SSH hostfwd port a domain records; malformed XML is an infrastructure fault."""
    root = _parse_domain_xml_strict(domain_xml, operation=operation, domain=domain)
    return recorded_ssh_port_from_root(root)
```

- [ ] **Step 5: `used_ssh_ports`.** In `gdb.py`, import `recorded_ssh_port_strict` and add a sibling of `used_gdb_ports` that calls it (same domain-vanishing skip). Keep `allocate_gdb_port` as the shared allocator (it is generic).

- [ ] **Step 6: Run — expect PASS**, then `just lint && just type`.

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/ tests/providers/shared/test_libvirt_xml.py -q`

- [ ] **Step 7: Commit.**

```bash
git add src/kdive/providers/shared/libvirt_xml.py src/kdive/providers/remote_libvirt/lifecycle/xml.py src/kdive/providers/remote_libvirt/lifecycle/gdb.py tests/providers/remote_libvirt/lifecycle/ tests/providers/shared/test_libvirt_xml.py
git commit -m "feat(remote): render per-System SSH hostfwd NIC + recorded-port read"
```

---

### Task 3: `RemoteBootstrapKeyInjector` — guest-agent key write

**Files:**
- Create: `src/kdive/providers/remote_libvirt/guest/bootstrap_key.py`
- Test: `tests/providers/remote_libvirt/guest/test_bootstrap_key.py`

**Interfaces:**
- Consumes: `GuestAgentExec`, `AgentCommand`, `qemu_agent_command`, `GuestDomain` (`guest/agent.py`).
- Produces: `RemoteBootstrapKeyInjector(agent_command=qemu_agent_command, timeout_s=60.0)` with `.inject(domain: GuestDomain, pubkey: str) -> None`; `INJECT_SCRIPT: str`.

- [ ] **Step 1: Failing tests.**

```python
def test_inject_runs_shell_with_key_on_stdin():
    calls = []
    def fake_agent(domain, command, timeout, flags):
        calls.append(json.loads(command))
        if '"guest-exec"' in command:
            return json.dumps({"return": {"pid": 7}})
        return json.dumps({"return": {"exited": True, "exitcode": 0}})
    RemoteBootstrapKeyInjector(agent_command=fake_agent).inject(_Dom("kdive-x"), "ssh-ed25519 AAAA k")
    spawn = calls[0]["arguments"]
    assert spawn["path"] == "/bin/sh"
    assert spawn["arg"][0] == "-c"
    assert base64.b64decode(spawn["input-data"]).decode() == "ssh-ed25519 AAAA k"

def test_inject_nonzero_exit_raises_provisioning_failure():
    def fake_agent(domain, command, timeout, flags):
        if '"guest-exec"' in command:
            return json.dumps({"return": {"pid": 7}})
        return json.dumps({"return": {"exited": True, "exitcode": 1}})
    with pytest.raises(CategorizedError) as ei:
        RemoteBootstrapKeyInjector(agent_command=fake_agent).inject(_Dom("kdive-x"), "k")
    assert ei.value.category is ErrorCategory.PROVISIONING_FAILURE
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

Run: `uv run python -m pytest tests/providers/remote_libvirt/guest/test_bootstrap_key.py -q`

- [ ] **Step 3: Implement.**

```python
"""Inject the per-System bootstrap key into a remote guest over the guest agent (ADR-0291).

The worker cannot virt-customize a remote disk (ADR-0289 obstacle), so the pre-SSH channel is
the qemu-guest-agent. This writes the public key into /root/.ssh/authorized_keys via one fixed,
worker-composed `/bin/sh -c` hop with the key on stdin (never in argv) — the ADR-0271
authorize-script shape — allowlist {'/bin/sh'}. A bounded exception to ADR-0078's debug-target
no-shell rule (precedent ADR-0100), documented in ADR-0291.
"""
from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    GuestAgentExec,
    GuestDomain,
    qemu_agent_command,
)

_SHELL = "/bin/sh"
_INJECT_TIMEOUT_S = 60.0

INJECT_SCRIPT = (
    "set -e\n"
    "umask 077\n"
    "mkdir -p /root/.ssh\n"
    "key=$(cat)\n"
    "touch /root/.ssh/authorized_keys\n"
    'grep -qxF "$key" /root/.ssh/authorized_keys '
    "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
)


class RemoteBootstrapKeyInjector:
    """Write the bootstrap public key into a remote guest's root authorized_keys."""

    def __init__(
        self, *, agent_command: AgentCommand = qemu_agent_command, timeout_s: float = _INJECT_TIMEOUT_S
    ) -> None:
        self._agent_command = agent_command
        self._timeout_s = timeout_s

    def inject(self, domain: GuestDomain, pubkey: str) -> None:
        """Append ``pubkey`` to the guest root authorized_keys, idempotently.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` for a non-zero in-guest exit; the guest-agent
                error contract (``CONFIGURATION_ERROR``/``TRANSPORT_FAILURE``/``INFRASTRUCTURE_FAILURE``)
                propagated from ``GuestAgentExec``.
        """
        agent = GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_SHELL}),
            timeout_s=self._timeout_s,
        )
        result = agent.run(domain, [_SHELL, "-c", INJECT_SCRIPT], input_data=pubkey)
        if result.exit_status != 0:
            raise CategorizedError(
                "guest-agent bootstrap key injection exited non-zero",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain.name(), "exit_status": result.exit_status},
            )
```

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/guest/bootstrap_key.py tests/providers/remote_libvirt/guest/test_bootstrap_key.py
git commit -m "feat(remote): guest-agent bootstrap-key injector"
```

---

### Task 4: `Provisioner` port `bootstrap_pubkey` param; local/fault-inject ignore

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py` (`Provisioner.provision`/`reprovision`)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`
- Modify: `src/kdive/providers/fault_inject/…` (the provision/reprovision impl)
- Test: existing local/fault-inject provisioning tests (add a "ignores bootstrap_pubkey" assertion)

**Interfaces:**
- Produces: `provision(system_id, profile, *, overlay_customizers=(), bootstrap_pubkey: str | None = None) -> str` (and `reprovision` likewise) on the port and all three providers.

- [ ] **Step 1: Failing test.** In the local provisioning test, call `provision(..., bootstrap_pubkey="ssh-ed25519 k")` and assert it still returns the domain name and does NOT change the overlay-customizer behavior (local injection stays via `overlay_customizers`).

- [ ] **Step 2: Run — expect FAIL** (unexpected keyword argument).

- [ ] **Step 3: Add the param.** Add `bootstrap_pubkey: str | None = None` to the `Provisioner` protocol `provision`/`reprovision`, and to the local + fault-inject signatures with `del bootstrap_pubkey` (documented: local injects pre-boot via `overlay_customizers`; fault-inject has no guest). Update each docstring one line.

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/ports/lifecycle.py src/kdive/providers/local_libvirt/lifecycle/provisioning.py src/kdive/providers/fault_inject/ tests/
git commit -m "feat(providers): add bootstrap_pubkey to the Provisioner port (local/fault-inject ignore)"
```

---

### Task 5: Handler threads `bootstrap_pubkey` to provision/reprovision

**Files:**
- Modify: `src/kdive/jobs/handlers/systems.py`
- Test: `tests/jobs/handlers/test_systems_bootstrap_key.py`

**Interfaces:**
- Consumes: Task 4's `bootstrap_pubkey` param.
- Produces: provision/reprovision handlers call `provisioner.provision(..., overlay_customizers=customizers, bootstrap_pubkey=pubkey)`.

- [ ] **Step 1: Failing test.** Extend `_RecordingProvisioner` to record `bootstrap_pubkey`; assert `test_provision_handler_...` sees the ensured pubkey passed as `bootstrap_pubkey` (and it equals the row's public key). Same for reprovision.

- [ ] **Step 2: Run — expect FAIL.**

Run: `uv run python -m pytest tests/jobs/handlers/test_systems_bootstrap_key.py -q`

- [ ] **Step 3: Implement.** Refactor `_bootstrap_key_customizers` to also return the pubkey:

```python
async def _bootstrap_key_material(
    conn: AsyncConnection, system_id: UUID, runtime: ProviderRuntime
) -> tuple[tuple[Callable[[str], None], ...], str]:
    """Ensure the System's bootstrap key (committed) and return (overlay_customizers, pubkey)."""
    async with conn.transaction():
        pubkey = await ensure_system_bootstrap_key(conn, system_id)
    factory = runtime.bootstrap_key_customizer
    customizers = (factory(pubkey),) if factory is not None else ()
    return customizers, pubkey
```

In both `provision_handler` and `reprovision_handler`:

```python
    customizers, pubkey = await _bootstrap_key_material(conn, system_id, runtime)
    domain_name = await asyncio.to_thread(
        functools.partial(
            provisioner.provision, system_id, profile,
            overlay_customizers=customizers, bootstrap_pubkey=pubkey,
        )
    )
```

(and the `reprovision` call likewise). Delete the old `_bootstrap_key_customizers` if now unused.

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/jobs/handlers/systems.py tests/jobs/handlers/test_systems_bootstrap_key.py
git commit -m "feat(jobs): thread ensured bootstrap pubkey to provision/reprovision"
```

---

### Task 6: Remote provisioning — allocate + render ssh port, inject key

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/provisioning.py`
- Test: `tests/providers/remote_libvirt/lifecycle/test_provisioning.py`

**Interfaces:**
- Consumes: Task 1 config, Task 2 `render_domain_xml`/`used_ssh_ports`, Task 3 injector, Task 4 `bootstrap_pubkey`, `allocate_gdb_port`.
- Produces: `RemoteLibvirtProvisioning(..., bootstrap_injector: _Injector | None = None)`; ssh port allocated + rendered + key injected when `config.ssh_parity_active` and `bootstrap_pubkey`.

- [ ] **Step 1: Failing tests.** With a fake conn/config where `ssh_parity_active` is True and a recording injector:
  - `provision` renders XML containing the ssh hostfwd (assert the fake `defineXML` saw a `hostfwd=` arg with an in-range ssh port).
  - the recording injector's `.inject` was called once after agent-ready with the passed `bootstrap_pubkey`.
  - when `config.ssh_parity_active` is False → no ssh arg rendered, injector not called.
  - when `bootstrap_pubkey is None` → injector not called even if parity active.
  - injector raising `CategorizedError(PROVISIONING_FAILURE)` propagates from `provision` (domain left defined).

- [ ] **Step 2: Run — expect FAIL.**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_provisioning.py -q`

- [ ] **Step 3: Implement.**
  - `__init__`: add `bootstrap_injector: _Injector | None = None`; default `RemoteBootstrapKeyInjector()`. Define a local `_Injector` Protocol with `inject(domain, pubkey) -> None`.
  - `provision`: remove `del overlay_customizers` (still ignore it — `del overlay_customizers` stays, as remote uses the guest-agent path, not overlay files). Accept `bootstrap_pubkey`. Compute `ssh_port` when `config.ssh_parity_active`:

```python
        ssh_port = None
        if config.ssh_parity_active:
            ssh_port = allocate_gdb_port(
                used_ssh_ports(conn), own_name=domain_name,
                port_min=config.ssh_port_min, port_max=config.ssh_port_max,
            )
```

  Pass `ssh_addr=config.ssh_addr, ssh_port=ssh_port` into `_define_and_start` → `render_domain_xml`. After `wait_for_agent`, inject:

```python
        if config.ssh_parity_active and bootstrap_pubkey is not None:
            domain = conn.lookupByName(domain_name)
            self._bootstrap_injector.inject(domain, bootstrap_pubkey)
```

  - `_define_and_start`: thread `ssh_addr`/`ssh_port` params through to `render_domain_xml`. In the bounded retry, keep the gdb-port advance; the ssh port is allocated once before the loop (a hostfwd bind clash surfaces as a start failure and the existing gdb-advance re-defines — acceptable within `_START_ATTEMPTS`; if flaky in the live-proof, extend the loop to also advance ssh). Keep `_define_and_start` ≤100 lines / complexity ≤8 — extract an XML-render helper if needed.
  - `reprovision`: pass `bootstrap_pubkey` through to `provision` (already does for `overlay_customizers`).

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/lifecycle/provisioning.py tests/providers/remote_libvirt/lifecycle/test_provisioning.py
git commit -m "feat(remote): allocate+render SSH port and inject bootstrap key on provision"
```

---

### Task 7: Connect — real `recorded_ssh_endpoint` over TLS

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/connect.py`
- Modify: `src/kdive/providers/remote_libvirt/composition.py` (`from_env(secret_registry=…)`)
- Test: `tests/providers/remote_libvirt/lifecycle/test_connect.py`

**Interfaces:**
- Consumes: `RemoteLibvirtConfig.ssh_addr`/`ssh_parity_active`, `recorded_ssh_port_strict`, `remote_connection`, `secret_backend_from_env`.
- Produces: `recorded_ssh_endpoint(system) -> tuple[str, int] | None` that reads the live domain XML (a real production read, NOT a raising stub).

- [ ] **Step 1: Failing tests.**

```python
def test_recorded_ssh_endpoint_reads_port_from_xml():
    xml = render_domain_xml(SID, profile, pool="p", volume="v", gdb_addr="10.0.0.1",
                            gdb_port=47001, ssh_addr="10.0.0.1", ssh_port=47101)
    connect = RemoteLibvirtConnect(
        config_factory=lambda: _cfg(ssh_addr="10.0.0.1", ssh_range="47100:47199"),
        open_connection=lambda uri: _FakeConn({domain_name_for(SID): xml}),
        secret_backend_factory=lambda: _FakeBackend(),
    )
    assert connect.recorded_ssh_endpoint(SystemHandle(domain_name_for(SID))) == ("10.0.0.1", 47101)

def test_recorded_ssh_endpoint_none_when_parity_inactive():
    connect = RemoteLibvirtConnect(config_factory=lambda: _cfg())  # no ssh_addr
    assert connect.recorded_ssh_endpoint(SystemHandle("kdive-x")) is None

def test_recorded_ssh_endpoint_does_not_raise_missing_dependency():
    # Regression for the challenge finding: must be a real read, not the gdb stub shape.
    ...  # as test 1, assert no CategorizedError(MISSING_DEPENDENCY)
```

- [ ] **Step 2: Run — expect FAIL.**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_connect.py -q`

- [ ] **Step 3: Implement.** Add injected `open_connection` + `secret_backend_factory` to `RemoteLibvirtConnect.__init__` (mirroring `RemoteLibvirtLiveIntrospect`), and:

```python
    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        """Return the recorded (ssh_addr, ssh_port), or None when SSH parity is inactive (ADR-0291).

        Reads the per-System hostfwd port from the live domain XML over TLS — a real worker read
        (authorize_ssh_key/ssh_info call this on the live path), not a live_vm stub.
        """
        config = self._config_factory()
        if not config.ssh_parity_active:
            return None
        port = self._read_ssh_port(config, str(system))
        if port is None:
            return None
        return (config.ssh_addr, port)

    def _read_ssh_port(self, config: RemoteLibvirtConfig, domain_name: str) -> int | None:
        with remote_connection(
            config, self._secret_backend_factory(), open_connection=self._open_connection
        ) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return None
                raise CategorizedError("looking up domain for ssh endpoint",
                                       category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
            return recorded_ssh_port_strict(domain.XMLDesc(), operation="ssh endpoint",
                                            domain=domain_name)
```

Keep the `# pragma: no cover - live_vm` only on the real `_open_libvirt` default (the socket open), not on the parse/orchestration. Update `from_env(cls, *, secret_registry, config_factory=...)` to build `secret_backend_factory=lambda: secret_backend_from_env(registry=secret_registry)` and `composition.py:329` to `RemoteLibvirtConnect.from_env(secret_registry=secret_registry, config_factory=config_factory)`.

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/remote_libvirt/lifecycle/connect.py src/kdive/providers/remote_libvirt/composition.py tests/providers/remote_libvirt/lifecycle/test_connect.py
git commit -m "feat(remote): recorded_ssh_endpoint reads the hostfwd port over TLS"
```

---

### Task 8: `authorize_ssh_key` targets the recorded endpoint host

**Files:**
- Modify: `src/kdive/jobs/handlers/ssh_authorize.py`
- Test: `tests/jobs/handlers/test_ssh_authorize.py`

**Interfaces:**
- Produces: `build_authorize_argv(host: str, port: int, key_path: str) -> list[str]`; handler uses `host, port = endpoint`.

- [ ] **Step 1: Failing tests.**

```python
def test_build_authorize_argv_uses_given_host():
    argv = build_authorize_argv("10.0.0.1", 47101, "/tmp/k")
    assert "root@10.0.0.1" in argv
    assert "47101" in argv

def test_build_authorize_argv_local_loopback_unchanged():
    argv = build_authorize_argv("127.0.0.1", 2222, "/tmp/k")
    assert "root@127.0.0.1" in argv
```

Update any existing call-site test that used `build_authorize_argv(port, key_path)`.

- [ ] **Step 2: Run — expect FAIL.**

Run: `uv run python -m pytest tests/jobs/handlers/test_ssh_authorize.py -q`

- [ ] **Step 3: Implement.** Change `build_authorize_argv(port, key_path)` → `build_authorize_argv(host, port, key_path)`, replace `f"{_SSH_USER}@{_LOOPBACK_HOST}"` with `f"{_SSH_USER}@{host}"`. In the handler, `host, port = endpoint` and `ssh_exec(build_authorize_argv(host, port, str(key_path)), payload.public_key)`. `_LOOPBACK_HOST` stays only if still referenced elsewhere; otherwise remove it.

- [ ] **Step 4: Run — expect PASS**, then `just lint && just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/jobs/handlers/ssh_authorize.py tests/jobs/handlers/test_ssh_authorize.py
git commit -m "feat(jobs): authorize_ssh_key targets the recorded endpoint host"
```

---

### Task 9: `live_vm` two-host e2e + operator docs

**Files:**
- Create/modify: a `live_vm`-marked test under `tests/` (mirror the nearest existing remote `live_vm` test module).
- Modify: the `systems.toml` example doc + operator runbook note.

- [ ] **Step 1: Add the gated e2e test** (marker `@pytest.mark.live_vm`), skipped by default: provision a remote System with `ssh_addr`/`ssh_range` set → read `/root/.ssh/authorized_keys` via guest-agent and assert the bootstrap pubkey is present → run `authorize_ssh_key` with a throwaway agent key → SSH into `(ssh_addr, ssh_port)` with that key → teardown → assert the `system_bootstrap_keys` row is gone. Use the existing remote `live_vm` fixtures; do not un-gate.

- [ ] **Step 2: Verify it collects + skips.**

Run: `uv run python -m pytest -m live_vm --collect-only -q | rg ssh` (present, deselected under default `just test`).

- [ ] **Step 3: Docs.** Add `ssh_addr`/`ssh_range` to a `[[remote_libvirt]]` example (with an operator note: ACL `ssh_addr:ssh_range`; the ranges must not overlap `gdbstub_range` on a shared `gdb_addr`). Run `./scripts/check-doc-links.sh` and `just check-mermaid`.

- [ ] **Step 4: Commit.**

```bash
git add tests/ docs/
git commit -m "test(remote): gated two-host SSH-parity e2e + operator ssh_addr docs"
```

---

## After all tasks

- Run full `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test). Fix everything.
- Adversarial-review the branch (`/challenge --base main`) and run `security-review` (the host-key accepted-risk from ADR-0291 is re-checked there).
- Open the PR (`Closes #966`), state the two-host `live_vm` proof cannot be driven from the implementing session, and drive to green + mergeable.

## Self-review notes

- **Spec coverage:** §3 config→T1; §4.1 endpoint/XML/ports→T2,T6; §4.3 injection→T3,T6; §4.4 threading→T4,T5; §4.2 Connect real read→T7; §4.5 authorize host→T8; §6 tests spread across tasks + T9 live_vm. All spec sections mapped.
- **Type consistency:** `bootstrap_pubkey: str | None` used identically in T4/T5/T6; `recorded_ssh_port(_strict)` names consistent T2/T6/T7; `RemoteBootstrapKeyInjector.inject(domain, pubkey)` consistent T3/T6; `build_authorize_argv(host, port, key_path)` consistent T8.
- **Ordering:** T1→T2 (config feeds XML/ports), T3 (injector standalone), T4→T5 (port param then handler), T6 needs T1-4, T7 needs T1-2, T8 standalone-ish. No task depends on a later one.
