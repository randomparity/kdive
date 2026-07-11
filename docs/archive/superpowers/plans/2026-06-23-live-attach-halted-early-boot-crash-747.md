# Live attach to a halted early-boot crash — Implementation Plan (#747)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `debug.start_session(run, "gdbstub")` attach to a Run whose boot ended in an early-boot kernel panic that left a reachable gdbstub, by recording a new `crashed_halted_live` boot outcome and admitting it at the precondition gate.

**Architecture:** Make `preserve_on_crash` actually render its pvpanic + `<on_crash>preserve</on_crash>` (Component 1); add a provider-neutral `ProfilePolicy.gdbstub_provisioned` seam (Component 2); in the worker boot handler, on a `READINESS_FAILURE` that shows a kernel-panic console signature on a `gdbstub`-provisioned System whose stub is reachable, record a succeeded `boot` step with `boot_outcome="crashed_halted_live"` (Component 3); admit that outcome to the `gdbstub` transport (reject `drgn-live`) in `_attach_preconditions` (Component 4); prove it end-to-end with a `live_vm` test on this host (Component 5).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; libvirt domain XML via `xml.etree.ElementTree`; the existing `rsp_reachable` RSP probe and `Connector.open_transport` seam.

**Spec:** [`../specs/2026-06-23-live-attach-halted-early-boot-crash-design.md`](../specs/2026-06-23-live-attach-halted-early-boot-crash-design.md)
**ADR:** [`../../adr/0233-live-attach-halted-early-boot-crash.md`](../../adr/0233-live-attach-halted-early-boot-crash.md)
**Branch:** `feat/live-attach-halted-crash-747` (already created off `main`)

## Global Constraints

- Absolute imports only (no `..`); Google-style docstrings on non-trivial public APIs; ≤100 lines/function, cyclomatic ≤8; line length 100.
- Plain factual prose in code comments/docstrings — no "critical", "robust", "comprehensive", "elegant"; "Milestone" never "Sprint".
- Pick the most specific existing `ErrorCategory` (`domain/errors.py`); never invent strings. Failure `ToolResponse`s carry an `error_category`; success ones must not.
- No new migration (the new `boot_outcome` is a schemaless value in `run_steps.result`). No new dependency.
- Redaction: all guest/console output passes the existing redactor before persistence; no raw guest text in any envelope or recorded result.
- New fixed user-facing strings interpolate no run state, guest output, host, or resource id (no-leak seam, ADR-0123).

## Guardrails (run before every commit)

- `just lint` — `ruff check` + `ruff format --check`
- `just type` — `ty check` (whole tree, src + tests). Locally this can fail on unrelated drgn/libguestfs unused-ignore divergence; if so run `SKIP=ty just lint test` and rely on CI for `ty` (note in PR). Prefer fixing if it is your code.
- focused tests per task (commands given inline)
- full suite once before first push: `just test`

## File Structure

- `src/kdive/providers/local_libvirt/lifecycle/xml.py` — add pvpanic device + `<on_crash>preserve</on_crash>` rendering (Component 1).
- `src/kdive/providers/core/runtime.py` — add `gdbstub_provisioned` to the `ProfilePolicy` Protocol (Component 2).
- `src/kdive/providers/local_libvirt/profile_policy.py`, `.../remote_libvirt/profile_policy.py`, `.../fault_inject/profile_policy.py` — implement it (Component 2).
- `src/kdive/jobs/handlers/runs_boot.py` — generic panic matcher + the `crashed_halted_live` recording branch; thread `connector` + `profile_policy` into `_run_boot_and_capture_outcome` (Component 3).
- `src/kdive/mcp/tools/debug/sessions_lifecycle.py` — admit `crashed_halted_live` (Component 4).
- Tests mirror each under `tests/`.

---

### Task 1: Render pvpanic + `<on_crash>preserve</on_crash>` for `preserve_on_crash`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py` (`render_domain_xml`, after the `devices` block ~line 78-87 and before `return`)
- Test: `tests/providers/local_libvirt/test_provisioning.py`

