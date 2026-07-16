# Manage and expose the guest CPU (local + cross-arch) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend #980/ADR-0368 to local-libvirt and cross-arch — advertise `host_cpu` + a per-arch `selectable_cpus` allow-list at local discovery, let an agent pin the guest CPU model (validated fail-closed against that allow-list), and make `resolved_cpu` a live-verified reading of the running local domain (#1227).

**Architecture:** Three phases, one PR, phased commits. **Phase A** (discovery visibility): local discovery advertises a flat native `host_cpu` and a per-arch `selectable_cpus` map, after the ADR-0368 mint snapshot is made explicitly remote-only. **Phase B** (control): an optional `cpu.model` profile pin, admission-validated against `selectable_cpus[arch]`, rendered `<cpu mode='custom'>`; unpinned XML byte-identical. **Phase C** (live-verified resolved): the local provider reads the running domain's resolved `<cpu>` (passthrough → host `<cpu>` fallback; TCG-default → best-effort NULL) and the job handler persists it via a state-guarded write on both provision and reprovision.

**Tech Stack:** Python 3.14, `uv`, `pytest -n auto`, `ruff`, `ty`, psycopg/psycopg_pool, defusedxml, libvirt-python. Design: [`2026-07-16-guest-cpu-manage-expose-1227.md`](2026-07-16-guest-cpu-manage-expose-1227.md). ADR: [`../adr/0369-manage-expose-guest-cpu-local-crossarch.md`](../adr/0369-manage-expose-guest-cpu-local-crossarch.md).

## Global Constraints

