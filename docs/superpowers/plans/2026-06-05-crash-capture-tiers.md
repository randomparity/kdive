# Crash-capture tiers — Implementation Plan (Phase 0 + groundwork + Tier 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** De-risk the four host-behavior unknowns, stand up the local hardware spine, and ship Tier 0 (console capture) end-to-end so an agent can boot a kernel with `dhash_entries=1` and read the crash console.

**Architecture:** Follows the spec `docs/superpowers/specs/2026-06-05-crash-capture-tiers-design.md` and ADR-0049. The capture-method vocabulary is a domain-level enum dispatched per plane; this plan builds the shared groundwork (enum, profile `debug` block, `vmcore.fetch` method validation) and the console tier (always-on serial `<log file>`, registered as an `artifacts.*` object by the boot handler). Host-touching seams stay injected/faked in unit tests (the codebase's established `live_vm`-gate pattern); a single `test-live` gated test exercises the real host.

**Tech Stack:** Python 3.13, `uv`, `ruff`, `ty`, `pytest`; libvirt/QEMU via `qemu:///system`; Pydantic profiles; Postgres job/artifact ledger.

**Scope note:** Tier 1 (`host_dump`) and Tier 2 (`gdbstub`) are **deferred to follow-on plans** gated on Phase 0 findings — their task code depends on empirical answers to §13.2/§13.3/§13.4. This plan produces working, demoable software on its own (boot → read crash console → clean-boot baseline for A/B).

**Commands:** `just lint` · `just type` · `just test` (CI runs these individually). Live tests: `just test-live`. Single test: `uv run pytest <path>::<name> -q`.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `~/src/linux/.config` | Debug kernel config (KASAN, CRASH_DUMP, DWARF5, pvpanic, fw_cfg vmcoreinfo, KASLR off) | Create (Phase 0) |
| `docs/runbooks/crash-capture-spine.md` | Phase 0 de-risk findings (resolves §13.1–4) | Create (Phase 0) |
| `src/kdive/domain/capture.py` | `CaptureMethod` vocabulary enum + local-libvirt supported-set | Create |
| `src/kdive/profiles/provisioning.py` | `LibvirtDebugOptions`, `debug` field, optional `crashkernel` | Modify |
| `src/kdive/providers/local_libvirt/provisioning.py` | `render_domain_xml`: always-on console `<serial>`+`<log>` | Modify |
| `src/kdive/providers/local_libvirt/install.py` | `_kdump_check` method-conditional; console-log path | Modify |
| `src/kdive/mcp/tools/vmcore.py` | `vmcore.fetch` `method` arg, supported-set validation, payload/dedup | Modify |
| `src/kdive/providers/local_libvirt/retrieve.py` | `capture(system_id, method)` signature | Modify |
| Tests mirror each under `tests/…` | | Create/Modify |

---

## Phase 0 — Host setup & de-risk (no feature code; gates everything)

### Task 0.1: Generate the debug kernel `.config`

**Files:**
- Create: `~/src/linux/.config` (via a tracked fragment)
- Create: `scripts/live-vm/x86_64-debug.config` (the merge fragment, checked in)

- [ ] **Step 1: Write the config fragment**

Create `scripts/live-vm/x86_64-debug.config`:

```
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y
CONFIG_KEXEC=y
CONFIG_CRASH_DUMP=y
CONFIG_DEBUG_INFO_DWARF5=y
# CONFIG_RANDOMIZE_BASE is not set
CONFIG_PVPANIC=y
CONFIG_PVPANIC_PCI=y
CONFIG_FW_CFG_SYSFS=y
CONFIG_SERIAL_8250=y
CONFIG_SERIAL_8250_CONSOLE=y
CONFIG_VIRTIO=y
CONFIG_VIRTIO_PCI=y
CONFIG_VIRTIO_BLK=y
CONFIG_VIRTIO_CONSOLE=y
```

- [ ] **Step 2: Merge into a defconfig and normalize**

Run:
```bash
cd ~/src/linux
make x86_64_defconfig
./scripts/kconfig/merge_config.sh -m .config /home/dave/src/kdive/scripts/live-vm/x86_64-debug.config
make olddefconfig
```

- [ ] **Step 3: Verify the load-bearing symbols are set**

Run:
```bash
cd ~/src/linux
grep -E 'CONFIG_(KASAN|CRASH_DUMP|PVPANIC|FW_CFG_SYSFS|DEBUG_INFO_DWARF5)=y' .config
grep -E 'CONFIG_RANDOMIZE_BASE' .config
```
Expected: the first five print `=y`; `RANDOMIZE_BASE` prints `# CONFIG_RANDOMIZE_BASE is not set`.

- [ ] **Step 4: Commit the fragment**

```bash
cd /home/dave/src/kdive
git add scripts/live-vm/x86_64-debug.config
git commit -m "chore(live-vm): add x86_64 debug kernel config fragment"
```
(The generated `~/src/linux/.config` is outside this repo and is not committed.)

### Task 0.2: Define and start the `qemu:///system` storage pool

- [ ] **Step 1: Define + build + start a dir pool**

Run:
```bash
virsh -c qemu:///system pool-define-as kdive dir --target /var/lib/libvirt/images
virsh -c qemu:///system pool-build kdive
virsh -c qemu:///system pool-start kdive
virsh -c qemu:///system pool-autostart kdive
```

- [ ] **Step 2: Verify**

Run: `virsh -c qemu:///system pool-list --all`
Expected: `kdive` listed, State `active`, Autostart `yes`.

### Task 0.3: Manual spine-up — resolve §13.1–§13.4 empirically

**Files:**
- Create: `docs/runbooks/crash-capture-spine.md` (record findings + the exact XML/cmdline used)

- [ ] **Step 1: Build the kernel**

Run:
```bash
cd ~/src/linux && make -j"$(nproc)" bzImage
ls -l arch/x86_64/boot/bzImage vmlinux
```
Expected: both exist.

- [ ] **Step 2: Hand-write a probe domain XML**

Create `/tmp/kdive-probe.xml` for a direct-kernel boot of a minimal rootfs (any cloud qcow2 in the pool), with: a `<serial type='file'>`+`<log file='/tmp/kdive-probe-console.log'>`, `<panic model='pvpanic'/>`, `<on_crash>preserve</on_crash>`, the QEMU `-gdb tcp:127.0.0.1:55555` passthrough, and `<cmdline>` =
`console=ttyS0 dhash_entries=1 panic_on_oops=1 kasan.fault=panic panic=0 nokaslr root=/dev/vda`.

- [ ] **Step 3: Boot and observe (resolves §13.1 — does it panic→crashed)**

Run:
```bash
virsh -c qemu:///system create /tmp/kdive-probe.xml
sleep 20
virsh -c qemu:///system domstate kdive-probe
grep -iE 'd_lookup|KASAN|Kernel panic|BUG' /tmp/kdive-probe-console.log | head
```
Record: did `domstate` report `crashed`? Did the console name `__d_lookup()`? (§13.1)

- [ ] **Step 4: Dump the frozen domain (resolves §13.4 — virsh dump on crashed-state)**

Run:
```bash
virsh -c qemu:///system dump --memory-only kdive-probe /tmp/kdive-probe.vmcore
echo "exit=$?"; ls -l /tmp/kdive-probe.vmcore; head -c4 /tmp/kdive-probe.vmcore | xxd
```
Record: exit status 0? ELF magic `7f454c46`? If it rejects a crashed-state domain, note that Tier 1 must switch to pause-on-panic (§13.4 contingency).

- [ ] **Step 5: Check the dump for the build-id note (resolves §13.3 — vmcoreinfo)**

Run:
```bash
readelf -n /tmp/kdive-probe.vmcore 2>/dev/null | grep -iA2 'VMCOREINFO\|build' | head
drgn -c /tmp/kdive-probe.vmcore -e 'print(prog["UTS_RELEASE"])' 2>&1 | head
```
Record: is `VMCOREINFO` present with a build-id? (§13.3) If absent, Tier 1 needs a host_dump-specific provenance fallback.

- [ ] **Step 6: Attach the gdbstub (smoke for Tier 2)**

Run: `gdb -ex 'target remote 127.0.0.1:55555' -ex 'bt' -ex 'detach' -ex 'quit' ~/src/linux/vmlinux`
Record: did it attach and backtrace?

- [ ] **Step 7: Clean up and record findings**

Run: `virsh -c qemu:///system destroy kdive-probe`
Write `docs/runbooks/crash-capture-spine.md` with a table: §13.1/§13.3/§13.4 → confirmed / adjusted (+ the exact mechanism that worked). Commit:
```bash
git add docs/runbooks/crash-capture-spine.md && git commit -m "docs(runbook): crash-capture spine de-risk findings"
```

> **Gate:** Phase 0 findings finalize the Tier-1/Tier-2 plans. If §13.4 forced pause-on-panic, or §13.3 found no build-id note, note it — those follow-on plans branch on it. Phase 1 and Phase 2 below do **not** depend on these answers.

---

## Phase 1 — Shared groundwork

### Task 1.1: The `CaptureMethod` vocabulary + supported-set

**Files:**
- Create: `src/kdive/domain/capture.py`
- Test: `tests/domain/test_capture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/domain/test_capture.py`:
```python
"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod


def test_vocabulary_has_four_methods() -> None:
    assert {m.value for m in CaptureMethod} == {"console", "host_dump", "gdbstub", "kdump"}


def test_local_libvirt_supports_three_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert LOCAL_LIBVIRT_SUPPORTED == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
    assert CaptureMethod.KDUMP not in LOCAL_LIBVIRT_SUPPORTED
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/domain/test_capture.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.domain.capture`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/kdive/domain/capture.py`:
```python
"""The provider-agnostic crash-capture method vocabulary (ADR-0049 Decision 1)."""

from __future__ import annotations

from enum import StrEnum


class CaptureMethod(StrEnum):
    """A capture verb; each provider maps it to a mechanism (or rejects it)."""

    CONSOLE = "console"
    HOST_DUMP = "host_dump"
    GDBSTUB = "gdbstub"
    KDUMP = "kdump"


LOCAL_LIBVIRT_SUPPORTED: frozenset[CaptureMethod] = frozenset(
    {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
)
"""The methods local-libvirt realizes today; `kdump` joins via #115."""
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/domain/test_capture.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/domain/capture.py tests/domain/test_capture.py
git commit -m "feat(domain): add CaptureMethod vocabulary + local-libvirt supported-set"
```

### Task 1.2: Profile `debug` block + optional `crashkernel`

**Files:**
- Modify: `src/kdive/profiles/provisioning.py:82-104` (`LibvirtProfile`)
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/profiles/test_provisioning.py`:
```python
def test_debug_block_defaults_to_disabled() -> None:
    profile = ProvisioningProfile.parse(_valid())
    debug = profile.provider.local_libvirt.debug
    assert debug.preserve_on_crash is False
    assert debug.gdbstub is False


def test_debug_flags_parse_when_present() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"preserve_on_crash": True, "gdbstub": True}
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.debug.preserve_on_crash is True
    assert profile.provider.local_libvirt.debug.gdbstub is True


def test_debug_block_rejects_unknown_key() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"bogus": True}
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_crashkernel_is_optional() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["crashkernel"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.crashkernel is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/profiles/test_provisioning.py -q -k "debug or crashkernel_is_optional"`
Expected: FAIL — `debug` attribute missing / `crashkernel` required.

- [ ] **Step 3: Implement the schema change**

In `src/kdive/profiles/provisioning.py`, add above `LibvirtProfile`:
```python
class LibvirtDebugOptions(_ProfileBase):
    """Per-System debug provisioning flags (ADR-0049 Decision 3).

    Bound at provision/boot; declare which capture methods the System is
    provisioned for. ``preserve_on_crash`` adds a pvpanic device +
    ``<on_crash>preserve>``; ``gdbstub`` adds the QEMU ``-gdb`` argument.
    """

    preserve_on_crash: bool = False
    gdbstub: bool = False
```
Then in `LibvirtProfile`, change `crashkernel` and add `debug`:
```python
    crashkernel: NonEmptyStr | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    ssh_credential_ref: NonEmptyStr | None = None
    debug: LibvirtDebugOptions = Field(default_factory=LibvirtDebugOptions)
```

- [ ] **Step 4: Run to verify they pass (and no regression)**

Run: `uv run pytest tests/profiles/test_provisioning.py -q`
Expected: PASS (all, including the pre-existing `test_crashkernel_is_present`, which still supplies `crashkernel`).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py
git commit -m "feat(profiles): add libvirt debug flags; make crashkernel optional"
```

### Task 1.3: `vmcore.fetch` gains `method` with supported-set validation

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve.py:157` (`capture` signature)
- Modify: `src/kdive/mcp/tools/vmcore.py:127-149,202-215,337-341`
- Test: `tests/mcp/test_vmcore_tools.py` (create if absent), `tests/providers/local_libvirt/test_retrieve.py`

- [ ] **Step 1: Write the failing test for the capture signature**

Append to `tests/providers/local_libvirt/test_retrieve.py` (create the file with the existing imports if absent):
```python
from uuid import uuid4

from kdive.domain.capture import CaptureMethod
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve


def test_capture_selects_host_dump_seam() -> None:
    seen: dict[str, object] = {}

    def fake_host_dump(system_id):  # noqa: ANN001
        seen["method"] = "host_dump"
        return b"\x7fELFcore-bytes"

    retr = LocalLibvirtRetrieve(
        tenant="t",
        store_factory=_fake_store_factory(),  # defined in this test module's helpers
        wait_for_vmcore=lambda _sid: (_ for _ in ()).throw(AssertionError("kdump seam used")),
        read_vmcore_build_id=lambda _b: "bid",
        extract_redacted=lambda _b: b"dmesg",
        host_dump_capture=fake_host_dump,
    )
    retr.capture(uuid4(), CaptureMethod.HOST_DUMP)
    assert seen["method"] == "host_dump"
```
(`_fake_store_factory` returns a fake `_StorePort` whose `put_artifact` returns a stub `StoredArtifact`; mirror the existing retrieve tests' fake store.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_retrieve.py::test_capture_selects_host_dump_seam -q`
Expected: FAIL — `capture()` takes 1 positional arg / no `host_dump_capture` param.

- [ ] **Step 3: Implement method-dispatch in `capture`**

In `retrieve.py`, add a `host_dump_capture` seam to `__init__`/`from_env` (defaulting to a `_real_host_dump_capture` stub raising `MISSING_DEPENDENCY`, mirroring the other live seams), and change `capture`:
```python
    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a core via ``method``; store raw + redacted; return refs + build-id."""
        if method is CaptureMethod.HOST_DUMP:
            data = self._host_dump_capture(system_id)
        else:  # CaptureMethod.KDUMP
            data = self._wait_for_vmcore(system_id)
        if data is None:
            raise CategorizedError(
                "no complete core appeared within the capture window",
                category=ErrorCategory.READINESS_FAILURE,
                details={"system_id": str(system_id)},
            )
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, "vmcore", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id, "vmcore-redacted", self._extract_redacted(data), Sensitivity.REDACTED
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)
```
Add the import `from kdive.domain.capture import CaptureMethod`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_retrieve.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the tool boundary**

Append to `tests/mcp/test_vmcore_tools.py` (create if absent, importing the helpers the other mcp tests use to build a `RequestContext` + crashed System):
```python
import pytest

from kdive.domain.errors import ErrorCategory


@pytest.mark.asyncio
async def test_fetch_rejects_unsupported_method(pool, operator_ctx, crashed_system):
    from kdive.mcp.tools.vmcore import fetch_vmcore

    resp = await fetch_vmcore(pool, operator_ctx, system_id=str(crashed_system), method="kdump")
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value


@pytest.mark.asyncio
async def test_fetch_records_method_on_payload(pool, operator_ctx, crashed_system):
    from kdive.mcp.tools.vmcore import fetch_vmcore

    resp = await fetch_vmcore(pool, operator_ctx, system_id=str(crashed_system), method="host_dump")
    assert resp.status != "error"
    # dedup key + payload carry the method (asserted via the enqueued job row in the fixture).
```
(Reuse the project's existing mcp-test fixtures for `pool`/`operator_ctx`/`crashed_system`; if none exist, build them from `tests/mcp/conftest.py` patterns.)

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_vmcore_tools.py -q`
Expected: FAIL — `fetch_vmcore` has no `method` kwarg.

- [ ] **Step 7: Implement the tool-boundary change**

In `mcp/tools/vmcore.py`:
- Add `from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod`.
- Change `fetch_vmcore` to accept `method: str = "host_dump"`; parse it to `CaptureMethod` (a bad value → `_config_error`); reject `method not in LOCAL_LIBVIRT_SUPPORTED` or a non-core method (`method not in {HOST_DUMP, KDUMP}`) with `_config_error`.
- Thread `method.value` into the job payload (`{"system_id": system_id, "method": method.value}`) and the dedup key (`f"{system_id}:capture_vmcore:{method.value}"`).
- In `capture_handler`, read `method = CaptureMethod(job.payload["method"])` and call `retriever.capture(system_id, method)`.
- In `register`, add the `method` arg to the `vmcore_fetch` tool wrapper (`Literal["host_dump", "kdump"]`, default `"host_dump"`).

- [ ] **Step 8: Run all touched tests**

Run: `uv run pytest tests/mcp/test_vmcore_tools.py tests/providers/local_libvirt/test_retrieve.py -q`
Expected: PASS.

- [ ] **Step 9: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/mcp/tools/vmcore.py src/kdive/providers/local_libvirt/retrieve.py \
        tests/mcp/test_vmcore_tools.py tests/providers/local_libvirt/test_retrieve.py
git commit -m "feat(vmcore): add capture method arg + supported-set validation"
```

> **Note:** The *provisioned-for* check (`host_dump` requires `preserve_on_crash`) needs the boundary to resolve the System's profile; it lands with Tier 1 (which introduces `preserve_on_crash`'s effect). Here the supported-set + core-method validation is sufficient and tested.

---

## Phase 2 — Tier 0: console capture

### Task 2.1: `render_domain_xml` adds the always-on console + log

**Files:**
- Modify: `src/kdive/providers/local_libvirt/provisioning.py:200-225` (`render_domain_xml`)
- Test: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/local_libvirt/test_provisioning.py`:
```python
from uuid import UUID

# Parse with defusedxml (XXE-safe), matching install.py's _safe_fromstring; stdlib ET
# parsing is vulnerable to XXE/billion-laughs even on self-rendered strings in tests.
from defusedxml.ElementTree import fromstring as safe_fromstring

from kdive.providers.local_libvirt.provisioning import console_log_path, render_domain_xml


def test_domain_xml_has_serial_console_with_log() -> None:
    sid = UUID("00000000-0000-0000-0000-0000000000aa")
    root = safe_fromstring(render_domain_xml(sid, _profile()))  # _profile(): a parsed valid profile
    serial = root.find("./devices/serial[@type='file']")
    assert serial is not None
    log = serial.find("log")
    assert log is not None
    assert log.get("file") == str(console_log_path(sid))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_provisioning.py::test_domain_xml_has_serial_console_with_log -q`
Expected: FAIL — no `serial` element / `console_log_path` undefined.

- [ ] **Step 3: Implement the console device**

In `provisioning.py`, add a module-level helper and a serial device in `render_domain_xml` (after the `disk`, before `metadata`):
```python
_CONSOLE_DIR = "/var/lib/kdive/console"


def console_log_path(system_id: UUID) -> Path:
    """The deterministic host path the System's serial console tees to."""
    return Path(_CONSOLE_DIR) / f"{system_id}.log"
```
```python
    serial = ET.SubElement(devices, "serial", type="file")
    ET.SubElement(serial, "source", path=str(console_log_path(system_id)))
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)))
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="file")
    ET.SubElement(console, "target", type="serial", port="0")
