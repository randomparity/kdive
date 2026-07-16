# Per-host guest CPU capabilities at System selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advertise each remote-libvirt host's expected guest CPU (raw model/vendor + normalized `x86-64-vN`) on `resources.list`/`resources.describe`, and persist that baseline onto a System at mint so `systems.get` reports it — closing the ADR-0297 host-model selection-surface gap (#980).

**Architecture:** Two additive, remote-libvirt-scoped surfaces, no new tool/RBAC/error-category. Surface 1: a `host_cpu` key in the Resource `capabilities` jsonb (no migration), populated at remote discovery from `getDomainCapabilities` host-model, surfaced to the agent. Surface 2: a nullable `resolved_cpu` jsonb column on `systems` (migration 0070), resolved at mint from the bound Resource's advertised `host_cpu` — the same mechanism `accel` uses (ADR-0339), not a live-XML read.

**Tech Stack:** Python 3.14, `uv`, `pytest -n auto`, `ruff`, `ty`, psycopg/psycopg_pool, defusedxml, libvirt-python. Design: [`2026-07-16-host-cpu-capabilities-980.md`](2026-07-16-host-cpu-capabilities-980.md). ADR: [`../adr/0368-host-cpu-capabilities-selection.md`](../adr/0368-host-cpu-capabilities-selection.md).

## Global Constraints

- **Guardrail suite:** `just ci` = `lint` (`ruff check` + `ruff format --check`), `type` (`ty check`, whole tree), `lint-shell`, `lint-workflows`, `check-mermaid`, `test` (`pytest -m "not live_vm and not live_stack" -n auto`). Run relevant tests per task; run full `just ci` before the PR.
- **Single test:** `uv run python -m pytest <path>::<name> -q`.
- **Ruff:** line length 100; lint set `E,F,I,UP,B,SIM`. **Absolute imports only** (no `..`).
- **Types:** `ty` strict. C-extension deps (`libvirt`, `drgn`) suppress `unresolved-import` with a scoped per-site `# ty: ignore[unresolved-import]` only where already used; libvirt binding methods use `# noqa: N802` for camelCase names.
- **Doc-style guard:** never "Sprint"/"critical"/"robust"/"comprehensive"/"elegant"/"significant" in docs, ADRs, commit messages, comments.
- **Wrapper docstring is the agent contract:** when a tool's returned `data` fields change, update the `@app.tool` wrapper docstring / `Field` text, not only the handler (AGENTS.md).
- **defusedxml** for any XML crossing the libvirtd trust boundary; a parse fault returns a sentinel (`None`/`{}`), never raises into discovery.
- **Migrations are byte-immutable once applied** (ADR-0015); a new migration is a new file, monotonic number. This feature's migration is `0070`.
- **Commits:** conventional format, imperative ≤72-char subject, end every commit with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stage explicit paths (never `git add -A`).

## File structure

| Path | Responsibility | Task |
|------|----------------|------|
| `src/kdive/domain/platform/cpu_baseline.py` (create) | x86-64 model→level table + disable-guarded `baseline_level()` | 1 |
| `src/kdive/providers/shared/libvirt_xml.py` (modify) | `parse_host_cpu()` — raw parse of host-model block (domain-free) | 2 |
| `src/kdive/domain/catalog/resource_capabilities.py` (modify) | `HostCpu` TypedDict, `HOST_CPU_KEY`, `host_cpu()` reader, `_KNOWN_KEYS` | 3 |
| `src/kdive/providers/remote_libvirt/connection/transport.py` (modify) | widen `_LibvirtConn` with `getDomainCapabilities` | 4 |
| `src/kdive/providers/remote_libvirt/discovery.py` (modify) | populate guarded `host_cpu` capability | 4 |
| `src/kdive/mcp/tools/_resource_envelopes.py` (modify) | flatten `host_cpu` into envelope `data` | 5 |
| `src/kdive/db/schema/0070_system_resolved_cpu.sql` (create) | nullable `resolved_cpu jsonb` column | 6 |
| `src/kdive/domain/lifecycle/records.py` (modify) | `System.resolved_cpu` field | 6 |
| `src/kdive/db/repositories.py` (modify) | add `resolved_cpu` to SYSTEMS `json_columns` | 6 |
| `src/kdive/services/systems/admission.py` (modify) | single-load mint resolution of accel + `resolved_cpu` + fadump | 7 |
| `src/kdive/mcp/tools/lifecycle/systems/view.py` (modify) | surface `resolved_cpu` in `system_envelope` | 7 |
| `tests/...` (create/modify) | unit/service/live tests per task | all |
| operator docs (modify) | host-registration rollout note | 8 |

---

### Task 1: x86-64 baseline-level table + disable-guard

**Files:**
- Create: `src/kdive/domain/platform/cpu_baseline.py`
- Test: `tests/domain/platform/test_cpu_baseline.py`