- **Guardrail suite:** `just ci` = `lint` (`ruff check` + `ruff format --check`), `type` (`ty check`, whole tree), `lint-shell`, `lint-workflows`, `check-mermaid`, `test` (`pytest -m "not live_vm and not live_stack" -n auto`). Run relevant tests per task; run full `just ci` before the PR.
- **Single test:** `uv run python -m pytest <path>::<name> -q`.
- **Ruff:** line length 100; lint set `E,F,I,UP,B,SIM`. **Absolute imports only** (no `..`).
- **Types:** `ty` strict, whole tree (src + tests). C-extension deps (`libvirt`, `drgn`) suppress `unresolved-import` with a scoped per-site `# ty: ignore[unresolved-import]`; libvirt binding camelCase methods use `# noqa: N802`. A `dict(TypedDict)` is `dict[str, object]`; when a `dict[str, JsonValue]` is required use `cast("dict[str, JsonValue]", dict(x))` (see #980's `host_cpu_json`).
- **PEP 758 / ruff gotcha:** write `except (A, B) as _exc:` (parenthesised, named) — ruff-format on py3.14 strips bare-tuple `except (A, B):` parens and can silently roll back a commit. Always verify `git log -1` advanced after committing.
- **Doc-style guard:** never "Sprint"/"critical"/"robust"/"comprehensive"/"elegant"/"significant" in docs, ADRs, commit messages, comments.
- **Wrapper docstring is the agent contract:** when a tool's returned `data` fields or an input `Field` change, update the `@app.tool` wrapper docstring / `Field` text, not only the handler (AGENTS.md). Then regenerate any generated doc (`just docs` / a `just --list` `*-docs` recipe) or CI docs-check fails.
- **defusedxml** for any XML crossing the libvirtd trust boundary; a parse fault returns a sentinel (`None`/`{}`/`[]`), never raises into discovery.
- **No new migration.** `systems.resolved_cpu` (jsonb) already exists (mig0070, #980) and is already in the `SYSTEMS` repository `json_columns`. This feature adds no schema change.
- **Commits:** conventional format, imperative ≤72-char subject, end every commit with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Stage explicit paths (never `git add -A`).
- **Reuse #980:** `HostCpu` TypedDict, `HOST_CPU_KEY`, `host_cpu()` reader, `host_cpu_json` serializer, and `cpu_baseline.baseline_level(model, disabled)` already exist and are reused verbatim — do not duplicate.

## File structure

| Path | Responsibility | Task |
|------|----------------|------|
| `src/kdive/services/systems/admission.py` (modify) | gate the ADR-0368 `resolved_cpu` mint snapshot to remote-only | 1 |
| `src/kdive/providers/shared/libvirt_xml.py` (modify) | `parse_host_capabilities_cpu()`, `parse_selectable_cpus()`, `parse_domain_resolved_cpu()` | 2, 8 |
| `src/kdive/domain/catalog/resource_capabilities.py` (modify) | `SELECTABLE_CPUS_KEY`, `selectable_cpus()` reader, `_KNOWN_KEYS` | 3 |
| `src/kdive/providers/local_libvirt/discovery.py` (modify) | widen `_LibvirtConn`; advertise native `host_cpu` + per-arch `selectable_cpus`, guarded | 4 |
| `src/kdive/mcp/tools/_resource_envelopes.py` (modify) | flatten `selectable_cpus` into envelope `data` (host_cpu already flattened by #980) | 5 |
| `src/kdive/mcp/tools/catalog/resources.py` (modify) | `resources.list/describe` wrapper docstrings — `host_cpu`/`selectable_cpus`/ISA-floor contract | 5 |
| `src/kdive/profiles/provisioning.py` (modify) | `LibvirtCpuPin` frozen model + `LibvirtProfile.cpu` field + `Field` ISA-floor text | 6 |
| `src/kdive/providers/local_libvirt/lifecycle/xml.py` (modify) | `_append_guest_cpu` custom-mode `cpu_model` param; thread through both renderers | 7 |
| `src/kdive/services/systems/admission.py` (modify) | per-arch pin validation against `selectable_cpus[arch]` | 8a |
| `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (modify) | thread `cpu.model` into render; `read_resolved_cpu()` provider method | 7, 9 |
| `src/kdive/db/repositories.py` (modify) | generic state-guarded `set_json_column()` write | 10 |
| `src/kdive/jobs/handlers/systems.py` (modify) | persist `resolved_cpu` at the provision/reprovision READY boundary | 10 |
| `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (modify) | `systems.get` wrapper docstring — `resolved_cpu` split contract | 10 |
| `tests/...` (create/modify) | unit/service/live tests per task | all |
| operator docs (modify) | local host re-discovery + pin/ISA-floor rollout note | 11 |

---

## Phase A — discovery visibility

### Task 1: Gate the ADR-0368 `resolved_cpu` mint snapshot to remote-only

**Why first (bisect-safety):** `_resolve_new_system_bindings` snapshots `host_cpu_json(caps)` into `resolved_cpu` for *both* providers, returning `None` for local only because local advertises no `host_cpu` **today**. The moment Task 4 makes local advertise `host_cpu`, this path would stamp the native host CPU onto every local System (wrong for a pin, arch-mismatched for TCG). Encoding the remote-only invariant **before** Task 4 keeps every intermediate commit correct. A no-op behaviour change today (local still returns `None`), so existing tests stay green.

**Files:**
- Modify: `src/kdive/services/systems/admission.py` (`_resolve_new_system_bindings`, lines ~258-289)
- Test: `tests/services/systems/` (the admission mint tests that cover accel/resolved_cpu)

**Interfaces:**
- Consumes: the bound Resource / profile provider kind (to decide remote vs local).
- Produces: `_resolve_new_system_bindings` returns `resolved_cpu = host_cpu_json(caps)` **only** when the System is remote-libvirt; `None` for local/fault regardless of advertised `host_cpu`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/systems/test_admission_resolved_cpu.py (add; mirror existing accel admission tests)
# A LOCAL profile bound to a Resource that DOES advertise host_cpu must mint resolved_cpu = None
# (Phase C, not the mint snapshot, is the local source).
async def test_local_mint_does_not_snapshot_host_cpu(...):
    # Arrange: a local-libvirt profile; a bound Resource whose capabilities carry host_cpu.
    # Act: mint the System via the create-lane admission path.
    # Assert: the persisted System.resolved_cpu is None (no native snapshot).
    ...

# A REMOTE profile bound to a Resource with host_cpu still snapshots it (regression, ADR-0368).
async def test_remote_mint_still_snapshots_host_cpu(...):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/services/systems/test_admission_resolved_cpu.py -q`
Expected: `test_local_mint_does_not_snapshot_host_cpu` FAILS (today local resolved_cpu is None only incidentally — assert it stays None *after* Task 4 too; write the test so it pins the remote-only gate, e.g. by using a Resource that advertises host_cpu for a local profile).

- [ ] **Step 3: Implement the remote-only gate**

In `_resolve_new_system_bindings`, decide remote-vs-local from the profile (`profile.provider.remote_libvirt` is not `None`) or the bound Resource kind — pick the signal already available at this call site (the profile is a parameter). Gate the snapshot:

```python
    resource = await RESOURCES.get(conn, resource_id) if resource_id is not None else None
    caps = resource.capability_view if resource is not None else None
    accel = resolve_accel(caps.guest_arches(), profile.arch) if caps is not None else None
    require_fadump_supported(
        requested=profile_policy.fadump_provisioned(profile),
        supported=caps is not None and caps.pseries_fadump(),
    )
    # ADR-0369: the mint-time host_cpu snapshot is REMOTE-ONLY. Local resolved_cpu is a live read
    # (Phase C), not a mint snapshot of the native host CPU (wrong for a pin / a foreign-TCG guest).
    # Use `remote_libvirt_section` (a plain optional field), NOT the `remote_libvirt` property —
    # that property RAISES AttributeError for a local/fault profile (provisioning.py:257-261), and
    # the caller's `except CategorizedError` would not catch it, aborting every local provision.
    is_remote = profile.provider.remote_libvirt_section is not None
    resolved_cpu = host_cpu_json(caps) if is_remote else None
    return accel, resolved_cpu
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/systems -q`
Expected: PASS (new + existing accel/fadump admission tests unchanged). `uv run ty check`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/systems/admission.py tests/services/systems/test_admission_resolved_cpu.py
git commit -m "feat(1227): gate resolved_cpu mint snapshot to remote-only

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Parse host `<cpu>` and custom-mode usable models

**Files:**
- Modify: `src/kdive/providers/shared/libvirt_xml.py`
- Test: `tests/providers/test_libvirt_xml.py`

**Interfaces:**
- Produces:
  - `parse_host_capabilities_cpu(caps_xml: str) -> ParsedHostCpu | None` — reads the `getCapabilities()` `<host><cpu><model>`/`<vendor>` block (the passthrough-honest host CPU). Returns `None` on parse fault / no `<model>`. Reuses the existing `ParsedHostCpu` dataclass (`disabled_features` is empty here — the host block has no `<feature policy='disable'>`).
  - `parse_selectable_cpus(dom_caps_xml: str) -> list[str]` — sorted, de-duplicated `<cpu><mode name='custom'><model usable='yes'>` names; `[]` on fault / no custom mode.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/test_libvirt_xml.py
from kdive.providers.shared.libvirt_xml import (
    ParsedHostCpu, parse_host_capabilities_cpu, parse_selectable_cpus,
)

_HOST_CAPS = """
<capabilities><host><cpu>
  <arch>x86_64</arch><model>SapphireRapids</model><vendor>Intel</vendor>
</cpu></host></capabilities>
"""

_DOMCAPS_CUSTOM = """
<domainCapabilities><cpu>
  <mode name='custom' supported='yes'>
    <model usable='yes'>qemu64</model>
    <model usable='no'>Denverton</model>
    <model usable='yes'>x86-64-v2</model>
    <model usable='yes'>SapphireRapids</model>
  </mode>
</cpu></domainCapabilities>
"""


def test_parse_host_capabilities_cpu():
    assert parse_host_capabilities_cpu(_HOST_CAPS) == ParsedHostCpu(
        model="SapphireRapids", vendor="Intel", arch="x86_64", disabled_features=frozenset()
    )


def test_parse_host_capabilities_cpu_none_on_malformed():
    assert parse_host_capabilities_cpu("<nope") is None
    assert parse_host_capabilities_cpu("<capabilities><host><cpu/></host></capabilities>") is None


def test_parse_selectable_cpus_usable_only_sorted():
    assert parse_selectable_cpus(_DOMCAPS_CUSTOM) == ["SapphireRapids", "qemu64", "x86-64-v2"]


def test_parse_selectable_cpus_empty_on_no_custom_mode():
    assert parse_selectable_cpus("<domainCapabilities><cpu/></domainCapabilities>") == []
    assert parse_selectable_cpus("<nope") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k "host_capabilities_cpu or selectable_cpus" -q`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Write minimal implementation**

Add beside `parse_host_cpu` in `libvirt_xml.py` (reuse `_safe_fromstring`, `ET`, `_log`, `DefusedXmlException`, and the existing `ParsedHostCpu`):

```python
def parse_host_capabilities_cpu(caps_xml: str) -> ParsedHostCpu | None:
    """Read the host's own ``<host><cpu>`` from a ``getCapabilities`` document (ADR-0369).

    The passthrough-honest host CPU: a ``host-passthrough`` guest gets exactly this CPU, so it is
    the correct local-x86 ``host_cpu`` source (the host-model block under-reports a passthrough
    guest). Returns ``None`` on parse fault or a block with no concrete ``<model>``; ``disabled_
    features`` is always empty (the host block carries no ``<feature policy='disable'>``).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse host capabilities for host cpu", exc_info=True)
        return None
    cpu = root.find("./host/cpu")
    if cpu is None:
        return None
    model = (cpu.findtext("model") or "").strip()
    if not model:
        return None
    vendor = (cpu.findtext("vendor") or "").strip() or None
    arch = (cpu.findtext("arch") or "").strip() or None
    return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=frozenset())


def parse_selectable_cpus(dom_caps_xml: str) -> list[str]:
    """Sorted, de-duplicated ``custom``-mode ``usable='yes'`` model names (ADR-0369).

    The exact set libvirt accepts in a ``<cpu mode='custom'><model>`` for this (arch, machine,
    virttype) — the host-derived allow-list for the CPU pin. ``[]`` on parse fault, an unsupported
    custom mode, or an empty usable set (discovery omits the key rather than advertising ``[]``).
    """
    try:
        root: ET.Element = _safe_fromstring(dom_caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain capabilities for selectable cpus", exc_info=True)
        return []
    models = {
        name.strip()
        for mode in root.findall("./cpu/mode")
        if mode.get("name") == "custom" and mode.get("supported") != "no"
        for model in mode.findall("model")
        if model.get("usable") == "yes" and (name := (model.text or "")).strip()
    }
    return sorted(models)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k "host_capabilities_cpu or selectable_cpus" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/libvirt_xml.py tests/providers/test_libvirt_xml.py
git commit -m "feat(1227): parse host <cpu> and custom-mode usable models

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `selectable_cpus()` capability reader

**Files:**
- Modify: `src/kdive/domain/catalog/resource_capabilities.py`
- Test: `tests/domain/catalog/test_resource_capabilities.py`

**Interfaces:**
- Produces: `SELECTABLE_CPUS_KEY = "selectable_cpus"`; `ResourceCapabilities.selectable_cpus() -> dict[str, list[str]]` — a per-arch map, defensive: drops non-string arch keys / non-list values / non-string models, returns `{}` when absent/malformed (mirrors `guest_arches()`). `SELECTABLE_CPUS_KEY` added to `_KNOWN_KEYS`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/domain/catalog/test_resource_capabilities.py
from kdive.domain.catalog.resource_capabilities import ResourceCapabilities


def test_selectable_cpus_per_arch():
    caps = ResourceCapabilities.from_mapping(
        {"selectable_cpus": {"x86_64": ["qemu64", "x86-64-v2"], "ppc64le": ["POWER9", "POWER10"]}}
    )
    assert caps.selectable_cpus() == {
        "x86_64": ["qemu64", "x86-64-v2"], "ppc64le": ["POWER9", "POWER10"]
    }


def test_selectable_cpus_absent_is_empty():
    assert ResourceCapabilities.from_mapping({}).selectable_cpus() == {}


def test_selectable_cpus_drops_malformed():
    caps = ResourceCapabilities.from_mapping(
        {"selectable_cpus": {"x86_64": ["ok", 7], "bad": "notalist", 9: ["x"]}}
    )
    assert caps.selectable_cpus() == {"x86_64": ["ok"]}
    assert ResourceCapabilities.from_mapping({"selectable_cpus": "nope"}).selectable_cpus() == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -k selectable_cpus -q`
Expected: FAIL (`selectable_cpus` not defined).

- [ ] **Step 3: Write minimal implementation**

Add the key near `GUEST_ARCHES_KEY`/`HOST_CPU_KEY`, add it to `_KNOWN_KEYS`, and add the reader:

```python
SELECTABLE_CPUS_KEY = "selectable_cpus"
```

```python
    def selectable_cpus(self) -> dict[str, list[str]]:
        """The per-arch pinnable CPU model allow-list (ADR-0369), or ``{}`` if absent/malformed.

        Defensive over the persisted JSON (mirrors :meth:`guest_arches`): keeps only string arch
        keys whose value is a list, and within each list only string model names. Any other shape
        (a stale/hand-edited row) drops to ``{}`` / the malformed entry is skipped.
        """
        raw = self._values.get(SELECTABLE_CPUS_KEY)
        if not isinstance(raw, Mapping):
            return {}
        result: dict[str, list[str]] = {}
        for arch, models in raw.items():
            if not isinstance(arch, str) or not isinstance(models, list):
                continue
            names = [m for m in models if isinstance(m, str)]
            if names:
                result[arch] = names
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -k selectable_cpus -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/domain/catalog/resource_capabilities.py tests/domain/catalog/test_resource_capabilities.py
git commit -m "feat(1227): per-arch selectable_cpus capability reader

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Local discovery advertises native `host_cpu` + per-arch `selectable_cpus`, guarded

**Files:**
- Modify: `src/kdive/providers/local_libvirt/discovery.py` (widen `_LibvirtConn` with `getDomainCapabilities`; extend `list_resources`)
- Test: `tests/providers/local_libvirt/test_discovery.py` (+ the fake connection used there)

**Interfaces:**
- Consumes: `parse_host_capabilities_cpu`, `parse_selectable_cpus` (Task 2), `parse_host_cpu` + `baseline_level` (#980), `HOST_CPU_KEY`/`SELECTABLE_CPUS_KEY` (Task 3), the parsed host `arch`, the `guest_arches` accel map (already computed at `list_resources` line ~204), `arch_traits`.
- Produces: `capabilities["host_cpu"]` = the flat native `HostCpu` dict (native arch = the `guest_arches` entry whose `accel == "kvm"` ∩ host arch); `capabilities["selectable_cpus"]` = `{arch: [models]}` for each advertised guest arch. Each guarded: a raise / empty parse omits only that field/arch.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/local_libvirt/test_discovery.py
# A fake conn whose getCapabilities carries an x86_64 host <cpu> (SapphireRapids) and whose
# getDomainCapabilities(arch, machine, virttype) returns a custom-mode usable list per arch.
def test_local_discovery_advertises_native_host_cpu_and_per_arch_selectable(...):
    record = discovery.list_resources()[0]
    assert record.capabilities["host_cpu"] == {
        "model": "SapphireRapids", "vendor": "Intel", "arch": "x86_64",
        "baseline_level": "x86-64-v4",
    }
    assert record.capabilities["selectable_cpus"]["x86_64"] == ["SapphireRapids", "qemu64", "x86-64-v2"]


def test_local_discovery_host_cpu_omitted_when_getcapabilities_cpu_absent(...):
    # host <cpu> block missing -> host_cpu omitted, resource still discovers with arch/guest_arches.
    record = discovery.list_resources()[0]
    assert "host_cpu" not in record.capabilities
    assert record.capabilities["arch"] == "x86_64"


def test_local_discovery_selectable_omits_arch_on_getdomaincaps_raise(...):
    # getDomainCapabilities raises for ppc64le only -> selectable_cpus has x86_64 but not ppc64le;
    # resource still discovers.
    record = discovery.list_resources()[0]
    assert "ppc64le" not in record.capabilities.get("selectable_cpus", {})
    assert "x86_64" in record.capabilities["selectable_cpus"]
```

Extend the discovery test's fake connection (in the local `test_discovery.py` fixtures) with a `getDomainCapabilities(emulatorbin, arch, machine, virttype, flags=0)` returning per-arch domcaps, and make an x86 host `getCapabilities` include a `<host><cpu>` block. Add a variant that raises `libvirt.libvirtError` for a given arch.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -k "host_cpu or selectable" -q`
Expected: FAIL.

- [ ] **Step 3: Implement guarded discovery**

Widen `_LibvirtConn` in `discovery.py` with `getDomainCapabilities` (same signature as Task-4 of #980's remote widening). Add module helpers (each ≤8 complexity), import `parse_host_capabilities_cpu`/`parse_host_cpu`/`parse_selectable_cpus`, `baseline_level`, `HOST_CPU_KEY`/`SELECTABLE_CPUS_KEY`, `arch_traits`, and `libvirt` (scoped ignore). In `list_resources`, after `guest_arches` is built:

```python
        native_arch = _native_kvm_arch(guest_arches, arch)  # arch whose accel=='kvm' & host arch
        host_cpu = _discover_local_host_cpu(conn, caps_xml, native_arch)
        selectable = _discover_selectable_cpus(conn, guest_arches)
        ...
        if host_cpu is not None:
            capabilities[HOST_CPU_KEY] = host_cpu
        if selectable:
            capabilities[SELECTABLE_CPUS_KEY] = selectable
```

Helper sketches (pin `machine`/`virttype`/`emulator` per arch from `arch_traits` + the `guest_arches` accel/emulator, mirroring the renderer — ADR-0369 §1):

```python
def _native_kvm_arch(guest_arches: Mapping[str, Mapping[str, str]], host_arch: str) -> str | None:
    """The arch that runs under KVM on this host and is the host's own arch (ADR-0369).

    accel is a per-host discovery value (guest_arches[arch]['accel']); host_cpu describes the
    single native CPU, so it is scoped to that arch. ``None`` if the host arch is not KVM-capable.
    """
    entry = guest_arches.get(host_arch)
    if entry is not None and entry.get("accel") == "kvm":
        return host_arch
    return None


def _discover_local_host_cpu(conn: _LibvirtConn, caps_xml: str, native_arch: str | None) -> dict[str, Any] | None:
    """Native host CPU: host <cpu> for x86 passthrough, host-model domcaps for native ppc64le."""
    if native_arch is None:
        return None
    if native_arch == "x86_64":
        parsed = parse_host_capabilities_cpu(caps_xml)  # passthrough-honest host block
    else:
        try:
            dom_caps = conn.getDomainCapabilities(None, native_arch, arch_traits(native_arch).machine, "kvm")
        except libvirt.libvirtError:
            _log.warning("getDomainCapabilities failed; omitting host_cpu", exc_info=True)
            return None
        parsed = parse_host_cpu(dom_caps)
    if parsed is None:
        return None
    result: dict[str, Any] = {"model": parsed.model, "arch": parsed.arch or native_arch}
    if parsed.vendor is not None:
        result["vendor"] = parsed.vendor
    level = baseline_level(parsed.model, parsed.disabled_features)
    if level is not None:
        result["baseline_level"] = level
    return result


def _discover_selectable_cpus(
    conn: _LibvirtConn, guest_arches: Mapping[str, Mapping[str, str]]
) -> dict[str, list[str]]:
    """Per-arch usable custom models; a per-arch fault omits only that arch (ADR-0369)."""
    out: dict[str, list[str]] = {}
    for guest_arch, entry in guest_arches.items():
        virttype = "kvm" if entry.get("accel") == "kvm" else "qemu"
        emulator = entry.get("emulator") if virttype == "qemu" else None
        try:
            dom_caps = conn.getDomainCapabilities(
                emulator, guest_arch, arch_traits(guest_arch).machine, virttype
            )
        except libvirt.libvirtError:
            _log.warning("getDomainCapabilities failed for %s; omitting selectable_cpus", guest_arch)
            continue
        models = parse_selectable_cpus(dom_caps)
        if models:
            out[guest_arch] = models
    return out
```

(Confirm `caps_xml`/`arch`/`guest_arches` variable names against the real `list_resources`; the emulator default for KVM is `None` — pass it with `# ty: ignore[invalid-argument-type]` if the stub types it `str`, per #980's gotcha.)

- [ ] **Step 4: Run tests + type to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -q && uv run ty check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/discovery.py tests/providers/local_libvirt/test_discovery.py
git commit -m "feat(1227): advertise local host_cpu + per-arch selectable_cpus

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Surface `selectable_cpus` + wrapper docstrings (host_cpu already surfaced)

**Files:**
- Modify: `src/kdive/mcp/tools/_resource_envelopes.py` (`resource_capability_data`)
- Modify: `src/kdive/mcp/tools/catalog/resources.py` (wrapper docstrings — agent contract)
- Test: `tests/mcp/catalog/test_resources_tools.py`

**Interfaces:**
- Consumes: `resource.capability_view.selectable_cpus()` (Task 3). `host_cpu` is already flattened by #980.
- Produces: envelope `data["selectable_cpus"]` = the per-arch map when non-empty, omitted otherwise.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/mcp/catalog/test_resources_tools.py
async def test_resource_data_includes_selectable_cpus(...):
    resp = await describe_resource(pool, ctx, resource_id)
    assert resp.structured_content["data"]["selectable_cpus"]["x86_64"] == ["qemu64", "x86-64-v2"]


async def test_resource_data_omits_selectable_cpus_when_absent(...):
    resp = await describe_resource(pool, ctx, resource_id)
    assert "selectable_cpus" not in resp.structured_content["data"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -k selectable_cpus -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `resource_capability_data`, beside the existing `host_cpu` flatten:

```python
    selectable = caps.selectable_cpus()
    if selectable:
        data["selectable_cpus"] = {arch: list(models) for arch, models in selectable.items()}
```

In `resources.py`, extend the `resources.list`/`resources.describe` **wrapper** docstrings to name both fields and the ISA-floor contract:
> `host_cpu` (the host's native CPU) and `selectable_cpus` (`{arch: [model, ...]}` — CPU models this host can pin for a System via `provisioning_profile.provider.local_libvirt.cpu.model`). Pin a portable `x86-64-vN` rung for a deterministic reproducer. **A pinned model below the rootfs image's ISA floor (x86-64-v2 for EL9/RHEL-family) produces a non-booting System — admission validates only that the host can deliver the model, not that the image can run on it.**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_resources_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/_resource_envelopes.py src/kdive/mcp/tools/catalog/resources.py tests/mcp/catalog/test_resources_tools.py
git commit -m "feat(1227): surface selectable_cpus + ISA-floor pin contract text

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase B — control knob

### Task 6: `LibvirtCpuPin` profile field

**Files:**
- Modify: `src/kdive/profiles/provisioning.py` (`LibvirtCpuPin` + `LibvirtProfile.cpu`)
- Test: `tests/profiles/test_provisioning.py`

**Interfaces:**
- Produces: `class LibvirtCpuPin(_ProfileBase)` with `model: NonEmptyStr`; `LibvirtProfile.cpu: LibvirtCpuPin | None = None`. `extra="forbid"` already rejects unknown keys. The `Field(description=...)` carries the ISA-floor contract (the agent-facing schema text).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/profiles/test_provisioning.py
from kdive.profiles.provisioning import ProvisioningProfile


def test_cpu_pin_parsed():
    prof = ProvisioningProfile.model_validate({
        "arch": "x86_64",
        "provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "rocky9"},
                                       "cpu": {"model": "x86-64-v2"}}},
    })
    assert prof.provider.local_libvirt.cpu.model == "x86-64-v2"


def test_cpu_pin_defaults_none():
    prof = ProvisioningProfile.model_validate({
        "arch": "x86_64",
        "provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "rocky9"}}},
    })
    assert prof.provider.local_libvirt.cpu is None


def test_cpu_pin_rejects_empty_model():
    import pytest
    with pytest.raises(ValueError):
        ProvisioningProfile.model_validate({
            "arch": "x86_64",
            "provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "rocky9"},
                                           "cpu": {"model": ""}}},
        })
```

(The **wire** provider key is the hyphenated alias `local-libvirt` — `ResourceKind.LOCAL_LIBVIRT.value` — not the Python field name `local_libvirt_section`. `ProviderSection` has `extra="forbid"`, so the underscore key is rejected. The read-side accessor is `prof.provider.local_libvirt`. Copy an existing `test_provisioning.py` profile dict to be certain.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -k cpu_pin -q`
Expected: FAIL (`cpu` field unknown / rejected by `extra="forbid"`).

- [ ] **Step 3: Implement**

Add before `LibvirtProfile`:

```python
class LibvirtCpuPin(_ProfileBase):
    """An agent-selected guest CPU model pin (ADR-0369).

    ``model`` must be one of the bound host's advertised ``selectable_cpus[arch]`` (validated at
    admission); the renderer emits ``<cpu mode='custom'><model>…</model></cpu>``. Omit ``cpu``
    entirely for the operator default (host-passthrough x86 / host-model ppc64le / TCG default).
    """

    model: NonEmptyStr = Field(
        description=(
            "Guest CPU model to pin, from this host's resources.describe `selectable_cpus[arch]`. "
            "Pin a portable `x86-64-vN` rung for a deterministic reproducer. A model below the "
            "rootfs image's ISA floor (x86-64-v2 for EL9/RHEL-family) produces a NON-BOOTING "
            "System — admission checks only that the host can deliver the model, not that the "
            "image can run on it. Omit to get the operator default (host CPU)."
        )
    )
```

Add to `LibvirtProfile` (after `debug` or beside `domain_xml_params`):

```python
    cpu: LibvirtCpuPin | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -k cpu_pin -q && uv run ty check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py
git commit -m "feat(1227): LibvirtCpuPin profile field with ISA-floor field text

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Renderer custom-mode `cpu_model` + thread through provisioning

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py` (`_append_guest_cpu`, both renderers)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (pass `cpu.model` into `render_domain_xml`)
- Test: `tests/providers/local_libvirt/test_xml.py`

**Interfaces:**
- Consumes: `LibvirtProfile.cpu` (Task 6).
- Produces: `_append_guest_cpu(domain, *, is_kvm, kvm_cpu_mode, cpu_model: str | None = None)` — when `cpu_model` is set, emit `<cpu mode='custom' check='partial'><model>NAME</model></cpu>` (KVM or TCG); when `None`, today's behaviour (byte-identical). `render_domain_xml`'s **public signature is unchanged** — it derives `cpu_model` internally from the profile (`section.cpu`) and threads it into `_build_baseline_domain`/`_append_guest_cpu`, so the provisioning call site is untouched.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/local_libvirt/test_xml.py
def test_pinned_cpu_renders_custom_mode():
    xml = render_domain_xml(system_id, _profile_with_cpu_pin("x86-64-v2"), disk_path="/d.qcow2",
                            ssh_port=2222, kernel_path=Path("/k"), initrd_path=None)
    assert "<cpu mode=\"custom\" check=\"partial\"><model>x86-64-v2</model></cpu>" in xml


def test_unpinned_cpu_byte_identical_x86_kvm():
    # No cpu pin -> exactly the pre-1227 host-passthrough block, unchanged.
    xml = render_domain_xml(system_id, _profile_no_pin(), disk_path="/d.qcow2", ssh_port=2222,
                            kernel_path=Path("/k"), initrd_path=None)
    assert "<cpu mode=\"host-passthrough\" />" in xml or "<cpu mode=\"host-passthrough\"/>" in xml
```

Also add a TCG unpinned assertion (no `<cpu>` element) and a ppc64le-KVM unpinned assertion (`host-model`), matching the existing renderer golden tests.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_xml.py -k cpu -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `_append_guest_cpu`:

```python
def _append_guest_cpu(
    domain: ET.Element, *, is_kvm: bool, kvm_cpu_mode: str, cpu_model: str | None = None
) -> None:
    """Pin the guest CPU (ADR-0340, ADR-0294, ADR-0369, #956).

    A pinned ``cpu_model`` emits ``<cpu mode='custom' check='partial'><model>…</model></cpu>``
    (valid under KVM and TCG). Unpinned: today's behaviour — ``host-passthrough``/``host-model``
    under KVM, nothing under TCG (byte-identical to pre-#1227 output).
    """
    if cpu_model is not None:
        cpu = ET.SubElement(domain, "cpu", mode="custom", check="partial")
        ET.SubElement(cpu, "model").text = cpu_model
        return
    if not is_kvm:
        return
    ET.SubElement(domain, "cpu", mode=kvm_cpu_mode)
```

Do **not** add a `cpu_model` param to the public `render_domain_xml`. Inside `render_domain_xml`, after `section = profile.provider.local_libvirt`, derive `cpu_model = section.cpu.model if section.cpu is not None else None` and pass it into `_build_baseline_domain(... cpu_model=cpu_model)` (add the `cpu_model: str | None = None` param there) → `_append_guest_cpu(... cpu_model=cpu_model)`. The provisioning call site is unchanged (it already passes the profile). Leave `render_customization_domain_xml` unpinned (a build boot has no agent pin) — do **not** thread it there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_xml.py -q`
Expected: PASS (pinned custom + all three unpinned byte-identical).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/xml.py tests/providers/local_libvirt/test_xml.py
git commit -m "feat(1227): render <cpu mode=custom> for a pinned model; unpinned byte-identical

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8a: Admission validates the pin against `selectable_cpus[arch]`

**Files:**
- Modify: `src/kdive/services/systems/admission.py` (a new pin-validation check in the mint path)
- Test: `tests/services/systems/test_admission_cpu_pin.py`

**Interfaces:**
- Consumes: `ResourceCapabilities.selectable_cpus()` (Task 3), `LibvirtProfile.cpu` (Task 6).
- Produces: mint rejects `CONFIGURATION_ERROR` (message names model + `profile.arch` + the arch's advertised set) when a pin is absent from `selectable_cpus[profile.arch]` or the arch advertises no set; accepts when present; a no-op when unpinned.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/systems/test_admission_cpu_pin.py
async def test_pin_in_arch_set_accepted(...): ...           # model in selectable_cpus["x86_64"] -> ok
async def test_pin_not_in_arch_set_rejected(...): ...        # CONFIGURATION_ERROR, message names model+arch+set
async def test_pin_in_other_arch_set_rejected(...): ...      # x86 model, ppc64le profile -> rejected
async def test_pin_when_arch_has_no_set_rejected(...): ...   # no selectable_cpus[arch] -> rejected
async def test_unpinned_profile_no_check(...): ...           # cpu None -> mint proceeds
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/systems/test_admission_cpu_pin.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add a helper called from the mint path (co-located with the accel/fadump checks in `_resolve_new_system_bindings` or the profile-policy validation — wherever the bound `caps` is already in scope). It must run against `caps.selectable_cpus()`:

```python
def require_pinned_cpu_selectable(profile: ProvisioningProfile, caps: ResourceCapabilities | None) -> None:
    """Reject a CPU pin the bound host cannot deliver for the profile arch (ADR-0369, fail-closed).

    Validates host-deliverability only — NOT that the rootfs image can run on the pinned model
    (see the pin field text). A pin with no advertised set for the arch is rejected: never render a
    custom <cpu> we cannot show the host supports.
    """
    section = profile.provider.local_libvirt
    if section is None or section.cpu is None:
        return
    allowed = caps.selectable_cpus().get(profile.arch, []) if caps is not None else []
    if section.cpu.model not in allowed:
        raise CategorizedError(
            f"CPU model {section.cpu.model!r} is not selectable for arch {profile.arch!r} on this "
            f"host; advertised: {sorted(allowed)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
```

Call it in the mint transaction where `caps` is loaded (the `_resolve_new_system_bindings` load), so a rejection consumes no capacity (before the granted→active flip).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/services/systems/test_admission_cpu_pin.py tests/services/systems -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/systems/admission.py tests/services/systems/test_admission_cpu_pin.py
git commit -m "feat(1227): validate CPU pin against per-arch selectable_cpus (fail-closed)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase C — live-verified `resolved_cpu`

### Task 8: `parse_domain_resolved_cpu` + provider `read_resolved_cpu` (passthrough fallback)

**Files:**
- Modify: `src/kdive/providers/shared/libvirt_xml.py` (`parse_domain_resolved_cpu`)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (`read_resolved_cpu`)
- Test: `tests/providers/test_libvirt_xml.py`, `tests/providers/local_libvirt/test_provisioning*.py`

**Interfaces:**
- Produces:
  - `parse_domain_resolved_cpu(domain_xml: str) -> ParsedHostCpu | None` — extract a concrete `<cpu>...<model>` from a running-domain XML (after `VIR_DOMAIN_XML_UPDATE_CPU`); `None` when the `<cpu>` has no concrete `<model>` (host-passthrough left unexpanded, TCG machine-default). `arch` from `<os><type arch=...>` if present.
  - `LocalLibvirtProvisioning.read_resolved_cpu(system_id: UUID) -> dict[str, JsonValue] | None` — open libvirt, `domain.XMLDesc(VIR_DOMAIN_XML_UPDATE_CPU)`, parse; **if the domain is host-passthrough with no concrete model, fall back to the host `getCapabilities` `<cpu>`** (the passthrough guest is the host CPU); return the `HostCpu`-shaped dict (with `baseline_level` for x86) or `None`. Best-effort: any `libvirt.libvirtError`/parse fault → `None` (logged), never raises.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/providers/test_libvirt_xml.py
def test_parse_domain_resolved_cpu_concrete_model():
    xml = ("<domain><os><type arch='x86_64'>hvm</type></os>"
           "<cpu mode='custom'><model>x86-64-v2</model></cpu></domain>")
    parsed = parse_domain_resolved_cpu(xml)
    assert parsed.model == "x86-64-v2" and parsed.arch == "x86_64"


def test_parse_domain_resolved_cpu_none_when_no_model():
    assert parse_domain_resolved_cpu("<domain><cpu mode='host-passthrough'/></domain>") is None
    assert parse_domain_resolved_cpu("<domain/>") is None
```

For `read_resolved_cpu`, unit-test with a fake libvirt connection/domain (mirror the provisioning tests' fakes): (a) domain XML with a concrete model → returns it; (b) host-passthrough unexpanded → returns the host `getCapabilities` `<cpu>` model (fallback); (c) TCG no-model → `None`; (d) a raising `XMLDesc` → `None`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k resolved_cpu tests/providers/local_libvirt -k read_resolved_cpu -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`parse_domain_resolved_cpu` in `libvirt_xml.py`:

```python
def parse_domain_resolved_cpu(domain_xml: str) -> ParsedHostCpu | None:
    """Concrete resolved ``<cpu><model>`` of a running domain (ADR-0369), or ``None``.

    ``None`` when the domain's ``<cpu>`` carries no concrete ``<model>`` (an unexpanded
    ``host-passthrough`` or a TCG machine-default) — the caller decides the fallback. defusedxml.
    """
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain xml for resolved cpu", exc_info=True)
        return None
    model = (root.findtext("./cpu/model") or "").strip()
    if not model:
        return None
    arch = (root.find("./os/type") is not None and root.find("./os/type").get("arch")) or None
    vendor = (root.findtext("./cpu/vendor") or "").strip() or None
    return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=frozenset())
```

`read_resolved_cpu` in the local provisioning class (opens libvirt like the existing lifecycle ops; reuse the connection helper the class already uses). Compose the `HostCpu` dict via a shared helper that runs `baseline_level` for x86 and omits it otherwise (reuse #980's discovery-side composition if one is extractable; else a small local `_host_cpu_dict(parsed)`), with the passthrough fallback to `parse_host_capabilities_cpu(conn.getCapabilities())`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/test_libvirt_xml.py -k resolved_cpu tests/providers/local_libvirt -k read_resolved_cpu -q && uv run ty check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/shared/libvirt_xml.py src/kdive/providers/local_libvirt/lifecycle/provisioning.py tests/providers/test_libvirt_xml.py tests/providers/local_libvirt/
git commit -m "feat(1227): live-read resolved guest cpu with passthrough host fallback

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: State-guarded persist + wire into the provision/reprovision READY boundary

**Files:**
- Modify: `src/kdive/db/repositories.py` (generic state-guarded `set_json_column`)
- Modify: `src/kdive/jobs/handlers/systems.py` (call `read_resolved_cpu` + `set_json_column` at READY)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (`systems.get` wrapper docstring)
- Test: `tests/db/test_repositories*.py`, `tests/jobs/handlers/test_systems*.py`, `tests/mcp/.../test_systems_view*.py`

**Interfaces:**
- Consumes: `LocalLibvirtProvisioning.read_resolved_cpu` (Task 8), `System.resolved_cpu` (mig0070, #980), `ResourceKind.LOCAL_LIBVIRT`, `runtime.provisioner`.
- Produces: a **generic** `StatefulRepository.set_json_column(conn, obj_id, column, value, allowed_states) -> bool` — `UPDATE {table} SET {column} = %s WHERE id = %s AND state = ANY(%s) RETURNING id`, serializing jsonb with `Jsonb` (the module's import — **not** `Json`), returning whether a row was affected (no-op on a terminal row). Generic (not a hardcoded `resolved_cpu`) so it is valid on any `StatefulRepository`; `column` is a caller-supplied `sql.Identifier`. The provision/reprovision handler, **after** the System reaches `READY`, reads the live CPU (via `asyncio.to_thread`) and calls `SYSTEMS.set_json_column(conn, system_id, "resolved_cpu", value, allowed_states=frozenset({SystemState.PROVISIONING, SystemState.READY}))` — the same code path serves first provision and reprovision (both terminate at `READY`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_repositories_resolved_cpu.py  (needs Docker; SKIPs without, KDIVE_REQUIRE_DOCKER=1 forces)
async def test_set_json_column_writes_when_ready(...):
    # System in READY -> set_json_column('resolved_cpu', ...) writes the value, returns True.
async def test_set_json_column_noop_when_terminal(...):
    # System driven to CRASHED/TORN_DOWN -> set_json_column returns False, row unchanged (None).

# tests/jobs/handlers/test_systems_resolved_cpu.py (fakes; no Docker)
async def test_provision_persists_resolved_cpu_at_ready(...):
    # provisioner.read_resolved_cpu returns a model -> handler writes it; systems row carries it.
async def test_reprovision_refreshes_resolved_cpu(...):
    # a reprovision with a new pin overwrites the prior value (or NULL on read failure).
async def test_provision_resolved_cpu_read_failure_writes_none(...):
    # read_resolved_cpu returns None -> resolved_cpu stays None, provisioning still succeeds.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/db/test_repositories_resolved_cpu.py tests/jobs/handlers/test_systems_resolved_cpu.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add a **generic** state-guarded payload write to `StatefulRepository` (so `SYSTEMS` gains it), using `Jsonb` (the module's existing import — there is no `Json` in scope) and a caller-supplied column identifier:

```python
    async def set_json_column(
        self, conn: AsyncConnection, obj_id: UUID, column: str,
        value: dict[str, JsonValue] | None, allowed_states: frozenset[S],
    ) -> bool:
        """Write a jsonb ``column`` only while the row is in ``allowed_states`` (ADR-0369).

        A state-guarded payload write (distinct from :meth:`update_state`, which transitions the
        state column). No-ops on a terminal row (crashed/reaped) so a post-provision live read
        cannot resurrect a value on a torn-down System. Returns whether a row was updated. Generic
        (``column`` is any jsonb column) so it is valid on every ``StatefulRepository``.
        """
        payload = Jsonb(value) if value is not None else None
        query = sql.SQL(
            "UPDATE {table} SET {column} = %s WHERE id = %s AND state = ANY(%s) RETURNING id"
        ).format(table=sql.Identifier(self._table), column=sql.Identifier(column))
        async with conn.cursor() as cur:
            await cur.execute(query, (payload, obj_id, [s.value for s in allowed_states]))
            return await cur.fetchone() is not None
```

**Wiring (finding-driven — this is the least-obvious part, pinned concretely).** `read_resolved_cpu` lives on `LocalLibvirtProvisioning`, **not** on the `Provisioner` protocol (`providers/ports/lifecycle.py` — it declares only `provision`/`teardown`/`reprovision`), and remote/fault-inject have no live local domain to read. So keep it local-only and narrow:

1. Do the read+write in `_execute_system_lifecycle_call` (`jobs/handlers/systems.py`) **after** the successful commit that drives the System to `READY` — that is where `runtime` (hence `runtime.provisioner`) is in scope; the `_commit_*` callbacks are typed `Callable[..., ]` and never receive the provisioner, so they are the wrong hook.
2. Gate on kind and narrow the type: only when `binding.kind is ResourceKind.LOCAL_LIBVIRT` **and** `isinstance(runtime.provisioner, LocalLibvirtProvisioning)` (satisfies ty-strict; skips remote/fault with no false attribute access).
3. Run the blocking libvirt read off the event loop, matching the handler's existing idiom (`_provider_lifecycle_call` already uses `asyncio.to_thread` — a direct blocking libvirt call starves the worker loop, cf. #583):

```python
   if binding.kind is ResourceKind.LOCAL_LIBVIRT and isinstance(provisioner, LocalLibvirtProvisioning):
       try:
           resolved = await asyncio.to_thread(provisioner.read_resolved_cpu, system_id)
       except (libvirt.libvirtError, CategorizedError) as _exc:
           _log.warning("resolved_cpu read failed; recording null", exc_info=True)
           resolved = None
       async with pool.connection() as conn:
           await SYSTEMS.set_json_column(
               conn, system_id, "resolved_cpu", resolved,
               allowed_states=frozenset({SystemState.PROVISIONING, SystemState.READY}),
           )
```

**State-set / ordering (finding-driven):** the write runs **after** the READY transition, so the row is `READY` at write time and the `{PROVISIONING, READY}` guard (the spec/ADR set) admits it; a guest that crashed/was reaped between the transition and the write has moved to `CRASHING`/`CRASHED`/`TORN_DOWN`, so the guard no-ops (no stale value). `REPROVISIONING` is **not** in the set because a reprovision also terminates at `READY` before this write — the state during reprovision is transient and never the write-time state. Confirm the exact post-commit line and that both `provision_handler` and `reprovision_handler` route through `_execute_system_lifecycle_call` (if reprovision has a separate commit path, add the same block there).

Update the `systems.get` **wrapper** docstring in `registrar.py` for `resolved_cpu`:
> `resolved_cpu` (`{model, vendor?, arch, baseline_level?}` or null): the guest CPU the System actually booted with — **live-verified for local Systems** (read from the running domain; passthrough resolves to the host CPU; a TCG machine-default the host does not expand reads null), and the **mint-time snapshot for remote Systems** (ADR-0368). Null means unrecorded/unreadable — treat as unknown.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/db/test_repositories_resolved_cpu.py tests/jobs/handlers/test_systems_resolved_cpu.py tests/mcp/tools/lifecycle/systems -q && uv run ty check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/repositories.py src/kdive/jobs/handlers/systems.py src/kdive/mcp/tools/lifecycle/systems/registrar.py tests/db/test_repositories_resolved_cpu.py tests/jobs/handlers/test_systems_resolved_cpu.py
git commit -m "feat(1227): persist live-verified resolved_cpu at READY, state-guarded

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: Live proofs, docs delta, generated-doc regen, full guardrails

**Files:**
- Create: `tests/integration/test_local_guest_cpu_live.py` (`live_vm`) + a `live_vm_tcg` proof (ppc64le, epic-#1139 box)
- Modify: operator docs (local host re-discovery + pin/ISA-floor + resolved_cpu contract)
- Modify: any generated doc that drifts — regenerate, don't hand-edit

- [ ] **Step 1: Write the live proofs**

Mirror the existing `live_vm` / `live_vm_tcg` integration idioms (env-gated, clean skip). Cover spec AC#12/#12a/#13:
- `live_vm` x86 KVM, **pinned** `x86-64-v2`: provision, assert the running domain resolves to it and `systems.get.resolved_cpu` reflects it.
- `live_vm` x86 KVM, **unpinned host-passthrough**: provision with no pin; assert `resolved_cpu` is non-NULL and equals the host `getCapabilities` `<cpu>` model; record whether the UPDATE_CPU expansion or the host-`<cpu>` fallback produced it.
- `live_vm_tcg` ppc64le: assert `resources.describe` `selectable_cpus["ppc64le"]`; record the definite Phase-C TCG outcome (concrete `<model>` matched to the running domain **or** logged NULL + the box's QEMU/libvirt version). Skips cleanly without the foreign emulator.

- [ ] **Step 2: Confirm clean skip without env**

Run: `uv run python -m pytest tests/integration/test_local_guest_cpu_live.py -q`
Expected: SKIP with explicit reasons when the live env is absent (never a hard fail).

- [ ] **Step 3: Operator docs note**

Add to the local-libvirt operator doc: existing local hosts must be **re-discovered** (`resources.reconcile`) to gain `host_cpu`/`selectable_cpus`; the CPU pin is per-System via `provisioning_profile...cpu.model` from `selectable_cpus[arch]`; a pin below the image ISA floor non-boots (admission checks host-deliverability only); `resolved_cpu` is live-verified for local, mint-snapshot for remote. Plain prose; no `just` in operator walkthroughs (use `python -m kdive` / `scripts/*.sh`).

- [ ] **Step 4: Full guardrails + regenerate generated docs**

Run: `just ci`
Regenerate any drifted generated doc (the new `selectable_cpus` field / updated wrapper docstrings will change the tool-schema/capability reference) with the repo's generator (`just --list` → a `*-docs`/`just docs` recipe); include it in the commit. Re-run `just ci` until green.
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_local_guest_cpu_live.py docs/
git commit -m "test(1227): live guest-cpu proofs; operator rollout note; regen docs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:**
- Phase A discovery `host_cpu` (native, per-mode source) + per-arch `selectable_cpus`, guarded, per-arch `getDomainCapabilities` args → Tasks 2, 3, 4. ✓
- Remote-only mint-snapshot suppression (finding-driven, bisect-ordered first) → Task 1. ✓
- Surface `selectable_cpus` + ISA-floor field text → Task 5. ✓
- Phase B pin field + custom-mode render (byte-identical unpinned) + per-arch fail-closed validation → Tasks 6, 7, 8a. ✓
- Phase C live read (passthrough host fallback, TCG best-effort NULL) + state-guarded write (no-op on terminal) + reprovision refresh + `systems.get` split contract → Tasks 8, 10. ✓
- AC#1–5 → Tasks 2,3,4,5; AC#6/8/8a → Tasks 6,7; AC#7 → Task 8a; AC#9/9a/10/11/11a/11b/11c → Tasks 1,8,10; AC#12/12a/13 → Task 11; AC#14 (`just ci`) → Task 11. ✓
- Non-x86 raw model (no invented level) → reuses #980 `baseline_level` (x86-only) unchanged; ppc64le carries model only (Tasks 2,4). ✓

**Placeholder scan:** live-test bodies (Task 11) and some handler/fixture wiring (Tasks 4, 8, 10) are specified against named existing tests/handlers to mirror, with exact assertions and the exact guard-state set — not `TBD`. The two genuinely deferred implementation details (the exact `jobs/handlers/systems.py` READY-transition line; the provider libvirt-connection helper name) are named by file and instructed to be pinned against the code, matching the spec's stated plan-time deferral.

**Type consistency:** `ParsedHostCpu` (reused, #980) produced by `parse_host_capabilities_cpu`/`parse_selectable_cpus` (Task 2, the latter returns `list[str]`) and `parse_domain_resolved_cpu` (Task 8), consumed in Tasks 4, 8. `selectable_cpus() -> dict[str, list[str]]` (Task 3) consumed in Tasks 4 (build), 5 (surface), 8a (validate). `LibvirtCpuPin.model` (Task 6) read in Tasks 7 (render), 8a (validate). `set_json_column(conn, obj_id, column, value, allowed_states) -> bool` (Task 10) with `allowed_states` a `frozenset[SystemState]`. `read_resolved_cpu -> dict[str, JsonValue] | None` (Task 8) consumed in Task 10. Consistent.

**Task ordering / bisect-safety:** Task 1 (remote-only mint gate) precedes Task 4 (local advertises `host_cpu`) so no intermediate commit stamps a wrong local `resolved_cpu`. Phase B renderer (Task 7) precedes admission validation (Task 8a) — a pin cannot reach an unrendered path since Task 8a rejects unknown pins. Phase C read (Task 8) precedes the handler wiring (Task 10).