```
Add `from pathlib import Path` if not present.

- [ ] **Step 4: Run to verify it passes (and no XML regression)**

Run: `uv run pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/provisioning.py \
        tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(provisioning): render an always-on serial console with a log tee"
```

### Task 2.2: Register the console as a `redacted` artifact on boot-window close

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py:233-251` (`_await_ready`) — add a console-read seam
- Modify: the boot handler that calls `boot()` (locate via `rg -n "\.boot\(" src/kdive/mcp src/kdive/jobs`) — register the artifact in a `finally`
- Test: `tests/providers/local_libvirt/test_install.py`, the boot-handler test module

- [ ] **Step 1: Write the failing test for the console-read seam**

Append to `tests/providers/local_libvirt/test_install.py`:
```python
from pathlib import Path

from kdive.providers.local_libvirt.install import read_console_log


def test_read_console_log_returns_bytes(tmp_path: Path) -> None:
    log = tmp_path / "sys.log"
    log.write_bytes(b"[ 0.0] Kernel panic - __d_lookup\n")
    assert b"__d_lookup" in read_console_log(log)


def test_read_console_log_missing_is_empty(tmp_path: Path) -> None:
    assert read_console_log(tmp_path / "absent.log") == b""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k read_console_log`
Expected: FAIL — `read_console_log` undefined.