**Interfaces:**
- Produces: `baseline_level(model: str, disabled_features: Collection[str]) -> str | None` — maps a libvirt/QEMU CPU model name to `"x86-64-v1"|..|"x86-64-v4"`, or `None` for an unmapped model **or** when a feature defining the mapped level appears in `disabled_features`. Also exports `LEVEL_DEFINING_FEATURES: dict[str, frozenset[str]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/platform/test_cpu_baseline.py
from kdive.domain.platform.cpu_baseline import baseline_level


def test_known_model_maps_to_level():
    assert baseline_level("Skylake-Client-IBRS", []) == "x86-64-v3"
    assert baseline_level("Nehalem", []) == "x86-64-v2"
    assert baseline_level("Cascadelake-Server", []) == "x86-64-v4"


def test_unknown_model_is_none():
    assert baseline_level("SomeFutureModel-v9", []) is None


def test_disable_guard_omits_level_when_defining_feature_stripped():
    # A v3 model with avx2 disabled by host-model must not advertise v3.
    assert baseline_level("Skylake-Client-IBRS", ["avx2"]) is None


def test_disable_of_unrelated_feature_keeps_level():
    assert baseline_level("Skylake-Client-IBRS", ["md-clear"]) == "x86-64-v3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/domain/platform/test_cpu_baseline.py -q`
Expected: FAIL (module `cpu_baseline` not found).

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/domain/platform/cpu_baseline.py
"""Normalize a libvirt/QEMU x86-64 CPU model name to an ``x86-64-vN`` baseline level (ADR-0368).

The level is a nominal, name-derived upper bound: it maps the model name to its spec level, then
omits the level when a feature that *defines* that level is explicitly disabled in the host-model
block. It does not reconstruct the full feature set (that alternative was rejected in ADR-0368),
so a base-model-implied feature the host silently lacks is not caught — callers must treat a
present level as nominal, not a guaranteed floor.
"""

from __future__ import annotations

from collections.abc import Collection

# Curated libvirt/QEMU model name -> x86-64 micro-arch level. Extend as new models ship; an
# unmapped model degrades to "raw model, no level" (never a wrong level).
X86_64_MODEL_LEVELS: dict[str, str] = {
    # v2
    "Nehalem": "x86-64-v2",
    "Nehalem-IBRS": "x86-64-v2",
    "Westmere": "x86-64-v2",
    "Westmere-IBRS": "x86-64-v2",
    "SandyBridge": "x86-64-v2",
    "SandyBridge-IBRS": "x86-64-v2",
    "IvyBridge": "x86-64-v2",
    "IvyBridge-IBRS": "x86-64-v2",
    "Opteron_G3": "x86-64-v2",
    "Opteron_G4": "x86-64-v2",
    "Opteron_G5": "x86-64-v2",
    "EPYC": "x86-64-v2",
    # v3
    "Haswell": "x86-64-v3",
    "Haswell-IBRS": "x86-64-v3",
    "Haswell-noTSX": "x86-64-v3",
    "Haswell-noTSX-IBRS": "x86-64-v3",
    "Broadwell": "x86-64-v3",
    "Broadwell-IBRS": "x86-64-v3",
    "Broadwell-noTSX": "x86-64-v3",
    "Broadwell-noTSX-IBRS": "x86-64-v3",
    "Skylake-Client": "x86-64-v3",
    "Skylake-Client-IBRS": "x86-64-v3",
    "Skylake-Client-noTSX-IBRS": "x86-64-v3",
    "Skylake-Server": "x86-64-v3",
    "Skylake-Server-IBRS": "x86-64-v3",
    "Skylake-Server-noTSX-IBRS": "x86-64-v3",
    "EPYC-Rome": "x86-64-v3",
    "EPYC-Milan": "x86-64-v3",
    # v4
    "Cascadelake-Server": "x86-64-v4",
    "Cascadelake-Server-noTSX": "x86-64-v4",
    "Icelake-Server": "x86-64-v4",
    "Icelake-Server-noTSX": "x86-64-v4",
    "SapphireRapids": "x86-64-v4",
    "EPYC-Genoa": "x86-64-v4",
}

# The features that define each level; if the host-model block disables one for its mapped level,
# the guest is below that level and the level is omitted. Only the level-boundary markers, not the
# full set (ADR-0368: a targeted disable-guard, not full feature expansion).
LEVEL_DEFINING_FEATURES: dict[str, frozenset[str]] = {
    "x86-64-v2": frozenset({"sse4.2", "popcnt"}),
    "x86-64-v3": frozenset({"avx2", "bmi2", "avx"}),
    "x86-64-v4": frozenset({"avx512f"}),
}