**Interfaces:**
- Consumes: `render_domain_xml(system_id, profile, *, disk_path, gdb_port=None, ssh_port=None) -> str` (unchanged signature); `profile.provider.local_libvirt.debug.preserve_on_crash: bool`.
- Produces: domain XML containing `<panic model="pvpanic"/>` under `<devices>` and `<on_crash>preserve</on_crash>` as a `<domain>` child, iff `preserve_on_crash` is set.

- [ ] **Step 1: Write the failing tests.** Add to `tests/providers/local_libvirt/test_provisioning.py` (reuse the module's existing profile builder; if it builds a profile via a helper, set `debug.preserve_on_crash`). Concrete bodies:

```python
def test_render_includes_pvpanic_and_on_crash_preserve_when_preserve_set() -> None:
    profile = _profile_with(preserve_on_crash=True)  # existing helper / dict→ProvisioningProfile.parse
    xml = render_domain_xml(uuid4(), profile, disk_path="/d.qcow2", gdb_port=1234)
    root = ET.fromstring(xml)
    assert root.findtext("on_crash") == "preserve"
    assert any(p.get("model") == "pvpanic" for p in root.findall("./devices/panic"))


def test_render_omits_pvpanic_and_on_crash_when_preserve_unset() -> None:
    profile = _profile_with(preserve_on_crash=False)
    xml = render_domain_xml(uuid4(), profile, disk_path="/d.qcow2")
    root = ET.fromstring(xml)
    assert root.find("on_crash") is None
    assert root.findall("./devices/panic") == []
```

If `_profile_with` does not exist, build the profile the same way the file's existing `render_domain_xml` tests do (search the file for `render_domain_xml(` and copy that construction, toggling `debug.preserve_on_crash`).

- [ ] **Step 2: Run, verify red.** `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -k "pvpanic or on_crash" -q` → FAIL (`on_crash` is None / no panic device).

- [ ] **Step 3: Implement.** In `render_domain_xml`, after the `metadata` block and before the `if section.debug.gdbstub:` block, add:

```python
    if section.debug.preserve_on_crash:
        # pvpanic notifies the host on guest panic; <on_crash>preserve</on_crash> holds the
        # domain (vCPUs stopped) instead of destroying it, so a crashed boot stays inspectable
        # (host_dump capture and the #747 live-gdb attach). ADR-0049 / ADR-0233.
        ET.SubElement(devices, "panic", model="pvpanic")
        ET.SubElement(domain, "on_crash").text = "preserve"
```

(`devices` and `domain` are already local variables in `render_domain_xml`.)

- [ ] **Step 4: Run, verify green.** `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -k "pvpanic or on_crash" -q` → PASS. Then `just lint` (no banned words; line length).

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/xml.py tests/providers/local_libvirt/test_provisioning.py
git commit -m "fix(local-libvirt): render pvpanic + on_crash=preserve for preserve_on_crash

preserve_on_crash documented adding a pvpanic device + <on_crash>preserve</on_crash>
but render_domain_xml emitted neither. Render them so a panicked domain is held
(vCPUs stopped) for host_dump and live-gdb attach (ADR-0233, #747).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `ProfilePolicy.gdbstub_provisioned` seam

**Files:**
- Modify: `src/kdive/providers/core/runtime.py` (`ProfilePolicy` Protocol, ~after `capture_method` at line 75)
- Modify: `src/kdive/providers/local_libvirt/profile_policy.py`, `src/kdive/providers/remote_libvirt/profile_policy.py`, `src/kdive/providers/fault_inject/profile_policy.py`
- Test: `tests/providers/local_libvirt/test_profile_policy.py` (create if absent), plus one assertion each for remote/fault in their existing policy tests (or the same new file).

**Interfaces:**
- Produces TWO provider-neutral predicates on the `ProfilePolicy` port, both consumed by Task 3 (so the generic boot handler never reads a provider-specific profile section):
  - `gdbstub_provisioned(self, profile) -> bool` — True iff the System has a gdbstub `-gdb` endpoint, **independent** of `capture_method` precedence (a kdump-primary System that also set `gdbstub` returns True).
  - `host_dump_provisioned(self, profile) -> bool` — True iff the System can produce a host-side memory dump on a preserved crash (local: `preserve_on_crash`; remote: False; fault: False). Used only to compute `available_capture`.

- [ ] **Step 1: Write failing tests.** Create `tests/providers/local_libvirt/test_profile_policy.py`:

```python
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy  # confirm class name

def _profile(gdbstub: bool, crashkernel: str | None = None) -> ProvisioningProfile:
    # Build via ProvisioningProfile.parse mirroring an existing local profile fixture;
    # set provider.local_libvirt.debug.gdbstub and (optionally) .crashkernel.
    ...

def test_gdbstub_provisioned_true_when_flag_set() -> None:
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(_profile(gdbstub=True)) is True

def test_gdbstub_provisioned_true_even_when_kdump_is_primary() -> None:
    p = _profile(gdbstub=True, crashkernel="256M")
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(p) is True  # not masked by capture_method

def test_gdbstub_provisioned_false_when_flag_unset() -> None:
    assert LocalLibvirtProfilePolicy().gdbstub_provisioned(_profile(gdbstub=False)) is False

def test_host_dump_provisioned_tracks_preserve_on_crash() -> None:
    pol = LocalLibvirtProfilePolicy()
    assert pol.host_dump_provisioned(_profile(gdbstub=True, preserve_on_crash=True)) is True
    assert pol.host_dump_provisioned(_profile(gdbstub=True, preserve_on_crash=False)) is False
```

(Extend `_profile` to take a `preserve_on_crash: bool = False` kwarg setting `debug.preserve_on_crash`.)

(Confirm the concrete class name with `rg -n "class .*ProfilePolicy" src/kdive/providers/local_libvirt/profile_policy.py`. Build `_profile` by copying an existing local-libvirt profile construction from `tests/providers/local_libvirt/`.)

- [ ] **Step 2: Run, verify red.** `uv run python -m pytest tests/providers/local_libvirt/test_profile_policy.py -q` → FAIL (`AttributeError: gdbstub_provisioned`).

- [ ] **Step 3: Implement.** In `runtime.py` `ProfilePolicy` Protocol add both:

```python
    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Whether the System has a gdbstub endpoint (independent of capture_method)."""
        ...

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        """Whether a host-side memory dump is available on a preserved crash."""
        ...
```

In `local_libvirt/profile_policy.py`:

```python
    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        return profile.provider.local_libvirt.debug.gdbstub

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        return profile.provider.local_libvirt.debug.preserve_on_crash
```

In `remote_libvirt/profile_policy.py` (remote unconditionally provisions gdbstub — ADR-0083, `capture_method` returns GDBSTUB absent crashkernel; the remote section has no preserve flag, so no host_dump):

```python
    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        return True

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        return False
```

In `fault_inject/profile_policy.py` (the test/failure provider has neither endpoint):

```python
    def gdbstub_provisioned(self, profile: ProvisioningProfile) -> bool:
        return False

    def host_dump_provisioned(self, profile: ProvisioningProfile) -> bool:
        return False
```

- [ ] **Step 4: Run, verify green + types.** `uv run python -m pytest tests/providers/local_libvirt/test_profile_policy.py -q` → PASS. `just type` (the Protocol + 3 impls must satisfy structurally). `just lint`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/providers/core/runtime.py src/kdive/providers/local_libvirt/profile_policy.py src/kdive/providers/remote_libvirt/profile_policy.py src/kdive/providers/fault_inject/profile_policy.py tests/providers/local_libvirt/test_profile_policy.py
git commit -m "feat(providers): add ProfilePolicy gdbstub/host_dump provisioned seam

Provider-neutral predicates 'this System has a gdbstub endpoint' (independent
of capture_method precedence) and 'host_dump available on a preserved crash',
so the generic boot handler detects the live-attach fallback and builds
available_capture without reading a provider-specific profile section
(ADR-0233, #747).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Record `crashed_halted_live` in the boot handler

**Files:**
- Modify: `src/kdive/jobs/handlers/runs_boot.py` (`_run_boot_and_capture_outcome` ~line 191-227; `boot_handler` call site ~line 256-259)
- Test: `tests/jobs/handlers/test_runs_boot.py`

**Interfaces:**
- Consumes: `ProfilePolicy.gdbstub_provisioned` and `ProfilePolicy.host_dump_provisioned` (Task 2 — the handler reads **no** provider-specific profile section); `Connector.open_transport(system: SystemHandle, kind) -> TransportHandle` (raises `CategorizedError` when unreachable); `domain_name_for` (import from `kdive.providers.local_libvirt.naming` — confirm with `rg -n "def domain_name_for" src/`); `search_text`; `_capture_console_artifact`; `_record_boot_audit`; `SYSTEMS` (`kdive.db.repositories`).
- Sole caller of `_run_boot_and_capture_outcome` is `boot_handler` (`runs_boot.py:257`); no test calls it directly, so the signature change touches exactly one call site (verified by `rg -n "_run_boot_and_capture_outcome" src/ tests/`).
- Produces: a `boot` `run_steps.result` dict `{system_id, boot_outcome: "crashed_halted_live", evidence_kind: "console", evidence_artifact_id, available_capture: [...]}` recorded as a **succeeded** step; consumed by Task 4. Allowed `available_capture` strings are `CaptureMethod` values `"gdbstub"`, `"console"`, `"host_dump"`.

> Probe mechanism: do **not** re-resolve the gdb port. Call `connector.open_transport(SystemHandle(domain_name_for(system_id)), "gdbstub")` inside a `try`; a returned handle ⇒ reachable, a `CategorizedError` ⇒ unreachable. `open_transport` internally runs the read-only `rsp_reachable` probe and the loopback guard, holds no session row, and `close_transport` is a no-op — so this does not consume the single-attach slot.

- [ ] **Step 1: Write failing tests — the generic panic matcher.** Add to `tests/jobs/handlers/test_runs_boot.py` (mirrors the existing `_expected_crash_matches` tests at lines 41-88):

```python
def test_generic_panic_matches_on_kernel_panic_line() -> None:
    console = b"[ 1.45] Kernel panic - not syncing: VFS: Unable to mount root fs\n"
    assert runs_boot._generic_panic_matches(console) is True

def test_generic_panic_no_match_on_clean_console() -> None:
    assert runs_boot._generic_panic_matches(b"[ 0.5] booting\n[ 2.0] systemd\n") is False

def test_generic_panic_fails_closed_when_search_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise ArtifactSearchInputError("forced")
    monkeypatch.setattr(runs_boot, "search_text", _boom)
    assert runs_boot._generic_panic_matches(b"Kernel panic - not syncing\n") is False
```

- [ ] **Step 2: Run, verify red.** `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k generic_panic -q` → FAIL (`_generic_panic_matches` undefined).

- [ ] **Step 3: Implement the matcher.** In `runs_boot.py`, near `_expected_crash_matches` (line 150):

```python
# A generic, provider-neutral kernel-panic signature for an undeclared early-boot crash.
# Errs toward the safe side: a console without this line is treated as no-crash (the boot
# abandons to FAILED), never as a spurious live-debuggable crash (ADR-0233, #747).
_GENERIC_PANIC_PATTERN = "Kernel panic - not syncing"


def _generic_panic_matches(redacted_console: bytes) -> bool:
    """True iff the redacted console shows a generic kernel panic; fails closed on bad input."""
    try:
        return (
            search_text(
                redacted_console,
                pattern=_GENERIC_PANIC_PATTERN,
                before_lines=0,
                after_lines=0,
                max_matches=1,
            ).match_count
            > 0
        )
    except ArtifactSearchInputError:
        return False
```

- [ ] **Step 4: Run, verify green.** `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k generic_panic -q` → PASS.

- [ ] **Step 5: Write failing tests — the recording branch.** There is no end-to-end harness for `_run_boot_and_capture_outcome` to mirror (the file only unit-tests `_expected_crash_matches`), so test the two pure/seamed helpers as units plus the recording helper with monkeypatched IO seams — do **not** touch the disk or object store.

Pure unit (no IO):

```python
class _Pol:
    def __init__(self, gdbstub: bool, host_dump: bool) -> None:
        self._g, self._h = gdbstub, host_dump
    def gdbstub_provisioned(self, _profile: object) -> bool: return self._g
    def host_dump_provisioned(self, _profile: object) -> bool: return self._h

def test_available_capture_without_preserve() -> None:
    assert runs_boot._available_capture(_Pol(True, False), cast(ProvisioningProfile, object())) == ["gdbstub", "console"]

def test_available_capture_with_preserve() -> None:
    assert runs_boot._available_capture(_Pol(True, True), cast(ProvisioningProfile, object())) == ["gdbstub", "console", "host_dump"]
```

`_gdbstub_reachable` with a fake connector (no real socket):

```python
class _Conn:
    def __init__(self, raises: bool) -> None: self._raises = raises
    def open_transport(self, _s: object, _k: object) -> object:
        if self._raises:
            raise CategorizedError("no stub", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
        return object()
    def close_transport(self, _h: object) -> None: ...

def test_gdbstub_reachable_true_when_open_succeeds() -> None:
    assert runs_boot._gdbstub_reachable(cast(Connector, _Conn(raises=False)), uuid4()) is True

def test_gdbstub_reachable_false_when_open_raises() -> None:
    assert runs_boot._gdbstub_reachable(cast(Connector, _Conn(raises=True)), uuid4()) is False
```

The recording helper, monkeypatching the IO seams (`_capture_console_artifact` → a fake `_ConsoleArtifact` carrying chosen bytes; `SYSTEMS.get` → a fake System whose `provisioning_profile` parses; `_record_boot_audit` → a recorder). Use one async test parametrized over the matrix below; assert on the returned dict / `None` and that the audit recorder fired only on the recorded case:
  - gdbstub + panic-line + reachable ⇒ `boot_outcome == "crashed_halted_live"`, `available_capture == ["gdbstub", "console"]` (no preserve) / `["gdbstub", "console", "host_dump"]` (preserve); audit recorded.
  - gdbstub + panic-line + **unreachable** (fake connector raises) ⇒ returns `None` (caller re-raises → FAILED).
  - gdbstub + **no panic line** + reachable ⇒ returns `None` (panic signature, not the probe, is the crash signal).
  - **no gdbstub** ⇒ returns `None`.
  - Plus an integration-level assertion that a declared+matched expected crash still yields `expected_crash_observed` (the existing branch is untouched — add/keep a test exercising that path).

(`_ConsoleArtifact` is the `NamedTuple` in `runs_boot.py` with `id`, `object_key`, `data`; build a fake System the same way other `tests/jobs/` tests stub `SYSTEMS.get`, or via a tiny stand-in with a `.provisioning_profile` that `ProvisioningProfile.parse` accepts.)

- [ ] **Step 6: Run, verify red.** `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k "crashed_halted or available_capture" -q` → FAIL.

- [ ] **Step 7: Implement the branch.** Change `_run_boot_and_capture_outcome` to accept `connector: Connector` and `profile_policy: ProfilePolicy`, and update `boot_handler` to pass `binding.runtime.connector` and `binding.runtime.profile_policy`. In the `except CategorizedError as exc:` block, after the existing expected-crash `return` and before `raise`, add:

```python
        if exc.category is ErrorCategory.READINESS_FAILURE:
            crash = await _record_crash_halted_live(
                conn, job_ctx, run, system_id, connector, profile_policy,
                secret_registry, artifact_store,
            )
            if crash is not None:
                return crash
        raise
```

Add the helper (keep it ≤100 lines / complexity ≤8 — extract `_available_capture` and `_gdbstub_reachable`):

```python
def _available_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    # Provider-neutral: built from ProfilePolicy predicates only, never a provider-specific
    # profile section, so this generic handler stays correct for every provider (ADR-0233).
    methods = [CaptureMethod.GDBSTUB.value, CaptureMethod.CONSOLE.value]
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    return methods


def _gdbstub_reachable(connector: Connector, system_id: UUID) -> bool:
    try:
        connector.open_transport(SystemHandle(domain_name_for(system_id)), "gdbstub")
    except CategorizedError:
        return False
    return True


async def _record_crash_halted_live(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    system_id: UUID,
    connector: Connector,
    profile_policy: ProfilePolicy,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> dict[str, Any] | None:
    """Record crashed_halted_live iff gdbstub-provisioned, console shows a panic, stub reachable."""
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        return None
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    if not profile_policy.gdbstub_provisioned(profile):
        return None
    artifact = await _capture_console_artifact(conn, system_id, secret_registry, artifact_store)
    if artifact is None or not artifact.data or not _generic_panic_matches(artifact.data):
        return None
    if not await asyncio.to_thread(_gdbstub_reachable, connector, system_id):
        return None
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "crashed_halted_live",
        "evidence_kind": "console",
        "evidence_artifact_id": str(artifact.id),
        "available_capture": _available_capture(profile_policy, profile),
    }
```

Imports to add to `runs_boot.py`: `Connector`, `SystemHandle` from `kdive.providers.ports`; `ProfilePolicy` from `kdive.providers.core.runtime`; `ProvisioningProfile` from `kdive.profiles.provisioning`; `CaptureMethod` from `kdive.domain.capture`; `domain_name_for` from `kdive.providers.shared.runtime_paths`; `SYSTEMS` from `kdive.db.repositories`; `UUID`/`Any` already imported. The profile load mirrors `sessions_lifecycle` (`SYSTEMS.get` then `ProvisioningProfile.parse(system.provisioning_profile)`).

- [ ] **Step 8: Run, verify green + guardrails.** `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q` → PASS. `just lint`; `just type`.

- [ ] **Step 9: Commit.**

```bash
git add src/kdive/jobs/handlers/runs_boot.py tests/jobs/handlers/test_runs_boot.py
git commit -m "feat(runs): record crashed_halted_live for a halted early-boot panic

On READINESS_FAILURE, when the System is gdbstub-provisioned, the redacted
console shows a kernel panic, and the stub answers a read-only RSP probe,
record a succeeded boot step with boot_outcome=crashed_halted_live (+ boot
audit + available_capture) instead of abandoning it. The console-panic
signature is the crash signal; the probe only confirms reachability
(ADR-0233, #747).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Admit `crashed_halted_live` at the precondition gate

**Files:**
- Modify: `src/kdive/mcp/tools/debug/sessions_lifecycle.py` (`_attach_preconditions` ~line 490-497; add a detail constant near line 79-82)
- Test: `tests/mcp/debug/test_debug_tools.py`

**Interfaces:**
- Consumes: the `boot_outcome == "crashed_halted_live"` result (Task 3); `transport: DebugTransportKind`.
- Produces: admit for `gdbstub` (fall through to System-ready/occupied checks); `configuration_error` reject for `drgn-live`.

- [ ] **Step 1: Write failing tests.** In `tests/mcp/debug/test_debug_tools.py`, mirror the existing `boot`-step-row insertion (line ~288 `VALUES (%s, 'boot', 'succeeded', %s)`), inserting a result with `boot_outcome="crashed_halted_live"`:

```python
def test_start_session_admits_gdbstub_for_crashed_halted_live(migrated_url: str) -> None:
    # seed: ready System, SUCCEEDED Run, boot step succeeded w/ result boot_outcome=crashed_halted_live
    ...
    resp = await _start_session(pool, _ctx(), run_id=run_id, transport="gdbstub")
    assert resp.status == "ok"  # session opened (use the same assertion the happy-path test uses)

def test_start_session_rejects_drgn_live_for_crashed_halted_live(migrated_url: str) -> None:
    ...
    resp = await _start_session(pool, _ctx(), run_id=run_id, transport="drgn-live")
    assert resp.error_category is ErrorCategory.CONFIGURATION_ERROR
    assert resp.data["reason"] == "crashed_not_ssh_debuggable"

def test_crashed_halted_live_system_stays_ready(migrated_url: str) -> None:
    # assert the seeded System row is still SystemState.READY after start_session (no transition)
    ...
```

(Copy the seeding helper from `test_start_session_attaches_and_row_is_live` at line 333 and change the inserted `boot` result JSON.)

- [ ] **Step 2: Run, verify red.** `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -k crashed_halted -q` → FAIL (currently the result has no `expected_crash_observed`, so it falls through and admits gdbstub already — but the drgn-live reject is missing; the gdbstub test may pass spuriously, so assert it explicitly AND keep the drgn-live red test as the gate).

- [ ] **Step 3: Implement.** Add the detail constant near line 82:

```python
_CRASHED_HALTED_LIVE_DRGN_DETAIL = (
    "run crashed during early boot and is halted with a live gdbstub; attach over gdbstub. "
    "drgn-live needs a running in-guest sshd, which a halted crash does not have"
)
```

In `_attach_preconditions`, after the `expected_crash_observed` branch (line 490-497), add:

```python
    if boot_result.get("boot_outcome") == "crashed_halted_live" and transport == _DRGN_LIVE:
        return ToolResponse.failure(
            str(run.id),
            ErrorCategory.CONFIGURATION_ERROR,
            detail=_CRASHED_HALTED_LIVE_DRGN_DETAIL,
            suggested_next_actions=["debug.start_session"],
            data={"reason": "crashed_not_ssh_debuggable"},
        )
```

The `gdbstub` case needs no new branch: a `crashed_halted_live` outcome is not `expected_crash_observed`, so it already falls through to the System-ready/occupied checks and is admitted. Add a comment at that fall-through making the intent explicit so a future reader does not re-add a blanket crash reject.

- [ ] **Step 4: Run, verify green + guardrails.** `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q` → PASS. `just lint`; `just type`.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/mcp/tools/debug/sessions_lifecycle.py tests/mcp/debug/test_debug_tools.py
git commit -m "feat(debug): admit crashed_halted_live to gdbstub, reject drgn-live

A crashed_halted_live boot outcome (halted early-boot panic with a live
stub) is admitted to the gdbstub transport; drgn-live is rejected with a
configuration_error naming gdbstub, because a halted guest has no sshd
(ADR-0233, #747).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `live_vm` end-to-end proof (run on this host)

**Files:**
- Modify/Create: a `live_vm`-marked test under `tests/providers/local_libvirt/` (mirror `tests/providers/local_libvirt/test_connect.py` live setup) or `tests/integration/`.

**Interfaces:**
- Consumes: the full local-libvirt provision→boot→`debug.start_session` path; a kernel that panics early in boot.

**Panic induction (the crux — confirm against the provisioning path first).** The spike forced the panic by booting the kernel with no mountable root (`VFS: Unable to mount root fs` → `Kernel panic - not syncing`). Reproduce that deterministically through kdive in this priority order:
1. **Kernel cmdline override**, if the local profile/boot path exposes one (grep first: `rg -n "cmdline|append|<kernel>|<cmdline>|root=" src/kdive/providers/local_libvirt/`). If a direct-kernel boot renders `<cmdline>`, provision with `root=/dev/does-not-exist panic=0` so the kernel halts (not reboots) on the VFS panic.
2. **Deliberately incompatible rootfs**, if no cmdline hook exists: point the profile's rootfs at an empty/garbage qcow2 the kernel cannot mount, yielding the same early-boot VFS panic.
3. Only if neither is reachable, add a **test-only** cmdline-append seam used solely by this `live_vm` test — it must not relax any production gate and must be off by default.

Whichever path: the System sets `provider.local_libvirt.debug = {gdbstub: true, preserve_on_crash: true}`, so the panic preserves the domain with a live stub. State in the test docstring which induction path was used.

- [ ] **Step 1: Write the gated test.** A `@pytest.mark.live_vm` test that provisions the System above, boots it (expects `READINESS_FAILURE` resolved to a succeeded `boot` step with `boot_outcome == "crashed_halted_live"`), asserts `debug.start_session(run, "gdbstub")` returns an `ok` live session, and `end_session` detaches. Follow the existing live harness for stack/fixtures; skip cleanly when absent. **Teardown (required):** wrap provisioning in a try/finally that tears down the System / releases the Allocation and asserts the libvirt domain is gone (`virsh -c qemu:///system list --all` shows no `kdive-<system_id>` domain), so a live run leaves no residual QEMU process or domain on the host.

- [ ] **Step 2: Run it for real on this host.** Bring up the live stack (runbook `docs/operating/runbooks/live-stack.md`; worker=root needs `sudo` + `KDIVE_KERNEL_SRC`), then `just test-live -k crashed_halted`. Capture the session evidence (a register read in the panic frame is a strong proof — mirror the spike's `bt`). If it fails, debug on the host — this is the falsifiable gate; do not stub past it. After the run, confirm no leaked domain remains.

- [ ] **Step 3: Commit.**

```bash
git add tests/providers/local_libvirt/<live_test>.py
git commit -m "test(live): prove gdbstub live-attach to a preserved early-boot panic (#747)"
```

---

### Task 6: Finalize — full suite, docs, branch review, PR

- [ ] **Step 1:** Regenerate any doc surfaces if a new tool/field were added (none here — no new MCP tool; `runs.get` exposes the free-form `data`/result, no schema/snapshot change). Run `just check-mermaid`.
- [ ] **Step 2:** Full suite once: `just test` (or `SKIP=ty just lint test` if the local drgn/libguestfs `ty` divergence bites; note in PR).
- [ ] **Step 3:** Adversarial branch review: `/challenge --base main` with the step-6 focus; address findings; then `/security-review`.
- [ ] **Step 4:** Fold fixups, push, open PR against `main` ending with `Closes #747`; drive to green CI + `MERGEABLE`/`CLEAN`.

## Self-Review

- **Spec coverage:** Component 1 → Task 1; Component 2 (probe) → Task 3 (via `open_transport`); Component 3 (record) → Task 3, with the `gdbstub_provisioned` seam in Task 2; Component 4 (admit) → Task 4; Component 5 (surface options) → `available_capture` recorded in Task 3 (read off the `boot` step result by `runs.get`'s existing free-form `data`); console-panic crash signal, audit parity, degraded-store, System-READY invariant all carried in Tasks 3-4 tests; `live_vm` proof → Task 5.
- **Type consistency:** `gdbstub_provisioned` + `host_dump_provisioned` (Protocol + 3 impls each) ↔ called in Task 3; the generic boot handler reads **no** provider-specific profile section (only policy predicates), so it is correct for remote/fault Systems; `_record_crash_halted_live`/`_available_capture`/`_gdbstub_reachable` defined and called in Task 3; `crashed_halted_live` string identical across Tasks 3-5; `available_capture` strings are `CaptureMethod` values.
- **Placeholders:** the `_profile_with` / live-harness builders are the only "copy the existing fixture" references; each names the exact existing test to copy from. No "TBD"/"add error handling".