- [ ] **Step 3: Implement the console-read seam**

In `install.py`:
```python
def read_console_log(path: Path) -> bytes:
    """Read the System's console log; an absent log is empty (boot may not have written)."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k read_console_log`
Expected: PASS.

- [ ] **Step 5: Write the failing boot-handler test (registration in a `finally`)**

In the boot-handler test module, add a test: a fake `Booter.boot` that raises `BOOT_TIMEOUT`; assert the handler still inserts a `redacted` console artifact row (owner_kind `systems`, key ending `/console`) and re-raises. (Model it on the existing capture-handler test that asserts artifact insertion.)

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest <boot-handler test path> -q -k console`
Expected: FAIL — no console artifact inserted.

- [ ] **Step 7: Implement registration in the handler**

Wrap the handler's `boot()` call in `try/.../finally`: in the `finally`, read `console_log_path(system_id)` via `read_console_log`, run it through `Redactor().redact_text(... )` (decode/replace), `register_artifact_row(...)` with name `"console"` and `Sensitivity.REDACTED`, and `ARTIFACTS.insert`. Use the same `register_artifact_row` + `ARTIFACTS.insert` pattern as `_finalize_capture` in `vmcore.py`.

- [ ] **Step 8: Run the handler tests**

Run: `uv run pytest <boot-handler test path> -q`
Expected: PASS — registration fires on both the success and the raised paths.

- [ ] **Step 9: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/install.py <handler file> tests/...
git commit -m "feat(boot): register the console log as a redacted artifact on window close"
```