def baseline_level(model: str, disabled_features: Collection[str]) -> str | None:
    """Return the ``x86-64-vN`` level for ``model``, or ``None`` if unmapped or under-delivered.

    Maps ``model`` via :data:`X86_64_MODEL_LEVELS`; returns ``None`` when the model is not in the
    table, or when any feature defining the mapped level is in ``disabled_features`` (the
    host-model disable-guard). The level is nominal — see the module docstring.
    """
    level = X86_64_MODEL_LEVELS.get(model)
    if level is None:
        return None
    disabled = set(disabled_features)
    if LEVEL_DEFINING_FEATURES.get(level, frozenset()) & disabled:
        return None
    return level
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/domain/platform/test_cpu_baseline.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/platform/cpu_baseline.py tests/domain/platform/test_cpu_baseline.py
git commit -m "feat(980): x86-64 CPU model->baseline-level table with disable-guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `parse_host_cpu` — parse the host-model block (domain-free)

**Files:**
- Modify: `src/kdive/providers/shared/libvirt_xml.py`
- Test: `tests/providers/test_libvirt_xml.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `parse_host_cpu(dom_caps_xml: str) -> ParsedHostCpu | None` and a frozen dataclass `ParsedHostCpu(model: str, vendor: str | None, arch: str | None, disabled_features: frozenset[str])`. Returns `None` on parse fault, empty XML, or a host-model block with no concrete `<model>`. **Does not import `domain/`** (level derivation happens in the discovery layer) — mirrors how `parse_guest_arches` takes `supported` injected.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/test_libvirt_xml.py
from kdive.providers.shared.libvirt_xml import ParsedHostCpu, parse_host_cpu

_DOMCAPS = """
<domainCapabilities>
  <cpu>
    <mode name='host-passthrough' supported='yes'/>
    <mode name='host-model' supported='yes'>
      <model fallback='forbid'>Skylake-Client-IBRS</model>
      <vendor>Intel</vendor>
      <feature policy='require' name='ssse3'/>
      <feature policy='disable' name='avx512f'/>
    </mode>
  </cpu>
</domainCapabilities>
"""


def test_parse_host_cpu_reads_model_vendor_and_disabled():
    parsed = parse_host_cpu(_DOMCAPS)
    assert parsed == ParsedHostCpu(
        model="Skylake-Client-IBRS",
        vendor="Intel",
        arch=None,
        disabled_features=frozenset({"avx512f"}),
    )


def test_parse_host_cpu_none_on_malformed():
    assert parse_host_cpu("<not-valid") is None


def test_parse_host_cpu_none_when_host_model_has_no_model():
    xml = "<domainCapabilities><cpu><mode name='host-model' supported='yes'/></cpu></domainCapabilities>"
    assert parse_host_cpu(xml) is None


def test_parse_host_cpu_none_when_host_model_unsupported():
    xml = (
        "<domainCapabilities><cpu>"
        "<mode name='host-model' supported='no'/></cpu></domainCapabilities>"
    )
    assert parse_host_cpu(xml) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k host_cpu -q`
Expected: FAIL (`parse_host_cpu` not defined).

- [ ] **Step 3: Write minimal implementation**

Add near `parse_guest_arches` in `src/kdive/providers/shared/libvirt_xml.py` (keep imports: it already imports `dataclass`? No — add `from dataclasses import dataclass`):

```python
@dataclass(frozen=True, slots=True)
class ParsedHostCpu:
    """The raw host-model CPU fields parsed from a domain-capabilities document (ADR-0368).

    Domain-free: ``arch`` is whatever the block carries (usually absent — the caller supplies the
    host arch parsed elsewhere); ``disabled_features`` are the ``<feature policy='disable'>`` names
    the level disable-guard consumes. Baseline-level derivation lives in ``domain/platform`` and is
    applied by the discovery layer, keeping this shared helper free of a ``providers/shared ->
    domain`` dependency.
    """

    model: str
    vendor: str | None
    arch: str | None
    disabled_features: frozenset[str]


def parse_host_cpu(dom_caps_xml: str) -> ParsedHostCpu | None:
    """Read the ``<cpu><mode name='host-model'>`` block from a domain-capabilities document.

    Returns ``None`` on a parse fault, an unsupported/absent host-model mode, or a host-model block
    with no concrete ``<model>`` text (a host libvirt cannot model) — discovery never crashes and
    never advertises an empty model, mirroring :func:`parse_capabilities_arch`. Parsed with
    ``defusedxml`` (the XML crosses the libvirtd trust boundary).
    """
    try:
        root: ET.Element = _safe_fromstring(dom_caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain capabilities for host cpu", exc_info=True)
        return None
    for mode in root.findall("./cpu/mode"):
        if mode.get("name") != "host-model" or mode.get("supported") == "no":
            continue
        model = (mode.findtext("model") or "").strip()
        if not model:
            return None
        vendor = (mode.findtext("vendor") or "").strip() or None
        arch = (mode.findtext("arch") or "").strip() or None
        disabled = frozenset(
            name
            for feat in mode.findall("feature")
            if feat.get("policy") == "disable" and (name := feat.get("name")) is not None
        )
        return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=disabled)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k host_cpu -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/libvirt_xml.py tests/providers/test_libvirt_xml.py
git commit -m "feat(980): parse host-model CPU block from domain capabilities

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `HostCpu` TypedDict + `host_cpu()` capability reader

**Files:**
- Modify: `src/kdive/domain/catalog/resource_capabilities.py`
- Test: `tests/domain/catalog/test_resource_capabilities.py` (create if absent; else add cases)

**Interfaces:**
- Consumes: nothing new.
- Produces: `HOST_CPU_KEY = "host_cpu"`; `class HostCpu(TypedDict)` with `model: str`, `vendor: NotRequired[str]`, `arch: str`, `baseline_level: NotRequired[str]`; `ResourceCapabilities.host_cpu() -> HostCpu | None` (defensive: `None` unless the stored mapping has a string `model` and `arch`, dropping malformed rows, mirroring `guest_arches()`). `HOST_CPU_KEY` added to `_KNOWN_KEYS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/catalog/test_resource_capabilities.py
from kdive.domain.catalog.resource_capabilities import ResourceCapabilities


