# Host-side Network Traffic Capture (#1258) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `control.capture_traffic`, a fixed-duration contributor tool that captures host-side guest network traffic on a `READY` local-libvirt System via QEMU `filter-dump`, storing a Run-owned `SENSITIVE` pcap egressed through `artifacts.fetch_raw`.

**Architecture:** Mirrors the `diagnostic_sysrq` job + `vmcore.fetch` egress. A thin `TrafficCapturer` provider port (`attach`/`detach` primitives over `libvirt_qemu.qemuMonitorCommand`) lets the worker handler own a bounded size-poll loop; the pcap is written by QEMU under `/var/lib/kdive/pcap/<system_id>/`, read back by the worker, optionally BPF-trimmed with `tcpdump`, and stored `SENSITIVE`/`retention_class="pcap"`/`owner_kind="runs"`. Remote-libvirt is fail-closed via a `supports_traffic_capture` capability flag (ADR-0378 pattern).

**Tech Stack:** Python 3.14, `uv`, FastMCP, psycopg (async), `libvirt`/`libvirt_qemu`, boto3 object store, pytest.

**Spec:** `docs/superpowers/specs/2026-07-18-capture-traffic-1258-design.md`
**ADR:** `docs/adr/0384-host-side-traffic-capture.md`

## Global Constraints

- **Branch:** `feat/capture-traffic-1258` (already created off `main`). Never work on `main`.
- **Guardrails:** `just lint` (ruff), `just type` (ty, whole tree), `just test`; full gate `just ci`. Run `just lint type` after every task; `just test` on the task's tests; `just ci` before the PR.
- **Style:** ruff line length 100, lint set `E,F,I,UP,B,SIM`. Absolute imports only. Google-style docstrings on public APIs. No `ADR-NNNN` strings in any agent-facing text (tool wrapper docstrings, `Field(description=...)`) — they live only in module docstrings / code comments (guard: `tests/mcp/core/test_no_adr_leak.py`).
- **Doc-style:** no "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- **Numeric bounds** in any `Field`/docstring must be f-string-interpolated from the enforcing constant, never hardcoded (guard: `test_agent_facing_numeric_bounds_are_interpolated_not_hardcoded`).
- **Error taxonomy:** use existing `ErrorCategory` values (`CONTROL_FAILURE`, `CONFIGURATION_ERROR`, `INFRASTRUCTURE_FAILURE`); never invent strings.
- **Bounds (constants, exact):** `duration_s` 1–300 default 30; `max_bytes` 1 MiB–512 MiB default 64 MiB (`1048576`–`536870912`, default `67108864`); `snaplen` 1–262144 default 128; `capture_filter` ≤ 1024 chars, printable ASCII.
- **QOM id:** `kdive-dump-<job_id>`. **Artifact name:** `pcap-<job_id>`. **Netdev:** `SYSTEM_SSH_NETDEV_ID` (= `"kdivessh"`). **Host dir:** `/var/lib/kdive/pcap/<system_id>/<job_id>.pcap`.
- **Migration:** `0072_capture_traffic_job_kind.sql` (forward-only, additive; drop-and-recreate `jobs_kind_check` keeping the name).

---

## File Structure