### Task 2.3: `_kdump_check` becomes method-conditional (unblocks console/non-kdump boots)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/install.py:141-160` (`install`)
- Test: `tests/providers/local_libvirt/test_install.py`

- [ ] **Step 1: Write the failing test**

```python
def test_install_skips_kdump_check_for_non_kdump(monkeypatch) -> None:  # noqa: ANN001
    # A non-kdump boot must not require a kdump capture path.
    # Build a LocalLibvirtInstall with fakes; kdump_check raises if called.
    ...  # assert install(..., method=CaptureMethod.CONSOLE) does not call kdump_check
```
(Fill with the existing `test_install.py` fake-seam construction; the assertion is that `kdump_check` is not invoked when `method != kdump`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q -k kdump_check_for_non_kdump`
Expected: FAIL — `install()` has no `method` param / always calls `kdump_check`.

- [ ] **Step 3: Implement the conditional**

Add a `method: CaptureMethod` param to `install`; guard the check:
```python
        if method is CaptureMethod.KDUMP and not self._kdump_check(system_id):
            raise CategorizedError(
                "kdump capture service/initramfs not present on the staged System",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id)},
            )
```
Thread `method` from the install handler (read from the Run/build profile; default `host_dump`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/providers/local_libvirt/test_install.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/local_libvirt/install.py tests/providers/local_libvirt/test_install.py
git commit -m "feat(install): gate the kdump preflight on method=kdump"
```