def test_host_cpu_reads_full_shape():
    caps = ResourceCapabilities.from_mapping(
        {"host_cpu": {"model": "Skylake-Client-IBRS", "vendor": "Intel",
                      "arch": "x86_64", "baseline_level": "x86-64-v3"}}
    )
    assert caps.host_cpu() == {
        "model": "Skylake-Client-IBRS", "vendor": "Intel",
        "arch": "x86_64", "baseline_level": "x86-64-v3",
    }


def test_host_cpu_absent_is_none():
    assert ResourceCapabilities.from_mapping({}).host_cpu() is None


def test_host_cpu_malformed_is_none():
    # missing model/arch -> dropped
    assert ResourceCapabilities.from_mapping({"host_cpu": {"vendor": "Intel"}}).host_cpu() is None
    assert ResourceCapabilities.from_mapping({"host_cpu": "nope"}).host_cpu() is None


def test_host_cpu_drops_non_string_optional_fields():
    caps = ResourceCapabilities.from_mapping(
        {"host_cpu": {"model": "EPYC", "arch": "x86_64", "vendor": 7, "baseline_level": None}}
    )
    assert caps.host_cpu() == {"model": "EPYC", "arch": "x86_64"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -k host_cpu -q`
Expected: FAIL (`host_cpu` not defined).

- [ ] **Step 3: Write minimal implementation**

Add the key constant near `GUEST_ARCHES_KEY`, the TypedDict near `GuestArch`, `HOST_CPU_KEY` to `_KNOWN_KEYS`, and the reader method on `ResourceCapabilities`:

```python
from typing import NotRequired  # add to the typing import line

# The host CPU baseline a remote host advertises for a host-model guest (ADR-0368): the model
# libvirt synthesizes, its vendor, arch, and a normalized x86-64-vN level. Agent-facing, unlike
# guest_arches. Absent on local-libvirt/fault-inject and un-refreshed remote hosts.
HOST_CPU_KEY = "host_cpu"


class HostCpu(TypedDict):
    """A host's advertised guest CPU baseline. ``vendor``/``baseline_level`` may be absent."""

    model: str
    vendor: NotRequired[str]
    arch: str
    baseline_level: NotRequired[str]
```

Add `HOST_CPU_KEY` to the `_KNOWN_KEYS` frozenset. Add the reader:

```python
    def host_cpu(self) -> HostCpu | None:
        """The host's advertised guest CPU baseline (ADR-0368), or ``None`` if absent/malformed.

        Defensive over the persisted JSON (mirrors :meth:`guest_arches`): requires a mapping with
        string ``model`` and ``arch``; ``vendor`` and ``baseline_level`` are included only when
        present as strings. Any other shape (a stale/hand-edited row) reads as ``None``.
        """
        raw = self._values.get(HOST_CPU_KEY)
        if not isinstance(raw, Mapping):
            return None
        model = raw.get("model")
        arch = raw.get("arch")
        if not isinstance(model, str) or not isinstance(arch, str):
            return None
        result: HostCpu = {"model": model, "arch": arch}
        vendor = raw.get("vendor")
        if isinstance(vendor, str):
            result["vendor"] = vendor
        level = raw.get("baseline_level")
        if isinstance(level, str):
            result["baseline_level"] = level
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -k host_cpu -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/catalog/resource_capabilities.py tests/domain/catalog/test_resource_capabilities.py
git commit -m "feat(980): HostCpu capability key + defensive host_cpu() reader

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Discovery populates the guarded `host_cpu` capability

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/connection/transport.py` (widen `_LibvirtConn`)
- Modify: `src/kdive/providers/remote_libvirt/discovery.py`
- Modify: `tests/providers/remote_libvirt/conftest.py` (`FakeConn.getDomainCapabilities`)
- Test: `tests/providers/remote_libvirt/test_discovery.py`

**Interfaces:**
- Consumes: `parse_host_cpu` (Task 2), `baseline_level` (Task 1), `HOST_CPU_KEY` (Task 3), the host `arch` already parsed at `discovery.py`, `self._config.machine`.
- Produces: `capabilities["host_cpu"]` = a `HostCpu`-shaped dict when the host advertises a host-model CPU; absent otherwise. A `getDomainCapabilities` raise or a `None` parse leaves the ResourceRecord otherwise intact.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/remote_libvirt/test_discovery.py
def test_capabilities_advertise_host_cpu(...):
    # Arrange a FakeConn whose getDomainCapabilities returns a host-model block for
    # Skylake-Client-IBRS with avx512f disabled; discover and assert the host_cpu key.
    record = discovery.list_resources()[0]
    assert record.capabilities["host_cpu"] == {
        "model": "Skylake-Client-IBRS", "vendor": "Intel",
        "arch": "x86_64", "baseline_level": "x86-64-v3",
    }


def test_host_cpu_absent_when_getdomaincapabilities_raises(...):
    # FakeConn.getDomainCapabilities raises libvirt.libvirtError; the record still discovers.
    record = discovery.list_resources()[0]
    assert "host_cpu" not in record.capabilities
    assert record.capabilities["arch"] == "x86_64"
    assert record.capabilities["vcpus"] == 8  # from getInfo


def test_host_cpu_absent_when_domcaps_has_no_model(...):
    # getDomainCapabilities returns a host-model block with no <model>; host_cpu omitted.
    record = discovery.list_resources()[0]
    assert "host_cpu" not in record.capabilities
```

Follow the existing `test_discovery.py` construction (its `RemoteLibvirtDiscovery` + injected `open_connection` fixture); extend `FakeConn` in `conftest.py` to return per-test domcaps XML (add a settable attribute or subclass), including a variant whose `getDomainCapabilities` raises `libvirt.libvirtError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_discovery.py -k host_cpu -q`
Expected: FAIL (`host_cpu` not populated / `getDomainCapabilities` missing).

- [ ] **Step 3: Write minimal implementation**

In `transport.py` widen the `_LibvirtConn` Protocol:

```python
class _LibvirtConn(Protocol):
    def getInfo(self) -> list[Any]: ...  # noqa: N802 - libvirt binding name
    def getCapabilities(self) -> str: ...  # noqa: N802 - libvirt binding name
    def getDomainCapabilities(  # noqa: N802 - libvirt binding name
        self,
        emulatorbin: str | None = None,
        arch: str | None = None,
        machine: str | None = None,
        virttype: str | None = None,
        flags: int = 0,
    ) -> str: ...
    def close(self) -> None: ...
```

In `conftest.py` add to `FakeConn` a `getDomainCapabilities` returning a host-model domcaps string (make the payload/raise configurable per test, e.g. `self.domcaps_xml` / `self.domcaps_error`).

In `discovery.py`: import `libvirt` (with the scoped ignore), `parse_host_cpu`, `baseline_level`, `HOST_CPU_KEY`, and a module logger. Inside the `with remote_connection(...) as conn:` block (conn closes after), after reading `info`/`arch`, capture the host_cpu guardedly:

```python
host_cpu = _discover_host_cpu(conn, arch, self._config.machine)
```

where a module helper (kept small, complexity ≤8):

```python
def _discover_host_cpu(conn: OpenConnection, arch: str, machine: str) -> dict[str, Any] | None:
    """Advertise the host-model guest CPU baseline (ADR-0368), or ``None`` on any fault.

    Parameterized to match the renderer (``render_domain_xml``): ``virttype='kvm'``,
    ``machine`` from config, host ``arch``, default emulator. A ``libvirtError`` (old libvirt
    without the API, transient RPC fault) or an unparseable/absent host-model block yields
    ``None`` so a new advisory field never drops the host from discovery.
    """
    try:
        dom_caps = conn.getDomainCapabilities(None, arch, machine, "kvm")
    except libvirt.libvirtError:
        _log.warning("getDomainCapabilities failed; omitting host_cpu", exc_info=True)
        return None
    parsed = parse_host_cpu(dom_caps)
    if parsed is None:
        return None
    result: dict[str, Any] = {"model": parsed.model, "arch": parsed.arch or arch}
    if parsed.vendor is not None:
        result["vendor"] = parsed.vendor
    level = baseline_level(parsed.model, parsed.disabled_features)
    if level is not None:
        result["baseline_level"] = level
    return result
```

Then in `list_resources`, after building `capabilities`, add:

```python
if host_cpu is not None:
    capabilities[HOST_CPU_KEY] = host_cpu
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_discovery.py -q`
Expected: PASS (existing + 3 new). Also run `uv run ty check` — the widened Protocol must still be satisfied by the real binding path.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/connection/transport.py src/kdive/providers/remote_libvirt/discovery.py tests/providers/remote_libvirt/conftest.py tests/providers/remote_libvirt/test_discovery.py
git commit -m "feat(980): advertise host_cpu at remote discovery, guarded

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Surface `host_cpu` on `resources.list`/`resources.describe`

**Files:**
- Modify: `src/kdive/mcp/tools/_resource_envelopes.py`
- Modify: `src/kdive/mcp/tools/catalog/resources.py` (wrapper docstrings only — agent contract)
- Test: `tests/mcp/catalog/test_resources_tools.py`

**Interfaces:**
- Consumes: `resource.capability_view.host_cpu()` (Task 3).
- Produces: envelope `data["host_cpu"]` = the `HostCpu` dict when present, omitted otherwise. This is the deliberate divergence from `guest_arches` (which is not surfaced).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/mcp/catalog/test_resources_tools.py
def test_resource_data_includes_host_cpu(...):
    # a remote Resource whose capabilities carry host_cpu -> resources.describe data.host_cpu
    resp = await describe_resource(pool, ctx, resource_id)
    assert resp.structured_content["data"]["host_cpu"]["model"] == "Skylake-Client-IBRS"
    assert resp.structured_content["data"]["host_cpu"]["baseline_level"] == "x86-64-v3"


def test_resource_data_omits_host_cpu_when_absent(...):
    # a local-libvirt/fault Resource with no host_cpu -> no key
    resp = await describe_resource(pool, ctx, resource_id)
    assert "host_cpu" not in resp.structured_content["data"]
```

Mirror the existing `test_resources_tools.py` fixtures (they seed Resources with capabilities and call the handlers). Read `structured_content` per the fielded-output convention.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -k host_cpu -q`
Expected: FAIL (no `host_cpu` in data).

- [ ] **Step 3: Write minimal implementation**

In `_resource_envelopes.py::resource_capability_data`, before `return data`:

```python
    host_cpu = caps.host_cpu()
    if host_cpu is not None:
        data["host_cpu"] = dict(host_cpu)
```

(`caps` is already `resource.capability_view`; `dict(host_cpu)` yields a plain `JsonValue` mapping.)

In `resources.py`, update the `resources.list` and `resources.describe` **wrapper** docstrings to name the new `host_cpu` field and its `baseline_level` semantics: "`host_cpu` (remote hosts only): `{model, vendor?, arch, baseline_level?}` — the expected guest CPU under host-model; `baseline_level` (`x86-64-vN`) is a nominal upper bound and may be absent for an unmapped model, not a floor."

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/_resource_envelopes.py src/kdive/mcp/tools/catalog/resources.py tests/mcp/catalog/test_resources_tools.py
git commit -m "feat(980): surface host_cpu on resources.list/describe

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Migration 0070 + `System.resolved_cpu` field

**Files:**
- Create: `src/kdive/db/schema/0070_system_resolved_cpu.sql`
- Modify: `src/kdive/domain/lifecycle/records.py` (`System.resolved_cpu`)
- Modify: `src/kdive/db/repositories.py` (SYSTEMS `json_columns`)
- Test: `tests/db/test_migration_0070_resolved_cpu.py`

**Interfaces:**
- Produces: `System.resolved_cpu: dict[str, JsonValue] | None` (a `HostCpu`-shaped dict, stored/read as jsonb). SYSTEMS repo serializes it via `json_columns`.

- [ ] **Step 1: Write the migration + failing test**

Create the migration (mirror `0067_system_accel.sql` header + doc-style):

```sql
-- 0070_system_resolved_cpu.sql — record the resolved guest CPU baseline on the System (ADR-0368).
--
-- Surface 2 of #980. Resolved at System mint from the bound Resource's advertised `host_cpu`
-- capability (the same mint-time mechanism ADR-0339 `accel` uses), so `systems.get` reports the
-- CPU baseline a System was minted against as a cheap row read (no live libvirt call).
--
-- Nullable with no default: NULL means "no CPU baseline recorded" — a pre-migration System, a
-- local-libvirt/fault-inject System, or a remote host that advertises no `host_cpu` (not
-- re-registered since this feature shipped). Consumers treat NULL as unknown, never crash.
ALTER TABLE systems
    ADD COLUMN resolved_cpu jsonb;
```

```python
# tests/db/test_migration_0070_resolved_cpu.py — mirror test_migration_0069_watch_for_crash.py
# Assert: after migrations, systems.resolved_cpu exists, is jsonb, is nullable, and a System
# inserted without it reads back None.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/db/test_migration_0070_resolved_cpu.py -q`
Expected: FAIL (column missing) — requires Docker/testcontainers; if Docker absent it SKIPs (set `KDIVE_REQUIRE_DOCKER=1` to force).

- [ ] **Step 3: Wire the model + repository**

In `records.py`, add to `System` (after `accel`):

```python
    #: Host-derived guest CPU baseline resolved from the bound Resource's advertised `host_cpu`
    #: at mint (ADR-0368): `{model, vendor?, arch, baseline_level?}`. NULL when the resource
    #: advertises none (local/fault/un-refreshed remote) — treat NULL as unknown, never crash.
    resolved_cpu: dict[str, JsonValue] | None = None
```

Ensure `JsonValue` is imported in `records.py` (it is used elsewhere; add the import if not). In `repositories.py`, extend SYSTEMS:

```python
SYSTEMS = StatefulRepository(
    System, "systems", SystemState,
    json_columns=frozenset({"provisioning_profile", "resolved_cpu"}),
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/db/test_migration_0070_resolved_cpu.py -q` (with Docker)
Expected: PASS. Also `uv run ty check`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0070_system_resolved_cpu.sql src/kdive/domain/lifecycle/records.py src/kdive/db/repositories.py tests/db/test_migration_0070_resolved_cpu.py
git commit -m "feat(980): migration 0070 + System.resolved_cpu column

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Resolve `resolved_cpu` at mint (single load) + surface on `systems.get`

**Files:**
- Modify: `src/kdive/services/systems/admission.py`
- Modify: `src/kdive/mcp/tools/lifecycle/systems/view.py`
- Modify: `src/kdive/mcp/tools/lifecycle/systems/*.py` wrapper (systems.get docstring — agent contract)
- Test: `tests/services/systems/test_admission_*.py` (mint resolution), `tests/mcp/.../test_systems_view*.py` (envelope)

**Interfaces:**
- Consumes: `ResourceCapabilities.host_cpu()` (Task 3), `System.resolved_cpu` (Task 6).
- Produces: a minted System carries `resolved_cpu` from the bound Resource's `host_cpu` (or `None`); `systems.get` `data["resolved_cpu"]` surfaces it. accel + fadump + resolved_cpu resolve from **one** `RESOURCES.get`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/systems/... (mirror the accel admission tests)
async def test_mint_records_resolved_cpu_from_bound_host(...):
    # Resource advertises host_cpu; mint a System; assert system.resolved_cpu == that host_cpu.

async def test_mint_records_null_resolved_cpu_when_host_advertises_none(...):
    # Resource with no host_cpu; mint; assert system.resolved_cpu is None.

# tests/mcp/.../test_systems_view*.py
async def test_systems_get_surfaces_resolved_cpu(...):
    resp = await get_system(pool, ctx, system_id)
    assert resp.structured_content["data"]["resolved_cpu"]["model"] == "Skylake-Client-IBRS"

async def test_systems_get_omits_resolved_cpu_when_absent(...):
    resp = await get_system(pool, ctx, system_id)
    assert "resolved_cpu" not in resp.structured_content["data"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/systems -k resolved_cpu tests/mcp -k resolved_cpu -q`
Expected: FAIL.

- [ ] **Step 3: Implement the single-load mint resolution + envelope**

In `admission.py`, replace the two separate helpers' per-call `RESOURCES.get` with one load. Introduce:

```python
async def _resolve_new_system_bindings(
    conn: AsyncConnection,
    resource_id: UUID | None,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
) -> tuple[str | None, dict[str, JsonValue] | None]:
    """Load the bound Resource once; resolve accel + host_cpu and validate fadump (one round-trip).

    Consolidates the ADR-0339 accel resolution, the ADR-0368 CPU-baseline snapshot, and the
    ADR-0349 fadump precondition onto a single `RESOURCES.get` in the mint transaction. Fail-open
    accel/host_cpu (None when the resource advertises none), fail-closed fadump (rejects a
    fadump-opted provision on a host that does not advertise it). Raises `CONFIGURATION_ERROR`
    (accel mis-arch or fadump-unsupported) before the granted->active flip.
    """
    resource = await RESOURCES.get(conn, resource_id) if resource_id is not None else None
    caps = resource.capability_view if resource is not None else None
    accel = resolve_accel(caps.guest_arches(), profile.arch) if caps is not None else None
    host_cpu = caps.host_cpu() if caps is not None else None
    requested = profile_policy.fadump_provisioned(profile)
    supported = caps is not None and caps.pseries_fadump()
    require_fadump_supported(requested=requested, supported=supported)
    resolved_cpu = dict(host_cpu) if host_cpu is not None else None
    return accel, resolved_cpu
```

Import `require_fadump_supported`/`resolve_accel` are already imported. Update `_insert_defined_system` and `_insert_provisioning_system` to call the new helper inside their existing `try`:

```python
        accel, resolved_cpu = await _resolve_new_system_bindings(
            conn, alloc.resource_id, profile, profile_policy
        )
```

and remove the now-dead `_resolve_new_system_accel` + `_validate_fadump_supported` (replace, don't leave shims). Thread `resolved_cpu` through `_insert_system_and_activate` (add a `resolved_cpu: dict[str, JsonValue] | None` param) into the `System(...)` construction (`resolved_cpu=resolved_cpu`).

In `view.py::system_envelope`, after the `accel` line in `data`:

```python
    if system.resolved_cpu is not None:
        data["resolved_cpu"] = system.resolved_cpu
```

Update the `systems.get` **wrapper** docstring to name `resolved_cpu`: "`resolved_cpu` (remote Systems): the `{model, vendor?, arch, baseline_level?}` CPU baseline the System was minted against; absent when the host advertised none. `baseline_level` is a nominal upper bound (see resources.describe)."

- [ ] **Step 4: Run tests + regression to verify they pass**

Run: `uv run python -m pytest tests/services/systems tests/mcp/tools/lifecycle/systems -q`
Expected: PASS (new + existing admission/view tests — the single-load refactor must not change accel/fadump behavior). Run `uv run ty check`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/systems/admission.py src/kdive/mcp/tools/lifecycle/systems/view.py tests/services/systems tests/mcp/tools/lifecycle/systems
git commit -m "feat(980): resolve resolved_cpu at mint (single load) + surface on systems.get

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Live proofs, docs delta, generated-doc regen, full guardrails

**Files:**
- Create: `tests/integration/test_remote_host_cpu_live.py` (`live_vm`-gated: discovery `host_cpu` proof + reconcile)
- Modify: operator host-registration docs (rollout note)
- Modify: any generated doc that drifts (tool schema / capability reference) — regenerate, don't hand-edit

**Interfaces:** none new (end-to-end proof + docs).

- [ ] **Step 1: Write the `live_vm` proofs**

Mirror `tests/integration/test_remote_el9_cpu_reachability_live.py` (read-only, env-gated, ADR-0035 §4 skip idiom). Two tests, both skipping cleanly without the remote host env (`KDIVE_EL9_REACHABILITY_URI` or a new `KDIVE_HOST_CPU_URI`):
- `test_remote_host_advertises_host_cpu`: run `RemoteLibvirtDiscovery` against the operator host; assert `host_cpu.model` non-empty; for a model pinned as known-in-table, assert `baseline_level` ≥ `x86-64-v2`; for an unmapped model assert `baseline_level` absent (never a wrong level). (Spec AC#10.)
- `test_host_cpu_matches_running_domain`: read the running domain's CPU with `domain.XMLDesc(libvirt.VIR_DOMAIN_XML_UPDATE_CPU)`; if it carries a concrete `<model>`, assert equality with the advertised `host_cpu.model`; else `pytest.skip("host does not expand host-model in live XML")`. (Spec AC#11, deterministic.)

- [ ] **Step 2: Run the live suite locally (best-effort) and confirm clean skip without env**

Run: `uv run python -m pytest tests/integration/test_remote_host_cpu_live.py -q`
Expected: SKIP with an explicit reason when the remote host env is absent (never a hard fail).

- [ ] **Step 3: Write the operator docs rollout note**

Add to the remote-libvirt host-registration operator doc a short note: existing remote hosts must be re-registered to gain `host_cpu`; `host_cpu`/`resolved_cpu` are registration-/mint-time snapshots (may lag a host CPU/microcode/libvirt change), so re-register after such a change; `baseline_level` is advisory (nominal, not a floor). Use plain prose; no `just` in operator walkthroughs (use `python -m kdive` / `scripts/*.sh`).

- [ ] **Step 4: Run full guardrails and regenerate generated docs**

Run: `just ci`
If a generated doc drifts (e.g. a tool-schema/capability reference snapshot changed by the new `host_cpu`/`resolved_cpu` fields or the updated wrapper docstrings), regenerate it with the repo's generator recipe (check `just --list` for a `*-docs` target such as `just config-docs`/`just docs`) rather than hand-editing, and include it in the commit. Re-run `just ci` until green.
Expected: all green (lint, type, lint-shell, lint-workflows, check-mermaid, test).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_remote_host_cpu_live.py docs/  # + any regenerated generated docs
git commit -m "test(980): live host_cpu discovery + reconcile proofs; operator rollout note

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:**
- Surface 1 (discovery `host_cpu`, raw + normalized, guarded, args match renderer) → Tasks 1,2,3,4,5. ✓
- Surface 2 (`resolved_cpu` at mint, migration 0070, single load, `systems.get`) → Tasks 6,7. ✓
- baseline_level disable-guard + nominal-upper-bound contract → Tasks 1,5 (docstring),7. ✓
- `getDomainCapabilities` guarded / resource-not-dropped → Task 4. ✓
- local/fault unchanged (no `host_cpu`) → Tasks 4 (remote-only populate), 5 (omit test). ✓
- Acceptance criteria 1–5 → Tasks 1–5; 6 → Task 4; 7 → Task 7; 8 → Task 6; 9 → Tasks 5,7; 10,11 → Task 8; 12 (`just ci`) → Task 8. ✓
- Rollout/freshness note → Task 8. ✓

**Placeholder scan:** live-test bodies (Task 8) and some MCP-test fixtures are described against a named existing test to mirror rather than pasted verbatim, because they depend on that test's fixtures — the mirror target and exact assertions are specified. No `TBD`/"handle edge cases".

**Type consistency:** `parse_host_cpu → ParsedHostCpu` (Task 2) consumed in Task 4; `baseline_level(model, disabled_features)` (Task 1) called in Task 4; `HostCpu`/`host_cpu()` (Task 3) consumed in Tasks 4-envelope? (Task 4 builds a plain dict, not `HostCpu`, to keep discovery domain-light — read side `host_cpu()` re-validates), 5, 7; `System.resolved_cpu: dict|None` (Task 6) written in Task 7, read in Task 7 envelope. Consistent.