- `src/kdive/db/schema/0072_capture_traffic_job_kind.sql` — **Create**. Widen `jobs_kind_check`.
- `src/kdive/domain/operations/jobs.py` — **Modify**. `JobKind.CAPTURE_TRAFFIC` + add to `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
- `src/kdive/jobs/payloads.py` — **Modify**. `CaptureTrafficPayload(RunPayload)` + register.
- `src/kdive/providers/shared/runtime_paths.py` — **Modify**. `pcap_dir(system_id)` / `pcap_path(system_id, job_id)` + `read_pcap_bytes` (readability-wall).
- `src/kdive/providers/local_libvirt/lifecycle/xml.py` — **Modify**. Extract `SYSTEM_SSH_NETDEV_ID` constant.
- `src/kdive/providers/ports/traffic.py` — **Create**. `TrafficCapturer` port.
- `src/kdive/providers/core/runtime.py` — **Modify**. `traffic_capturer` field + `ProviderSupport.supports_traffic_capture`.
- `src/kdive/providers/local_libvirt/lifecycle/traffic_capture.py` — **Create**. `LocalLibvirtTrafficCapture`.
- `src/kdive/providers/local_libvirt/composition.py` — **Modify**. Wire capturer + flag.
- `src/kdive/artifacts/pcap_count.py` — **Create**. Endianness-aware pcap record counter.
- `src/kdive/jobs/handlers/control/capture_traffic.py` — **Create**. Worker handler.
- `src/kdive/jobs/assembly.py` — **Modify**. Register the handler.
- `src/kdive/security/artifacts/bpf_filter.py` — **Create**. `hygiene_check` + `validate_bpf` (tcpdump -d) + `trim_pcap`.
- `src/kdive/mcp/tools/lifecycle/control/registrar.py` — **Modify**. Admission handler + `@app.tool`.
- `src/kdive/mcp/exposure.py` — **Modify**. `_TOOL_SCOPES` entry.
- `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` — **Modify**. `RawAsset.PCAP` + `artifact_id` param + `_resolve_key` branch.
- `src/kdive/artifacts/read_model.py` — **Modify**. `raw_pcap_key(conn, run_id, artifact_id)`.
- `src/kdive/mcp/tools/lifecycle/systems/view.py` — **Modify**. Surface `supports_traffic_capture`.
- `src/kdive/jobs/handlers/systems.py` — **Modify**. Teardown `rmtree` of the pcap dir.
- `tests/mcp/core/test_tool_docs.py` — **Modify**. `_BEHAVIOR_TESTS_BY_TOOL` entry.
- Generated docs (regenerated, not hand-edited): `just rbac-matrix`, `just docs`, `just resources-docs`.

**Build order:** foundation (1–2) → provider seam (3–5) → helpers (6–7) → worker handler (8) → admission tool (9) → egress + discoverability + teardown (10–12) → regen + guardrails (13) → live proof (14).

---

### Task 1: Migration + JobKind + cancelable set

**Files:**
- Create: `src/kdive/db/schema/0072_capture_traffic_job_kind.sql`
- Modify: `src/kdive/domain/operations/jobs.py` (`JobKind`, `CONTRIBUTOR_CANCELABLE_JOB_KINDS`)
- Test: existing `tests/db/test_migrate.py` (SQL↔enum tie), `tests/domain/` job-kind tests if present

**Interfaces:**
- Produces: `JobKind.CAPTURE_TRAFFIC` (value `"capture_traffic"`), admitted by `jobs_kind_check`.

- [ ] **Step 1: Write the migration.** Create `0072_capture_traffic_job_kind.sql`. Copy the `jobs_kind_check` drop/recreate from `0071_system_snapshots.sql` and append `'capture_traffic'` to the `CHECK (kind IN (...))` list (keep all existing values incl. `snapshot`/`restore`/`delete_snapshot`). Header comment (no `ADR-NNNN` ban applies to SQL comments, but keep it factual):

```sql
-- 0072_capture_traffic_job_kind.sql — host-side network traffic capture job kind (#1258, ADR-0384).
-- Forward-only (ADR-0015), additive. Widens jobs.kind to admit the `capture_traffic` job kind:
-- control.capture_traffic enqueues one job whose handler runs a QEMU filter-dump on a ready
-- local-libvirt guest's netdev for a bounded window, storing a Run-owned SENSITIVE pcap.
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie (test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic'));
```

- [ ] **Step 2: Add the enum member + cancelable entry.** In `jobs.py`, add `CAPTURE_TRAFFIC = "capture_traffic"` to `JobKind` (after `DELETE_SNAPSHOT`), and add `JobKind.CAPTURE_TRAFFIC` to the `CONTRIBUTOR_CANCELABLE_JOB_KINDS` frozenset.

- [ ] **Step 3: Run the SQL↔enum tie test.** Run: `uv run python -m pytest tests/db/test_migrate.py -q`. Expected: PASS (the check now lists every `ACTIVE_JOB_KINDS` value). If a test enumerates job kinds and fails, it is asserting the tie — update only if it hardcodes an expected set that must now include `capture_traffic`.

- [ ] **Step 4: Guardrails + commit.** Run `just lint type`; `git add src/kdive/db/schema/0072_capture_traffic_job_kind.sql src/kdive/domain/operations/jobs.py` and commit `feat(1258): add capture_traffic job kind + migration 0072`.

---

### Task 2: `CaptureTrafficPayload`

**Files:**
- Modify: `src/kdive/jobs/payloads.py`
- Test: `tests/jobs/test_payloads.py` (or the existing payload test module)

**Interfaces:**
- Consumes: `JobKind.CAPTURE_TRAFFIC` (Task 1), `RunPayload` (existing, carries `run_id: str`).
- Produces: `CaptureTrafficPayload(RunPayload)` with fields `duration_s: int`, `max_bytes: int`, `snaplen: int`, `capture_filter: str | None = None`.

- [ ] **Step 1: Write the failing test.** In the payload test module:

```python
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.payloads import CaptureTrafficPayload, model_for_kind  # model_for_kind: existing lookup

def test_capture_traffic_payload_roundtrips_and_is_registered():
    p = CaptureTrafficPayload(run_id="11111111-1111-1111-1111-111111111111",
                              duration_s=30, max_bytes=67108864, snaplen=128, capture_filter="tcp port 80")
    assert p.run_id.endswith("111")
    assert model_for_kind(JobKind.CAPTURE_TRAFFIC) is CaptureTrafficPayload
    # capture_filter is optional
    CaptureTrafficPayload(run_id=p.run_id, duration_s=5, max_bytes=1048576, snaplen=1)
```

(Check the real accessor name for the payload-model lookup in `payloads.py` — it may be `_ACTIVE_PAYLOAD_MODELS[kind]` directly rather than a `model_for_kind` helper; use whichever the module exposes.)

- [ ] **Step 2: Run it, verify it fails.** Run: `uv run python -m pytest tests/jobs/test_payloads.py -k capture_traffic -q`. Expected: FAIL (`ImportError`/`KeyError`).

- [ ] **Step 3: Implement.** In `payloads.py`, after `CaptureVmcorePayload`, add:

```python
class CaptureTrafficPayload(RunPayload):
    """A `capture_traffic` job: the Run + capture window/size/snaplen and an optional BPF filter.

    Run-addressed (like `CaptureVmcorePayload`): the pcap is owned by the Run under investigation;
    the worker resolves the bound System from ``run_id`` to reach the live guest.
    """

    duration_s: int
    max_bytes: int
    snaplen: int
    capture_filter: str | None = None
```

Register it in `_ACTIVE_PAYLOAD_MODELS`: `JobKind.CAPTURE_TRAFFIC: CaptureTrafficPayload,`.

- [ ] **Step 4: Run, verify pass.** Run: `uv run python -m pytest tests/jobs/test_payloads.py -k capture_traffic -q`. Expected: PASS.

- [ ] **Step 5: Guardrails + commit.** `just lint type`; commit `feat(1258): add CaptureTrafficPayload`.

---

### Task 3: `SYSTEM_SSH_NETDEV_ID` constant

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py`
- Test: existing xml render test (`tests/providers/local_libvirt/test_xml*.py`)

**Interfaces:**
- Produces: `from kdive.providers.local_libvirt.lifecycle.xml import SYSTEM_SSH_NETDEV_ID` (`= "kdivessh"`).

- [ ] **Step 1: Failing test.** Add to the xml test module:

```python
from kdive.providers.local_libvirt.lifecycle import xml as xmlmod

def test_ssh_netdev_id_is_shared_constant():
    assert xmlmod.SYSTEM_SSH_NETDEV_ID == "kdivessh"
```

- [ ] **Step 2: Run, verify fail.** Run the test; expected FAIL (`AttributeError`).

- [ ] **Step 3: Implement.** In `xml.py`, add near the top constants: `SYSTEM_SSH_NETDEV_ID = "kdivessh"`. In `_append_ssh_forward`, replace the two inline `id=kdivessh` / `netdev=kdivessh` literals with f-strings referencing `SYSTEM_SSH_NETDEV_ID` (e.g. `f"user,id={SYSTEM_SSH_NETDEV_ID},restrict=..."` and `f"virtio-net-pci,netdev={SYSTEM_SSH_NETDEV_ID},addr=0x10"`). Do **not** touch the `kdivebuild` egress-NIC literal.

- [ ] **Step 4: Run existing xml render tests, verify pass.** Run: `uv run python -m pytest tests/providers/local_libvirt/ -k xml -q`. Expected: PASS (rendered XML byte-identical — the netdev id string is unchanged).

- [ ] **Step 5: Guardrails + commit.** `just lint type`; commit `refactor(1258): extract SYSTEM_SSH_NETDEV_ID constant`.

---

### Task 4: `TrafficCapturer` port + runtime wiring

**Files:**
- Create: `src/kdive/providers/ports/traffic.py`
- Modify: `src/kdive/providers/core/runtime.py` (add `traffic_capturer` field + `ProviderSupport.supports_traffic_capture`)
- Test: `tests/providers/core/test_runtime.py` (or wherever `ProviderRuntime`/`ProviderSupport` defaults are asserted)

**Interfaces:**
- Produces:
  - `TrafficCapturer` Protocol: `attach(self, domain_name: str, *, qom_id: str, netdev_id: str, dest_path: str, snaplen: int) -> None` and `detach(self, domain_name: str, *, qom_id: str) -> None`.
  - `ProviderRuntime.traffic_capturer: TrafficCapturer | None = None`.
  - `ProviderSupport.supports_traffic_capture: bool = False`.

- [ ] **Step 1: Failing test.** In the runtime test module:

```python
from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport

def test_traffic_capture_is_fail_closed_by_default():
    # ProviderSupport default is False; ProviderRuntime.traffic_capturer default is None.
    assert ProviderSupport.__dataclass_fields__["supports_traffic_capture"].default is False
    assert ProviderRuntime.__dataclass_fields__["traffic_capturer"].default is None
```

- [ ] **Step 2: Run, verify fail.** Expected FAIL (`KeyError` on the missing field).

- [ ] **Step 3: Implement the port.** Create `providers/ports/traffic.py`:

```python
"""Traffic-capture provider port (ADR-0384): host-side pcap of a running guest's netdev.

Thin primitives — the worker handler owns the bounded poll loop and cancellation, so a provider
only attaches/detaches a capture sink keyed on the provider domain name (DB-free, like Controller).
"""

from __future__ import annotations

from typing import Protocol


class TrafficCapturer(Protocol):
    """Attach/detach a host-side packet-capture sink on a running guest's netdev."""

    def attach(
        self, domain_name: str, *, qom_id: str, netdev_id: str, dest_path: str, snaplen: int
    ) -> None:
        """Start capturing ``netdev_id`` into the libpcap file ``dest_path`` (snaplen bytes/packet).

        Idempotent: any pre-existing sink under ``qom_id`` is removed first (tolerating not-found).
        Raises ``CategorizedError(CONTROL_FAILURE)`` on a monitor failure other than not-found.
        """
        ...

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the capture sink ``qom_id`` (tolerating not-found)."""
        ...
```

- [ ] **Step 4: Wire the runtime.** In `runtime.py`: import `TrafficCapturer`; add `traffic_capturer: TrafficCapturer | None = None` to `ProviderRuntime` (alongside `snapshotter`); add `supports_traffic_capture: bool = False` to `ProviderSupport` (alongside `supports_snapshots`). Keep the scoped `unresolved-import` ignore pattern if `ty` needs it (it will not — this is a pure-Python protocol).

- [ ] **Step 5: Run, verify pass.** Run the runtime test; expected PASS. Run `just type` (whole tree) to confirm no `ty` break from the new optional field.

- [ ] **Step 6: Commit.** `just lint type`; commit `feat(1258): add TrafficCapturer port + fail-closed capability`.

---

### Task 5: `LocalLibvirtTrafficCapture` + composition wiring

**Files:**
- Create: `src/kdive/providers/local_libvirt/lifecycle/traffic_capture.py`
- Modify: `src/kdive/providers/local_libvirt/composition.py`
- Test: `tests/providers/local_libvirt/test_traffic_capture.py` (Create)

**Interfaces:**
- Consumes: `TrafficCapturer` (Task 4), `SYSTEM_SSH_NETDEV_ID` (Task 3).
- Produces: `LocalLibvirtTrafficCapture` implementing `TrafficCapturer`, built by `from_env()` like `LocalLibvirtControl`. Composition sets `traffic_capturer=LocalLibvirtTrafficCapture.from_env()` and `supports_traffic_capture=True`.

**Reference to mirror exactly:** `src/kdive/providers/local_libvirt/lifecycle/control.py` (connection factory, `_open`/`_lookup`/`_close`, `_control_failure`). QMP passthrough shape: `src/kdive/providers/remote_libvirt/connection/transport_reset.py` (how `libvirt_qemu.qemuMonitorCommand` is called + only `libvirt.libvirtError` is caught).

- [ ] **Step 1: Failing tests.** Create `tests/providers/local_libvirt/test_traffic_capture.py`. Use a fake connection/domain that records the JSON commands passed to `qemuMonitorCommand`:

```python
import json
import libvirt
import pytest
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.traffic_capture import LocalLibvirtTrafficCapture

class _FakeDomain:
    def __init__(self, monitor):
        self._monitor = monitor  # callable(cmd_json) -> str

class _FakeConn:
    def __init__(self, domain): self._domain = domain
    def lookupByName(self, name): return self._domain
    def close(self): return 0

def _capturer(monitor):
    dom = _FakeDomain(monitor)
    return LocalLibvirtTrafficCapture(connect=lambda: _FakeConn(dom)), dom

def test_attach_deletes_stale_then_adds_filter_dump():
    seen = []
    def monitor(dom, cmd, flags):
        seen.append(json.loads(cmd)); return "{}"
    cap, _ = _capturer(monitor)
    # patch libvirt_qemu.qemuMonitorCommand to call `monitor` (see impl note)
    cap.attach("kdive-x", qom_id="kdive-dump-J", netdev_id="kdivessh",
               dest_path="/var/lib/kdive/pcap/S/J.pcap", snaplen=128)
    assert seen[0]["execute"] == "object-del" and seen[0]["arguments"]["id"] == "kdive-dump-J"
    add = seen[1]
    assert add["execute"] == "object-add"
    args = add["arguments"]
    assert args["qom-type"] == "filter-dump"
    assert args["id"] == "kdive-dump-J"
    assert args["netdev"] == "kdivessh"
    assert args["file"] == "/var/lib/kdive/pcap/S/J.pcap"
    assert args["maxlen"] == 128

def test_attach_swallows_object_not_found_on_first_run(monkeypatch):
    calls = []
    def monitor(dom, cmd, flags):
        c = json.loads(cmd); calls.append(c["execute"])
        if c["execute"] == "object-del":
            raise libvirt.libvirtError("Device 'kdive-dump-J' not found")  # QMP DeviceNotFound
        return "{}"
    cap, _ = _capturer(monitor)
    cap.attach("kdive-x", qom_id="kdive-dump-J", netdev_id="kdivessh",
               dest_path="/p.pcap", snaplen=128)  # must NOT raise
    assert calls == ["object-del", "object-add"]

def test_attach_reraises_other_monitor_error_as_control_failure():
    def monitor(dom, cmd, flags):
        raise libvirt.libvirtError("some other monitor failure")
    cap, _ = _capturer(monitor)
    with pytest.raises(CategorizedError) as ei:
        cap.attach("kdive-x", qom_id="q", netdev_id="kdivessh", dest_path="/p", snaplen=128)
    assert ei.value.category is ErrorCategory.CONTROL_FAILURE

def test_detach_issues_object_del():
    seen = []
    def monitor(dom, cmd, flags):
        seen.append(json.loads(cmd)); return "{}"
    cap, _ = _capturer(monitor)
    cap.detach("kdive-x", qom_id="kdive-dump-J")
    assert seen[0]["execute"] == "object-del" and seen[0]["arguments"]["id"] == "kdive-dump-J"
```

Note: the impl calls `libvirt_qemu.qemuMonitorCommand(domain, cmd, 0)`. In the test, `monkeypatch.setattr("kdive.providers.local_libvirt.lifecycle.traffic_capture.libvirt_qemu.qemuMonitorCommand", monitor)` (or inject a `monitor` callable — see Step 3, prefer a small injected seam for testability, mirroring `connect`).

- [ ] **Step 2: Run, verify fail.** Run: `uv run python -m pytest tests/providers/local_libvirt/test_traffic_capture.py -q`. Expected FAIL (module missing).

- [ ] **Step 3: Implement.** Create `traffic_capture.py`, mirroring `control.py`'s structure. Key points:
  - Constructor `__init__(self, *, connect: Connect, monitor: Callable = libvirt_qemu.qemuMonitorCommand)` — inject `monitor` so tests don't need real libvirt (default is the real passthrough).
  - `from_env()` reads `LIBVIRT_URI` and returns `cls(connect=lambda: libvirt.open(uri))`.
  - `attach`: `_open` → `_lookup` domain → `self._object_del(dom, qom_id, tolerate_missing=True)` → `self._object_add(dom, {...filter-dump...})` → `_close`.
  - `_object_del(dom, qom_id, *, tolerate_missing)`: build `{"execute":"object-del","arguments":{"id":qom_id}}`, call `self._monitor(dom, json.dumps(cmd), 0)`; on `libvirt.libvirtError` as `exc`: if `tolerate_missing and _is_not_found(exc)` → log + return; else raise `_control_failure("deleting capture filter on", domain_name)`.
  - `_is_not_found(exc)`: match the QMP not-found signature in the message string — `s = str(exc).lower(); return "not found" in s or "devicenotfound" in s` (QMP `object-del` on a missing id yields "object 'X' not found" / `DeviceNotFound`; `qemuMonitorCommand` carries no `VIR_ERR_*` code, so match text).
  - `_object_add(dom, args)`: `{"execute":"object-add","arguments":{"qom-type":"filter-dump","id":qom_id,"netdev":netdev_id,"file":dest_path,"maxlen":snaplen}}`; on error raise `_control_failure("adding capture filter on", domain_name)`.
  - `detach`: `_open` → `_lookup` → `_object_del(dom, qom_id, tolerate_missing=True)` → `_close`.
  - `_control_failure(verb, domain_name)`: `CategorizedError(f"libvirt error {verb} domain", category=ErrorCategory.CONTROL_FAILURE, details={"domain": domain_name})` (copy from `control.py:193-199`).
  - Import: `from kdive.providers.ports.traffic import TrafficCapturer as TrafficCapturer` and `import libvirt_qemu` (add the scoped `unresolved-import` ignore if `ty` flags the C-extension import, matching the `libvirt`/`drgn` pattern noted in AGENTS.md).

- [ ] **Step 4: Run, verify pass.** Run the test file; expected PASS (all four).

- [ ] **Step 5: Wire composition.** In `composition.py`, mirror the `controller = LocalLibvirtControl.from_env()` line: add `traffic_capturer=LocalLibvirtTrafficCapture.from_env()` to the `ProviderRuntime(...)` construction and set the `ProviderSupport(...)` `supports_traffic_capture=True`. (Find the existing `supports_snapshots=True` set in the same file for the exact call site.)

- [ ] **Step 6: Guardrails + commit.** `just lint type`; run `uv run python -m pytest tests/providers/local_libvirt/test_traffic_capture.py -q`; commit `feat(1258): local-libvirt filter-dump TrafficCapturer`.

---

### Task 6: Endianness-aware pcap record counter + host pcap paths

**Files:**
- Create: `src/kdive/artifacts/pcap_count.py`
- Modify: `src/kdive/providers/shared/runtime_paths.py` (add `pcap_dir`, `pcap_path`, `read_pcap_bytes`)
- Test: `tests/artifacts/test_pcap_count.py` (Create)

**Interfaces:**
- Produces:
  - `count_pcap_packets(data: bytes) -> int` — walks libpcap records; returns whole-record count.
  - `pcap_dir(system_id: UUID) -> Path` (= `/var/lib/kdive/pcap/<system_id>`), `pcap_path(system_id: UUID, job_id: UUID) -> Path`, `read_pcap_bytes(path: Path) -> bytes` (raises the ADR-0223 `CONFIGURATION_ERROR` w/ `WORKER_READABILITY_REMEDIATION` on `PermissionError`, empty on missing).

- [ ] **Step 1: Failing tests.** Create `tests/artifacts/test_pcap_count.py`:

```python
import struct
from kdive.artifacts.pcap_count import count_pcap_packets

def _hdr(magic): return struct.pack("=IHHiIII", magic, 2, 4, 0, 0, 65535, 1)
def _rec_le(n): return struct.pack("<IIII", 0, 0, n, n) + b"\x00" * n
def _rec_be(n): return struct.pack(">IIII", 0, 0, n, n) + b"\x00" * n

def test_header_only_is_zero():
    assert count_pcap_packets(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)) == 0

def test_little_endian_two_records():
    body = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1) + _rec_le(4) + _rec_le(8)
    assert count_pcap_packets(body) == 2

def test_big_endian_two_records():
    body = struct.pack(">IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1) + _rec_be(4) + _rec_be(8)
    assert count_pcap_packets(body) == 2

def test_nanosecond_magic_counts():
    body = struct.pack("<IHHiIII", 0xa1b23c4d, 2, 4, 0, 0, 65535, 1) + _rec_le(4)
    assert count_pcap_packets(body) == 1

def test_truncated_tail_counts_whole_records_only():
    body = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1) + _rec_le(4) + b"\x00\x00\x00"
    assert count_pcap_packets(body) == 1

def test_not_a_pcap_is_zero():
    assert count_pcap_packets(b"garbage") == 0
```

- [ ] **Step 2: Run, verify fail.** Expected FAIL (module missing).

- [ ] **Step 3: Implement `pcap_count.py`.**

```python
"""Endianness-aware libpcap record counter (ADR-0384).

Reads the 4-byte magic to pick byte order (0xa1b2c3d4 native / 0xd4c3b2a1 swapped, plus the
nanosecond variants 0xa1b23c4d / 0x4d3cb2a1), then walks 16-byte record headers by ``incl_len``.
Counts only whole records: a truncated tail (e.g. a capture cut off mid-record) is ignored, so a
header-only file is zero. Non-pcap input is zero, never an exception — the count only drives a
signal, so it must never fail the capture.
"""

from __future__ import annotations

import struct

_GLOBAL_HEADER_LEN = 24
_RECORD_HEADER_LEN = 16
_LE = {0xA1B2C3D4, 0xA1B23C4D}
_BE = {0xD4C3B2A1, 0x4D3CB2A1}


def count_pcap_packets(data: bytes) -> int:
    if len(data) < _GLOBAL_HEADER_LEN:
        return 0
    magic = struct.unpack("<I", data[:4])[0]
    if magic in _LE:
        endian = "<"
    elif magic in _BE:
        endian = ">"
    else:
        return 0
    offset = _GLOBAL_HEADER_LEN
    count = 0
    while offset + _RECORD_HEADER_LEN <= len(data):
        incl_len = struct.unpack(endian + "I", data[offset + 8 : offset + 12])[0]
        end = offset + _RECORD_HEADER_LEN + incl_len
        if end > len(data):
            break  # truncated final record — not counted
        count += 1
        offset = end
    return count
```

- [ ] **Step 4: Run, verify pass.** Run: `uv run python -m pytest tests/artifacts/test_pcap_count.py -q`. Expected: PASS.

- [ ] **Step 5: Add host-path helpers.** In `runtime_paths.py`, mirroring `console_log_path`/`read_console_log`:

```python
_PCAP_DIR = "/var/lib/kdive/pcap"

def pcap_dir(system_id: UUID) -> Path:
    return Path(_PCAP_DIR) / str(system_id)

def pcap_path(system_id: UUID, job_id: UUID) -> Path:
    return pcap_dir(system_id) / f"{job_id}.pcap"

def read_pcap_bytes(path: Path) -> bytes:
    """Read a captured pcap whole; a non-root worker under qemu:///system may hit the readback wall."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError as err:
        raise CategorizedError(
            "failed to read captured pcap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"operation": "read_pcap", "path": str(path),
                     "error": type(err).__name__, "remediation": WORKER_READABILITY_REMEDIATION},
        ) from err
    except OSError as err:
        raise CategorizedError(
            "failed to read captured pcap",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "read_pcap", "path": str(path), "error": type(err).__name__},
        ) from err
```

Add a test asserting `read_pcap_bytes` on a missing path returns `b""` and on a `PermissionError` (monkeypatch `Path.read_bytes` to raise) raises `CONFIGURATION_ERROR` with the remediation string.

- [ ] **Step 6: Guardrails + commit.** `just lint type`; commit `feat(1258): pcap packet counter + host pcap path helpers`.

---

### Task 7: BPF filter hygiene + validation + trim

**Files:**
- Create: `src/kdive/security/artifacts/bpf_filter.py`
- Test: `tests/security/artifacts/test_bpf_filter.py` (Create)

**Interfaces:**
- Produces:
  - `MAX_FILTER_LEN = 1024`
  - `hygiene_reason(expr: str | None) -> str | None` — admission-time pure check; returns a reason string (`"too_long"` / `"non_printable"`) or `None` if acceptable (or `expr is None`).
  - `validate_bpf(expr: str) -> None` — runs `tcpdump -d <expr>` (compile-only), raising `CategorizedError(CONFIGURATION_ERROR, {reason: "invalid_filter", stderr})` on non-zero exit.
  - `trim_pcap(src: Path, dst: Path, expr: str) -> None` — runs `tcpdump -r <src> -w <dst> <expr>`, raising `CONFIGURATION_ERROR` on failure. Both pass `expr` as a single argv element (never a shell string).

- [ ] **Step 1: Failing tests.**

```python
import pytest
from pathlib import Path
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.artifacts import bpf_filter as bf

def test_hygiene_accepts_none_and_normal():
    assert bf.hygiene_reason(None) is None
    assert bf.hygiene_reason("tcp port 80 and host 10.0.0.5") is None

def test_hygiene_rejects_too_long():
    assert bf.hygiene_reason("a" * (bf.MAX_FILTER_LEN + 1)) == "too_long"

def test_hygiene_rejects_non_printable():
    assert bf.hygiene_reason("tcp\nport 80") == "non_printable"

def test_validate_bpf_accepts_valid(tmp_path):
    bf.validate_bpf("tcp port 80")  # tcpdump -d compiles → no raise

def test_validate_bpf_rejects_garbage():
    with pytest.raises(CategorizedError) as ei:
        bf.validate_bpf("this is not a filter )(")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ei.value.details.get("reason") == "invalid_filter"

def test_validate_bpf_metachars_are_not_shell_interpreted(tmp_path):
    # A shell metachar payload is passed as one argv element; tcpdump -d just fails to compile it.
    with pytest.raises(CategorizedError):
        bf.validate_bpf("tcp; touch /tmp/pwned")
    assert not Path("/tmp/pwned").exists()
```

(These tests exercise the real `tcpdump` binary — it is present in CI per host prereqs; if a runner lacks it, gate with `pytest.importorskip`-style `shutil.which("tcpdump")` skip. Add that skip guard to the module-level of the test.)

- [ ] **Step 2: Run, verify fail.** Expected FAIL (module missing).

- [ ] **Step 3: Implement.**

```python
"""BPF capture-filter hygiene, validation, and post-capture trim (ADR-0384).

The agent-supplied filter is the trailing pcap-filter(7) expression of a tcpdump line. It is passed
to tcpdump as a single argv element (never a shell string), validated compile-only with
``tcpdump -d`` before use, and applied after capture with ``tcpdump -r <src> -w <dst> <expr>``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

MAX_FILTER_LEN = 1024


def hygiene_reason(expr: str | None) -> str | None:
    if expr is None:
        return None
    if len(expr) > MAX_FILTER_LEN:
        return "too_long"
    if not expr.isprintable():
        return "non_printable"
    return None


def _run(args: list[str], op: str) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as err:
        raise CategorizedError(
            f"{op} failed to run", category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "invalid_filter", "error": type(err).__name__},
        ) from err
    if proc.returncode != 0:
        raise CategorizedError(
            f"{op} rejected the capture filter", category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "invalid_filter", "stderr": proc.stderr.strip()[:500]},
        )


def validate_bpf(expr: str) -> None:
    _run(["tcpdump", "-d", expr], "filter validation")


def trim_pcap(src: Path, dst: Path, expr: str) -> None:
    _run(["tcpdump", "-r", str(src), "-w", str(dst), expr], "filter trim")
```

- [ ] **Step 4: Run, verify pass.** Run: `uv run python -m pytest tests/security/artifacts/test_bpf_filter.py -q`. Expected: PASS.

- [ ] **Step 5: Guardrails + commit.** `just lint type`; commit `feat(1258): BPF filter hygiene/validate/trim`.

---

### Task 8: Worker handler `capture_traffic_handler`

**Files:**
- Create: `src/kdive/jobs/handlers/control/capture_traffic.py`
- Modify: `src/kdive/jobs/assembly.py` (register the handler)
- Test: `tests/jobs/handlers/control/test_capture_traffic.py` (Create), and the capture-loop unit inside it.

**Interfaces:**
- Consumes: `CaptureTrafficPayload` (T2), `TrafficCapturer` port (T4), `pcap_path`/`read_pcap_bytes`/`pcap_dir` (T6), `count_pcap_packets` (T6), `validate_bpf`/`trim_pcap` (T7), `SYSTEM_SSH_NETDEV_ID` (T3).
- Produces: `capture_traffic_handler(conn, job, *, resolver, artifact_store) -> str | None` (returns pcap artifact id), `register_handlers(registry, *, resolver, artifact_store)`, and a pure `run_capture_loop(...)` helper for the poll.

**Reference to mirror exactly:** `src/kdive/jobs/handlers/control/diagnostic_sysrq.py` — `_snapshot` (tx1, per-System lock, verify READY+local, resolve domain + port), the lock-free provider op, `_store_capture` (tx2, re-verify, insert-if-absent, audit), `register_handlers`, and the `ArtifactStreamRequest`/`put_stream` disk-backed write (see `jobs/handlers/artifacts/vmcore.py` `finalize_capture` + `providers/local_libvirt/retrieve.py:_put_stream` for the `SENSITIVE` stream write with `sha256_b64`). Store owner is `owner_kind="runs"`, `owner_id=run_id`.

- [ ] **Step 1: Write the capture-loop unit test (pure).**

```python
import asyncio, pytest
from kdive.jobs.handlers.control.capture_traffic import run_capture_loop, LoopResult

async def _drive(sizes, canceled_at=None, max_bytes=10_000, max_polls=5):
    calls = {"n": 0}
    async def stat():
        i = min(calls["n"], len(sizes) - 1); return sizes[i]
    async def sleep(_): calls["n"] += 1
    async def canceled():
        return canceled_at is not None and calls["n"] >= canceled_at
    return await run_capture_loop(stat=stat, sleep=sleep, canceled=canceled,
                                  max_bytes=max_bytes, max_polls=max_polls)

def test_loop_stops_at_duration():
    r = asyncio.run(_drive([100, 200, 300], max_polls=3))
    assert r.truncated is False and r.canceled is False

def test_loop_stops_at_max_bytes():
    r = asyncio.run(_drive([100, 5000, 20000], max_bytes=10_000, max_polls=9))
    assert r.truncated is True and r.canceled is False

def test_loop_stops_on_cancel():
    r = asyncio.run(_drive([100, 100, 100], canceled_at=2, max_polls=9))
    assert r.canceled is True
```

(`run_capture_loop` bounds its own iteration by `max_polls` = `ceil(duration_s / POLL_INTERVAL)`; the handler computes `max_polls` from `duration_s`.)

- [ ] **Step 2: Run, verify fail.** Expected FAIL (module missing).

- [ ] **Step 3: Implement `run_capture_loop` + the handler.** Structure (fill in mirroring `diagnostic_sysrq.py`):

```python
POLL_INTERVAL_SECONDS = 0.5

@dataclass(frozen=True, slots=True)
class LoopResult:
    truncated: bool
    canceled: bool

async def run_capture_loop(*, stat, sleep, canceled, max_bytes, max_polls) -> LoopResult:
    for _ in range(max_polls):
        await sleep(POLL_INTERVAL_SECONDS)
        if await canceled():
            return LoopResult(truncated=False, canceled=True)
        if await stat() >= max_bytes:
            return LoopResult(truncated=True, canceled=False)
    return LoopResult(truncated=False, canceled=False)
```

Handler flow (`capture_traffic_handler`):
1. `payload = load_payload(job, CaptureTrafficPayload)`; `run_id = UUID(payload.run_id)`.
2. `snapshot = await _snapshot(conn, run_id, resolver)` — tx1 under `advisory_xact_lock(SYSTEM, system_id)`: resolve Run→`system_id` (`RUNS.get`), `SYSTEMS.get`, assert `READY`, `binding = resolver.binding_for_system`, assert `LOCAL_LIBVIRT`, assert `binding.runtime.traffic_capturer is not None` else `CategorizedError(CONFIGURATION_ERROR, capability)`. Return `(system_id, domain_name, project, capturer)`.
3. `qom_id = f"kdive-dump-{job.id}"`; `dest = pcap_path(system_id, job.id)`; `await asyncio.to_thread(pcap_dir(system_id).mkdir, parents=True, exist_ok=True)`.
4. `await asyncio.to_thread(capturer.attach, domain_name, qom_id=qom_id, netdev_id=SYSTEM_SSH_NETDEV_ID, dest_path=str(dest), snaplen=payload.snaplen)`.
5. `try:` run the loop — `stat = lambda: asyncio.to_thread(_safe_size, dest)`; `canceled = lambda: _job_canceled(conn, job.id)` (`SELECT state FROM jobs WHERE id=%s` → `== 'canceled'`); `max_polls = math.ceil(payload.duration_s / POLL_INTERVAL_SECONDS)`. `finally:` `await asyncio.to_thread(capturer.detach, domain_name, qom_id=qom_id)`.
6. If `result.canceled`: `await asyncio.to_thread(dest.unlink, missing_ok=True)`; `return None` (no store).
7. `raw = await asyncio.to_thread(read_pcap_bytes, dest)`. If `payload.capture_filter`: `await asyncio.to_thread(validate_bpf, payload.capture_filter)`; trim to a sibling temp `out`, `await asyncio.to_thread(trim_pcap, dest, out, payload.capture_filter)`, use `out` as the store source; else use `dest`.
8. `packets = count_pcap_packets(<final bytes>)` — for telemetry logging; log `packets`/size via the worker's job logger.
9. `artifact_id = await _store_capture(conn, store, job, run_id, source_path)` — tx2 under `advisory_xact_lock(SYSTEM, system_id)`: re-check `_job_canceled` (skip + return None if canceled); `name = f"pcap-{job.id}"`; `object_key = artifact_key("local","runs",str(run_id),name)`; insert-if-absent; `stored = await asyncio.to_thread(store.put_stream, ArtifactStreamRequest(tenant="local", owner_kind="runs", owner_id=str(run_id), name=name, path=source_path, sha256_b64=<computed>, sensitivity=Sensitivity.SENSITIVE, retention_class="pcap"))`; `register_artifact_row(stored, owner_kind="runs", owner_id=run_id, run_id=run_id)`; `ARTIFACTS.insert`; `audit.record(... tool="control.capture_traffic", object_kind="runs", object_id=run_id, transition="capture_traffic", args={"run_id":..., "duration_s":..., "snaplen":..., "filtered": bool(capture_filter)}, project=...)`.
10. Clean up host files (`dest`, `out`) via `asyncio.to_thread(... unlink, missing_ok=True)`; `return str(artifact_id)`.

(Compute `sha256_b64` by streaming the source file — reuse the helper `providers/local_libvirt/retrieve.py` uses for `put_stream`; grep `sha256_b64` there for the exact one-pass hash + `ArtifactStreamRequest` construction to copy.)

- [ ] **Step 4: Write handler behavior tests.** In the same test file, with a fake resolver/binding (mirror `tests/jobs/handlers/control/test_diagnostic_sysrq*.py` fakes) + a fake store recording `put_stream`:
  - READY+local snapshot resolves; a non-READY System → `CategorizedError(CONFIGURATION_ERROR)`; a non-local binding → capability config error; `traffic_capturer is None` → capability config error.
  - Happy path stores an artifact `owner_kind="runs"`, `retention_class="pcap"`, `Sensitivity.SENSITIVE`, name `pcap-<job.id>`; returns its id; `attach` then `detach` both called.
  - Retry with the same `job.id` → insert-if-absent returns the existing id (one row).
  - `capture_filter` set → `validate_bpf` + `trim_pcap` invoked; stored source is the trimmed file.
  - Canceled loop → `detach` called, partial file unlinked, `return None`, no `put_stream`.
  - `PermissionError` from `read_pcap_bytes` → `CONFIGURATION_ERROR` with the remediation.

- [ ] **Step 5: Run, verify pass.** Run: `uv run python -m pytest tests/jobs/handlers/control/test_capture_traffic.py -q`. Expected: PASS.

- [ ] **Step 6: Register the handler.** In `jobs/assembly.py`, add a `_capture_traffic_handler_registrar(*, resolver, object_stores)` closure (mirror `_diagnostic_sysrq_handler_registrar`, but note this handler needs **no** `secret_registry` — packet bytes are not redacted) that does a local import and calls `capture_traffic.register_handlers(registry, resolver=resolver, artifact_store=object_stores.store)`, and append it to `build_handler_registrars`'s returned tuple.

- [ ] **Step 7: Guardrails + commit.** `just lint type`; `uv run python -m pytest tests/jobs/handlers/control/test_capture_traffic.py -q`; commit `feat(1258): capture_traffic worker handler + registration`.

---

### Task 9: Admission tool `control.capture_traffic`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/control/registrar.py` (handler + `@app.tool` wrapper)
- Modify: `src/kdive/mcp/exposure.py` (`_TOOL_SCOPES`)
- Modify: `tests/mcp/core/test_tool_docs.py` (`_BEHAVIOR_TESTS_BY_TOOL`)
- Test: `tests/mcp/lifecycle/test_control_tools.py` (add capture_traffic admission cases)

**Interfaces:**
- Consumes: `JobKind.CAPTURE_TRAFFIC`, `CaptureTrafficPayload`, `hygiene_reason` (T7), the bounds constants.
- Produces: registered tool `control.capture_traffic` (contributor), returning `job_envelope(job, "run_id", uid)`.

**Reference to mirror exactly:** `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py` `_fetch_vmcore` (Run→System resolution, `require_role(CONTRIBUTOR)`, `keyed_mutation`, `job_envelope`) and `control.diagnostic_sysrq`'s `@app.tool` block in `registrar.py:437-475` for the wrapper shape (flat top-level params, `_docmeta.mutating()`, `_docmeta.maturity_meta("implemented")`).

- [ ] **Step 1: Failing admission tests.** In `tests/mcp/lifecycle/test_control_tools.py` (mirror the sysrq admission tests):
  - happy path (READY local Run, contributor) enqueues a `CAPTURE_TRAFFIC` job and returns `status="running"` with `object_id == run_id`.
  - unbound Run → `config_error` `{reason: run_unbound}`, no job.
  - System not READY → `config_error` `{current_status}`, no job.
  - non-local provider → `capability_unsupported`, no job.
  - `capture_filter` too long → `config_error` `{reason: invalid_filter}`, no job.
  - viewer role → permission denied.

- [ ] **Step 2: Run, verify fail.** Expected FAIL (tool not registered).

- [ ] **Step 3: Implement the admission handler.** Add `capture_traffic_system(pool, ctx, *, run_id, duration_s, max_bytes, snaplen, capture_filter, idempotency_key)` near `diagnostic_sysrq_system` in `registrar.py`. Flow (mirror `_fetch_vmcore` + sysrq):
  - `uid = _as_uuid(run_id)`; None → `_invalid_uuid_error("run_id", run_id)`.
  - `RUNS.get`; None/foreign project → `_config_error`; `require_role(ctx, run.project, Role.CONTRIBUTOR)`.
  - `run.system_id is None` → `_config_error(run_id, detail=..., data={"reason":"run_unbound"})`.
  - `SYSTEMS.get`; state ≠ READY → `_config_error(run_id, data={"current_status": state.value})`.
  - resolve runtime for the Run (`with_runtime_for_run` or the binding); `not runtime.support.supports_traffic_capture` → `_capability_unsupported(run_id, capability="traffic_capture", provider=..., supported=[])`.
  - `reason = hygiene_reason(capture_filter)`; not None → `_config_error(run_id, data={"reason":"invalid_filter","detail":reason})`.
  - enqueue under `keyed_mutation(kind="control.capture_traffic")`: `queue.enqueue(conn, JobKind.CAPTURE_TRAFFIC, CaptureTrafficPayload(run_id=run_id, duration_s=duration_s, max_bytes=max_bytes, snaplen=snaplen, capture_filter=capture_filter), job_authorizing(ctx, run.project), f"{run_id}:capture_traffic")` → `job_envelope(job, "run_id", uid)`.

- [ ] **Step 4: Add the `@app.tool` wrapper.** Inside `register()`, alongside `control.diagnostic_sysrq`. Bounds constants live at module top (e.g. `CAPTURE_MIN_DURATION_S=1`, `CAPTURE_MAX_DURATION_S=300`, `CAPTURE_DEFAULT_DURATION_S=30`, `CAPTURE_MIN_BYTES=1048576`, `CAPTURE_MAX_BYTES=536870912`, `CAPTURE_DEFAULT_BYTES=67108864`, `CAPTURE_DEFAULT_SNAPLEN=128`, `CAPTURE_MAX_SNAPLEN=262144`). Flat top-level params, each `Annotated[..., Field(description=f"... {CAPTURE_MAX_DURATION_S} ...")]` interpolating the bound. Wrapper docstring: describe the host-side capture, the `restrict=on` default (only SSH-forward traffic visible unless `guest_egress` on), that the pcap is fetched via `artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<refs.result>)`, and that a 24-byte result means zero packets. **No `ADR-NNNN` anywhere in the docstring/Field.** `annotations=_docmeta.mutating()`, `meta=_docmeta.maturity_meta("implemented")`, `suggested_next_actions` via the job envelope. Example wrapper skeleton:

```python
@app.tool(
    name="control.capture_traffic",
    annotations=_docmeta.mutating(),
    meta=_docmeta.maturity_meta("implemented"),
)
async def capture_traffic(
    run_id: Annotated[str, Field(description="The Run whose bound System's guest traffic to capture.")],
    duration_s: Annotated[int, Field(
        ge=CAPTURE_MIN_DURATION_S, le=CAPTURE_MAX_DURATION_S,
        description=f"Capture window in seconds ({CAPTURE_MIN_DURATION_S}-{CAPTURE_MAX_DURATION_S}).",
    )] = CAPTURE_DEFAULT_DURATION_S,
    max_bytes: Annotated[int, Field(
        ge=CAPTURE_MIN_BYTES, le=CAPTURE_MAX_BYTES,
        description=f"Stop early when the pcap reaches this many bytes ({CAPTURE_MIN_BYTES}-{CAPTURE_MAX_BYTES}).",
    )] = CAPTURE_DEFAULT_BYTES,
    snaplen: Annotated[int, Field(
        ge=1, le=CAPTURE_MAX_SNAPLEN,
        description=f"Bytes captured per packet (1-{CAPTURE_MAX_SNAPLEN}); {CAPTURE_DEFAULT_SNAPLEN} captures headers.",
    )] = CAPTURE_DEFAULT_SNAPLEN,
    capture_filter: Annotated[str | None, Field(
        description="Optional pcap-filter(7)/tcpdump BPF expression applied after capture (e.g. 'tcp port 80').",
    )] = None,
    idempotency_key: Annotated[str | None, Field(description="Optional idempotency key.")] = None,
) -> ToolResponse:
    """Capture host-side guest network traffic into a Run-owned pcap. ..."""
    return await capture_traffic_system(
        pool, current_context(), run_id=run_id, duration_s=duration_s, max_bytes=max_bytes,
        snaplen=snaplen, capture_filter=capture_filter, idempotency_key=idempotency_key,
    )
```

- [ ] **Step 5: Register scope + behavior-test map.** In `exposure.py` `_TOOL_SCOPES`, add `"control.capture_traffic": _CONTRIBUTOR,`. In `tests/mcp/core/test_tool_docs.py` `_BEHAVIOR_TESTS_BY_TOOL`, add `"control.capture_traffic": "tests/mcp/lifecycle/test_control_tools.py",`.

- [ ] **Step 6: Run tool-docs + admission tests.** Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/core/test_no_adr_leak.py tests/mcp/lifecycle/test_control_tools.py -q`. Expected: PASS (flat-params guard, description guard, maturity guard, no-ADR-leak, destructive-set unchanged since the tool is `mutating` not `destructive`, admission cases). Fix any guard failure before proceeding.

- [ ] **Step 7: Guardrails + commit.** `just lint type`; commit `feat(1258): control.capture_traffic admission tool`.

---

### Task 10: Egress — `RawAsset.PCAP` + `artifact_id` + `raw_pcap_key`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py`
- Modify: `src/kdive/artifacts/read_model.py`
- Test: `tests/mcp/tools/artifacts/test_raw_fetch*.py` (add pcap cases), `tests/artifacts/test_read_model*.py`

**Interfaces:**
- Consumes: the stored pcap rows from Task 8 (`owner_kind='runs'`, `retention_class='pcap'`, name `pcap-<job_id>`).
- Produces: `RawAsset.PCAP` (`"pcap"`); `fetch_raw(pool, ctx, *, run_id, asset, artifact_id=None, store_factory=...)`; `raw_pcap_key(conn, run_id, artifact_id) -> str | None`.

- [ ] **Step 1: Failing tests.**
  - `raw_pcap_key(conn, run_id, artifact_id)` returns the exact object_key for a matching run-owned pcap row; returns `None` for a cross-Run `artifact_id`; with `artifact_id=None` returns the newest pcap (`ORDER BY created_at DESC, id DESC LIMIT 1`).
  - `fetch_raw(run_id, asset=RawAsset.PCAP, artifact_id=<id>)` presigns a URL for a contributor and audits; a cross-Run id → `not_found`/`config_error`; `artifact_id` omitted with two pcaps returns the newest; `artifacts.get` on the pcap id is `not_found` (SENSITIVE).

- [ ] **Step 2: Run, verify fail.** Expected FAIL.

- [ ] **Step 3: Implement `raw_pcap_key`** in `read_model.py`:

```python
async def raw_pcap_key(conn: AsyncConnection, run_id: UUID, artifact_id: UUID | None) -> str | None:
    """Object key of a run-owned pcap: the exact one by ``artifact_id`` (validating ownership),
    or the newest for the Run when ``artifact_id`` is None. Returns None if absent/cross-Run."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if artifact_id is not None:
            await cur.execute(
                "SELECT object_key FROM artifacts WHERE id=%s AND owner_kind='runs' "
                "AND owner_id=%s AND retention_class='pcap'",
                (artifact_id, run_id),
            )
        else:
            await cur.execute(
                "SELECT object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s "
                "AND retention_class='pcap' ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            )
        row = await cur.fetchone()
    return row["object_key"] if row else None
```

- [ ] **Step 4: Implement the `fetch_raw` PCAP branch.** In `raw_fetch.py`: add `PCAP = "pcap"` to `RawAsset`; add a keyword-only `artifact_id: str | None = None` param to `fetch_raw` (thread it to `_resolve_key`); in `_resolve_key`, add:

```python
if asset is RawAsset.PCAP:
    aid = _as_uuid(artifact_id) if artifact_id is not None else None
    if artifact_id is not None and aid is None:
        return _config_error(run_id, data={"reason": "invalid_artifact_id"})
    key = await raw_pcap_key(conn, run_uid, aid)
    if key is None:
        return _config_error(run_id, data={"reason": "pcap_unavailable"})
    return key
```

Keep `require_role(ctx, run.project, Role.CONTRIBUTOR)` as-is (already at the top of `_resolve_key`). The `@app.tool` wrapper for `artifacts.fetch_raw` must expose the new `artifact_id` param (find the wrapper in the artifacts registrar; add the flat `Annotated[str | None, Field(...)]` param, defaulting `None`, description noting it selects a specific pcap when `asset="pcap"`).

- [ ] **Step 5: Run, verify pass.** Run: `uv run python -m pytest tests/mcp/tools/artifacts/ tests/artifacts/ -k "pcap or raw" -q`. Expected: PASS.

- [ ] **Step 6: Guardrails + commit.** `just lint type`; commit `feat(1258): fetch_raw pcap egress by artifact_id`.

---

### Task 11: Surface `supports_traffic_capture` on `systems.get`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/view.py`
- Test: `tests/mcp/lifecycle/test_systems*.py` (the `systems.get` view test)

**Interfaces:**
- Consumes: `ProviderSupport.supports_traffic_capture` (T4), wired True for local (T5).
- Produces: `data["supports_traffic_capture"]` on the `systems.get` envelope.

- [ ] **Step 1: Failing test.** Assert a `systems.get` on a local-libvirt System includes `data["supports_traffic_capture"] is True` (mirror the existing `supports_snapshots` assertion).

- [ ] **Step 2: Run, verify fail.** Expected FAIL (`KeyError`).

- [ ] **Step 3: Implement.** In `view.py` `get_system` (the get-only path where `supports_snapshots` is threaded from `runtime.support`), add `supports_traffic_capture=runtime.support.supports_traffic_capture` into the `system_envelope`/`data` exactly parallel to `supports_snapshots` (same try/except CategorizedError guard for `runtime_for_system`).

- [ ] **Step 4: Run, verify pass.** Expected PASS.

- [ ] **Step 5: Guardrails + commit.** `just lint type`; commit `feat(1258): surface supports_traffic_capture on systems.get`.

---

### Task 12: Teardown removes the per-System pcap directory

**Files:**
- Modify: `src/kdive/jobs/handlers/systems.py`
- Test: `tests/jobs/handlers/test_systems*.py` (teardown test)

**Interfaces:**
- Consumes: `pcap_dir(system_id)` (T6).
- Produces: teardown `shutil.rmtree(pcap_dir(system_id), ignore_errors=True)` under the existing best-effort `try/except`.

- [ ] **Step 1: Failing test.** A teardown test that creates `pcap_dir(system_id)` with a dummy `.pcap` file, runs teardown, and asserts the directory is gone; and a second asserting teardown succeeds when the directory is absent (no raise).

- [ ] **Step 2: Run, verify fail.** Expected FAIL (dir still present).

- [ ] **Step 3: Implement.** In `systems.py`, inside the existing best-effort teardown block that calls `_reclaim_console_artifacts`/`_reclaim_sysrq_artifacts` (the `try/except Exception` log-and-continue region — after `provisioner.teardown` has destroyed the domain), add `await asyncio.to_thread(shutil.rmtree, str(pcap_dir(system_id)), ignore_errors=True)`. Import `shutil` and `pcap_dir`. Add a short comment: this is a host-filesystem reclaim (the pcap file is written to local disk by QEMU), distinct from the object-store `_reclaim_*` functions.

- [ ] **Step 4: Run, verify pass.** Expected PASS (both cases).

- [ ] **Step 5: Guardrails + commit.** `just lint type`; commit `feat(1258): teardown reclaims per-System pcap directory`.

---

### Task 13: Regenerate generated docs + full guardrails

**Files:**
- Modify (generated): `docs/guide/safety-and-rbac.md` (rbac-matrix), `docs/guide/reference/*` (tool reference), MCP doc-resource snapshots.

- [ ] **Step 1: Regenerate.** Run: `just rbac-matrix` (adds `control.capture_traffic` to the matrix), `just docs` (tool reference incl. the new tool + `fetch_raw` `artifact_id`), `just resources-docs` (doc-resource snapshots if the tool index changed).

- [ ] **Step 2: Verify no drift.** Run: `just rbac-matrix-check`, `just docs-check`, `just resources-docs-check`. Expected: all report in-sync.

- [ ] **Step 3: Full gate.** Run: `just ci`. Expected: lint, type, lint-shell, lint-workflows, check-mermaid, test all PASS. Fix any failure (a `test_tool_docs`/`test_no_adr_leak`/`test_app` guard is the most likely — `CLASSIFIED_TOOLS | PUBLIC_TOOLS` must equal the live registry; the new tool must be in `_TOOL_SCOPES`, which Task 9 did).

- [ ] **Step 4: Commit.** `git add` the regenerated docs (explicit paths); commit `docs(1258): regenerate tool reference + rbac matrix for capture_traffic`.

---

### Task 14: Live proof (`live_vm`)

**Files:**
- Create/extend: a `live_vm`-marked test under `tests/` (mirror the sysrq/vmcore live proofs) — or a scripted operator walkthrough if no live test host is available in CI.

**Interfaces:**
- Consumes: the whole feature end-to-end on a real KVM/libvirt host.

- [ ] **Step 1: Write the live proof.** A `@pytest.mark.live_vm` test (skips without a host) that, against a pre-provisioned READY local-libvirt System bound to a Run: generates SSH-forward traffic (e.g. an `ssh`/`scp` no-op over the forwarded port) concurrently with `control.capture_traffic(run_id, duration_s=5)`, waits for the job, fetches via `artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<refs.result>)`, downloads the presigned URL, and asserts the bytes are a valid libpcap file with `count_pcap_packets(...) > 0`.

- [ ] **Step 2: Run it on the dev host.** This host runs KVM/libvirt directly. Run: `just test-live -k capture_traffic` (or the equivalent live invocation). Expected: PASS — validates the SELinux/label + qemu:///system readback path. If SELinux denies QEMU writing under `/var/lib/kdive/pcap/`, that surfaces as the `CONTROL_FAILURE`/`CONFIGURATION_ERROR` from the handler; apply the operator remediation (label the dir like the staged-rootfs path) and re-run.

- [ ] **Step 3: Commit.** Commit `test(1258): live_vm proof for control.capture_traffic`.

---

## Rollback / cleanup

- The migration is forward-only (ADR-0015); a rollback drops the feature branch, not the migration. No production data depends on it pre-merge.
- Host pcap files are ephemeral (`/var/lib/kdive/pcap/`), deleted by the handler on success/cancel and by teardown; a failed mid-development run may leave files there — `rm -rf /var/lib/kdive/pcap/<system_id>/` is safe.
- No new dependency: `tcpdump` and `libvirt_qemu` are already host/runtime prereqs.

## Self-review notes

- **Spec coverage:** tool surface (T9), bounds interpolation (T9), provider port + fail-closed flag (T4/T5), filter-dump attach/detach + not-found swallow (T5), handler-owned poll loop + cancel + max_bytes (T8), zero-packet-is-success via fetch_raw size (T9 docstring + count telemetry T8), SENSITIVE Run-owned store + insert-if-absent (T8), egress by artifact_id (T10), capability discoverability (T11), teardown reclaim (T12), migration 0072 (T1), regen + guards (T13), live proof (T14). Endianness-aware count (T6). BPF hygiene/validate/trim argv-not-shell (T7).
- **Payload base:** `CaptureTrafficPayload(RunPayload)` — run-addressed, matches the spec fix.
- **No new redaction:** the handler takes no `secret_registry` (binary packets are not regex-redacted; `SENSITIVE` + `fetch_raw` gate is the exposure control), unlike `diagnostic_sysrq`.