### Task 2.4: Live end-to-end (gated)

**Files:**
- Test: `tests/integration/live_stack/test_console_capture.py` (marked for `just test-live`)

- [ ] **Step 1: Write the gated test**

A `@pytest.mark.live_vm` (match the project's existing live marker) test that: provisions a System on `qemu:///system` from a profile with the debug `.config` kernel, boots with `dhash_entries=1`, and asserts the registered console artifact contains `__d_lookup`. Also a clean-boot variant asserting the console contains no panic (the A/B baseline).

- [ ] **Step 2: Run it on the host**

Run: `just test-live -k console_capture`
Expected: PASS — vulnerable boot's console names `__d_lookup`; clean boot's does not.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/live_stack/test_console_capture.py
git commit -m "test(live): console capture A/B on dhash_entries=1"
```

---

## Self-Review

- **Spec coverage (this plan's scope):** §2 method vocabulary → Task 1.1; §5 profile `debug`/optional `crashkernel` → Task 1.2; §4 `vmcore.fetch` method + supported-set → Task 1.3; §6.1 always-on console XML → Task 2.1; §4/§6.1/§11 console registration in `finally` → Task 2.2; §8 method-conditional `_kdump_check` → Task 2.3; §12.1/§12.4 console A/B → Task 2.4. §13.1–4 → Phase 0. **Deferred (own plans):** §6.2/§7/§8 panic-escalation + host_dump capture (Tier 1); §6.3/§9 gdbstub port allocation + resolver (Tier 2); §5 provisioned-for check (lands with Tier 1).
- **Placeholder scan:** Phase 0 steps are exact commands. Tasks 1.1–2.1 carry complete code. Tasks 2.2/2.3/2.4 reference existing test-fixture patterns (`_finalize_capture`, the live marker) rather than inline-duplicating large fixtures — the engineer must `rg` the boot-handler path (given) and reuse the named patterns; this is a deliberate pointer, not a missing detail.
- **Type consistency:** `CaptureMethod` (Task 1.1) is the type used in `capture()` (1.3), `install()` (2.3), and the handlers; `console_log_path()` (2.1) is consumed by `read_console_log` (2.2). `read_vmcore_build_id`/`extract_redacted` seam names match `retrieve.py`.

---

## Follow-on plans (after Phase 0)

- **Tier 1 — host_dump:** panic-escalation cmdline (§8), `_host_dump_capture` seam (§7, branch on §13.4), `_real_read_vmcore_build_id`/`_real_extract_redacted` (branch on §13.3), `_await_ready` crashed-success outcome + `READY→CRASHED` handler transition (§8), provisioned-for check (§5).
- **Tier 2 — gdbstub:** atomic port allocation + persistence on the System (needs a migration) + release on teardown (§6.3), QEMU `-gdb` passthrough in `render_domain_xml`, `_real_resolve_endpoint` reading the persisted port (§9).
