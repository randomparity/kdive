# Implementation plan: remote-libvirt external Run-boot activation

Spec: [2026-09-02-remote-external-boot-activation-design.md](../specs/2026-09-02-remote-external-boot-activation-design.md).
Issue: [#2110](https://github.com/randomparity/kdive/issues/2110).

**Goal.** Give remote-libvirt the primitives that boot a System from the exact finalized kernel,
optional initrd, and command line through direct-kernel domain XML, and prove after boot that the
running kernel and effective command line are exactly the ones the plan named.

**Architecture.** One new module,
`src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`, in three layers: pure XML
projection and ADR-0583 identity digests; a closed `RemoteExternalBootDefinition` value built by a
pure `prepare_target_definition`; and two operations (`activate_definition`,
`observe_guest_identity`) over injected libvirt and guest-agent seams. Every seam is injected, as in
every other remote lifecycle module, so unit tests drive every path with no libvirt host.

**Tech stack.** Python 3.14, `uv`, pydantic v2, `defusedxml`, `xml.etree.ElementTree`, pytest.

Expected implementation size: 1100–1600 changed lines (M) — derived from the file map and the five
tasks below.

## Global constraints

These bind every task.

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults over `src` **and**
  `tests`.
- Guardrails: `just lint`, `just type`, `just test-changed` while iterating; `just ci` before push.
  Run them bare — never piped through `head`/`tail`, never with output redirected, never with
  `|| true`.
- Before `git commit`, run `just format` for a Python-only change so the mutating ruff hooks do not
  abort the commit.
- Doc style: use "Milestone", never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant", "significant", "essential", "crucial" in prose, code comments, and commit messages.
- Conventional Commits 1.0.0, imperative, subject at most 72 characters.
- Error taxonomy: pick the most specific existing `kdive.domain.errors.ErrorCategory` value. Never
  invent a string. The values this change uses are `CONFLICT`, `READINESS_FAILURE`,
  `INFRASTRUCTURE_FAILURE`, `TRANSPORT_FAILURE`, and `CONFIGURATION_ERROR`.
- Parse untrusted XML only with `defusedxml.ElementTree.fromstring`. Never with the stdlib parser.
- Do not modify anything under `src/kdive/providers/remote_libvirt/lifecycle/rootfs/` or
  `tests/providers/remote_libvirt/lifecycle/rootfs/`. Those belong to #2129.
- Do not modify `src/kdive/providers/local_libvirt/`, `src/kdive/providers/shared/`,
  `src/kdive/providers/ports/`, `src/kdive/providers/remote_libvirt/composition.py`, or
  `deploy/`. Read them; do not edit them.
- Do not set `ProviderRuntime.external_boot`. Advertisement is #2140's.
- The two ADR-0583 identity algorithms are normative. Reproduce them exactly, including the domain
  prefixes `kdive-libvirt-preserved-v1` and `kdive-libvirt-boot-projection-v1`, each followed by a
  NUL byte.

### Golden digests from ADR-0583 (transcribed exactly)

- `<domain><os><type arch="x86_64">hvm</type></os></domain>` has preserved digest
  `sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea`.
- The all-null boot projection digest is
  `sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925`.
- The projection
  `{"cmdline":"root=LABEL=café","initrd":null,"kernel":"/var/lib/kdive/café","schema":"libvirt-boot-projection-v1"}`
  has digest `sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb`.

## File map

| File | Status | Answerable for |
| --- | --- | --- |
| `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py` | created (Task 1, extended by 2–5) | the whole remote activation surface |
| `tests/providers/remote_libvirt/lifecycle/test_external_boot.py` | created (Task 1, extended by 2–5) | its tests |
| `docs/workflow/specs/2026-09-02-remote-external-boot-activation-design.md` | created (already written) | the design record |
| `docs/workflow/plans/2026-09-02-remote-external-boot-activation.md` | created (this file) | the plan |

No other file changes.

## Names borrowed from the codebase

Each was checked to exist with the signature assumed here.

| Name | Source | Signature |
| --- | --- | --- |
| `ExternalBootPlan` | `src/kdive/providers/ports/external_boot.py:159` | frozen model; `.cmdline: str`, `.initrd: InitrdSource \| None`, `.architecture`, `.ownership: PlanOwnership`, `.module_obligation.release`, `.bundle.vmlinuz_sha256`, `.identity -> str` |
| `ExternalBootMaterialization` | same file, `:248` | frozen model; `.plan_identity`, `.ownership: ActivationOwnership`, `.kernel_observation: RunningKernelObservation`, `.artifacts: MaterializedArtifacts`, `.identity -> str` |
| `MaterializedArtifacts` | same file, `:236` | `.kernel: OpaqueProviderRef`, `.modules: OpaqueProviderRef`, `.initrd: OpaqueProviderRef \| None` |
| `ExternalBootActivationBinding` | same file, `:95` | `.system_id: str`, `.run_id: str`, `.activation_id: str` (canonical UUID strings) |
| `RunningKernelObservation` | same file, `:242` | `.architecture`, `.release`, `.gnu_build_id` |
| `Digest` | same file, `:13` | `Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]` |
| `KDIVE_METADATA_NS`, `QEMU_NS`, `register_kdive_namespace`, `register_qemu_namespace` | `src/kdive/providers/shared/libvirt_xml.py:16,17,23,31` | constants and zero-argument functions |
| `overlay_volume_name` | `src/kdive/providers/remote_libvirt/lifecycle/xml.py:40` | `(system_id: UUID \| str) -> str` |
| `render_domain_xml` | same file, `:66` | `(system_id, profile, *, pool, volume, gdb_addr, gdb_port, network=…, machine=…, ssh_addr=None, ssh_port=None) -> str` |
| `domain_name_for` | `src/kdive/providers/shared/runtime_paths.py` | `(system_id: UUID \| str) -> str` |
| `AgentCommand` | `src/kdive/providers/remote_libvirt/guest/agent.py:130` | `Callable[[GuestDomain, str, int, int], str]` |
| `GuestDomain` | same file, `:32` | Protocol with `def name(self) -> str` |
| `classify_agent_libvirt_error` | same file, `:84` | `(domain: GuestDomain, exc: libvirt.libvirtError) -> CategorizedError` |
| `parse_gnu_build_id` | `src/kdive/build_artifacts/validation.py:310` | `(notes: bytes) -> str`, lowercase hex |
| `CategorizedError`, `ErrorCategory` | `src/kdive/domain/errors.py` | `CategorizedError(message, *, category, details=None)` |

Test fixtures borrowed: `tests/providers/remote_libvirt/lifecycle/test_provisioning.py` builds a
concrete `ProvisioningProfile` for `render_domain_xml`; reuse that construction rather than
inventing one. Read it before Task 1 step 1 and copy the profile construction into the new test
module (tasks repeat code rather than cross-referencing).

---

## Task 1 — the pure projection and the two ADR-0583 identities

Creates `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py` and
`tests/providers/remote_libvirt/lifecycle/test_external_boot.py`.

Where it fits: everything below consumes these three functions. This task is the one that must
reproduce the normative algorithm byte for byte, so it is proved against ADR-0583's published golden
digests before anything else is built on it.

### Interfaces

Consumes: nothing from earlier tasks.

Provides to later tasks:

```python
def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str
def preserved_definition_identity(domain_xml: str) -> str
def boot_projection_identity(domain_xml: str) -> str
def parse_domain_xml(domain_xml: str) -> ET.Element   # module-internal, shared by all of the above
```

`parse_domain_xml` raises `CategorizedError(ErrorCategory.INFRASTRUCTURE_FAILURE)` for non-NFC,
malformed, entity-bearing, or non-`domain`-rooted XML.

### Steps

1. Read `tests/providers/remote_libvirt/lifecycle/test_provisioning.py` and note how it builds a
   `ProvisioningProfile` with a concrete `remote_libvirt_section` and concrete sizing. Read
   `src/kdive/providers/remote_libvirt/lifecycle/xml.py:66-135` for the exact device set
   `render_domain_xml` emits.

2. Write the failing test file
   `tests/providers/remote_libvirt/lifecycle/test_external_boot.py` with the three golden-vector
   tests:

```python
"""Contract tests for the remote-libvirt external-boot activation primitives (#2110)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    boot_projection_identity,
    preserved_definition_identity,
    render_target_xml,
)

_GOLDEN_SOURCE = '<domain><os><type arch="x86_64">hvm</type></os></domain>'
_GOLDEN_PRESERVED = "sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea"
_GOLDEN_NULL_BOOT = "sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925"
_GOLDEN_UNICODE_BOOT = "sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb"


def test_preserved_identity_matches_the_adr_golden_vector() -> None:
    assert preserved_definition_identity(_GOLDEN_SOURCE) == _GOLDEN_PRESERVED


def test_all_null_boot_projection_matches_the_adr_golden_vector() -> None:
    assert boot_projection_identity(_GOLDEN_SOURCE) == _GOLDEN_NULL_BOOT


def test_non_ascii_boot_projection_matches_the_adr_golden_vector() -> None:
    projected = render_target_xml(
        _GOLDEN_SOURCE,
        kernel="/var/lib/kdive/café",
        initrd=None,
        cmdline="root=LABEL=café",
    )
    assert boot_projection_identity(projected) == _GOLDEN_UNICODE_BOOT
```

3. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_external_boot.py -q`.
   Expect collection to fail with
   `ModuleNotFoundError: No module named 'kdive.providers.remote_libvirt.lifecycle.external_boot'`.

4. Create `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`:

```python
"""Remote-libvirt external Run-boot activation primitives (ADR-0583, #2110).

Three layers, each testable alone: the pure direct-kernel XML projection and the two ADR-0583
definition identities; a closed ``RemoteExternalBootDefinition`` built by a pure
``prepare_target_definition``; and two operations over injected libvirt and guest-agent seams.

Recovery to the disk/GRUB baseline (#2120), offline module capture and restoration (#2129),
provider-host authority fencing (#2140), and capability advertisement (#2140) are separately owned.
This module implements no shared port and is not wired into ``ProviderRuntime``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits a trusted tree after a defused parse

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.libvirt_xml import (
    register_kdive_namespace,
    register_qemu_namespace,
)

_BOOT_FIELDS = ("kernel", "initrd", "cmdline")
_PRESERVED_PREFIX = b"kdive-libvirt-preserved-v1"
_BOOT_PROJECTION_PREFIX = b"kdive-libvirt-boot-projection-v1"


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()


def _malformed(reason: str) -> CategorizedError:
    return CategorizedError(
        f"remote-libvirt domain XML {reason}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


def parse_domain_xml(domain_xml: str) -> ET.Element:
    """Safely parse an NFC domain definition, or raise ``INFRASTRUCTURE_FAILURE``."""
    if unicodedata.normalize("NFC", domain_xml) != domain_xml:
        raise _malformed("must be NFC")
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise _malformed("is malformed or forbidden") from exc
    if root.tag != "domain":
        raise _malformed("must have a domain root")
    return root


def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str:
    """Return ``source`` with only the ADR-0583 direct-boot projection replaced.

    ``<os><boot>`` is deliberately left in place: ADR-0583 excludes only the three boot fields
    from the preserved digest, and libvirt ignores the boot device once ``<kernel>`` is set.
    """
    root = parse_domain_xml(source)
    os_element = root.find("os")
    if os_element is None:
        os_element = ET.SubElement(root, "os")
    for tag in _BOOT_FIELDS:
        element = os_element.find(tag)
        if element is not None:
            os_element.remove(element)
    ET.SubElement(os_element, "kernel").text = kernel
    if initrd is not None:
        ET.SubElement(os_element, "initrd").text = initrd
    ET.SubElement(os_element, "cmdline").text = cmdline
    register_kdive_namespace()
    register_qemu_namespace()
    return ET.tostring(root, encoding="unicode")


def preserved_definition_identity(domain_xml: str) -> str:
    """The ADR-0583 preserved digest: everything but the three provider-owned boot fields."""
    root = parse_domain_xml(domain_xml)
    cloned = ET.fromstring(ET.tostring(root, encoding="unicode"))  # noqa: S314 - defused above
    os_element = cloned.find("os")
    if os_element is not None:
        for tag in _BOOT_FIELDS:
            element = os_element.find(tag)
            if element is not None:
                os_element.remove(element)
    for element in cloned.iter():
        if len(element) and element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    canonical = ET.canonicalize(
        ET.tostring(cloned, encoding="unicode"),
        with_comments=False,
        strip_text=False,
        rewrite_prefixes=True,
    ).encode()
    return _digest(_PRESERVED_PREFIX, canonical)


def boot_projection_identity(domain_xml: str) -> str:
    """The ADR-0583 boot projection digest over the three provider-owned boot fields."""
    os_element = parse_domain_xml(domain_xml).find("os")
    value = {
        tag: os_element.findtext(tag) if os_element is not None else None for tag in _BOOT_FIELDS
    }
    value["schema"] = "libvirt-boot-projection-v1"
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(_BOOT_PROJECTION_PREFIX, payload)
```

5. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_external_boot.py -q`.
   Expect `3 passed`.

6. Append the golden device-preservation test and the projection-edge tests to the test module:

```python
def test_projection_preserves_every_remote_device_and_the_preserved_digest() -> None:
    source = render_domain_xml(
        _SYSTEM_ID,
        _profile(),
        pool="kdive",
        volume=overlay_volume_name(_SYSTEM_ID),
        gdb_addr="10.0.0.5",
        gdb_port=1234,
        ssh_addr="10.0.0.5",
        ssh_port=2222,
    )
    projected = render_target_xml(
        source, kernel="kernel.img", initrd="initrd.img", cmdline="root=/dev/vda1 console=ttyS0"
    )
    for fragment in (
        '<disk type="volume" device="disk">',
        '<driver name="qemu" type="qcow2" />',
        f'<source pool="kdive" volume="{overlay_volume_name(_SYSTEM_ID)}" />',
        '<target dev="vda" bus="virtio" />',
        '<interface type="network">',
        '<serial type="pty">',
        '<console type="pty">',
        'name="org.qemu.guest_agent.0"',
        '<vmcoreinfo state="on" />',
        'value="-gdb"',
        'value="tcp:10.0.0.5:1234"',
        "hostfwd=tcp:10.0.0.5:2222-:22",
        f"<kdive:system>{_SYSTEM_ID}</kdive:system>",
    ):
        assert fragment in projected, fragment
    assert preserved_definition_identity(projected) == preserved_definition_identity(source)
    assert boot_projection_identity(projected) != boot_projection_identity(source)
    assert "<kernel>kernel.img</kernel>" in projected
    assert "<initrd>initrd.img</initrd>" in projected
    assert "<cmdline>root=/dev/vda1 console=ttyS0</cmdline>" in projected
    assert '<boot dev="hd" />' in projected


def test_projection_omits_the_initrd_element_when_no_initrd_is_supplied() -> None:
    projected = render_target_xml(_GOLDEN_SOURCE, kernel="k", initrd=None, cmdline="c")
    assert "<initrd>" not in projected


def test_projection_replaces_rather_than_duplicates_existing_boot_fields() -> None:
    once = render_target_xml(_GOLDEN_SOURCE, kernel="k1", initrd="i1", cmdline="c1")
    twice = render_target_xml(once, kernel="k2", initrd="i2", cmdline="c2")
    assert twice.count("<kernel>") == 1
    assert twice.count("<initrd>") == 1
    assert twice.count("<cmdline>") == 1
    assert "k1" not in twice


def test_projection_creates_the_os_element_when_the_source_has_none() -> None:
    projected = render_target_xml("<domain><name>d</name></domain>", kernel="k", initrd=None, cmdline="c")
    assert "<os><kernel>k</kernel><cmdline>c</cmdline></os>" in projected


@pytest.mark.parametrize(
    "source",
    [
        "<domain>",
        '<!DOCTYPE d [<!ENTITY x "y">]><domain><name>&x;</name></domain>',
        "<not-a-domain />",
        "<domain><name>café</name></domain>",
    ],
    ids=["malformed", "entity", "wrong-root", "non-nfc"],
)
def test_projection_rejects_malformed_forbidden_or_non_nfc_sources(source: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        render_target_xml(source, kernel="k", initrd=None, cmdline="c")
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

   Add the imports and module fixtures those tests need at the top of the file:
   `from uuid import UUID`, `from kdive.providers.remote_libvirt.lifecycle.xml import
   overlay_volume_name, render_domain_xml`, a `_SYSTEM_ID = UUID("...")` constant, and a `_profile()`
   helper copied from `test_provisioning.py`'s profile construction.

7. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_external_boot.py -q`.
   Expect every test to pass. If a device fragment assertion fails, the expected string is wrong,
   not the projection — read the actual rendered XML and correct the fragment.

8. Run `just format`, then `just lint`, then `just type`. Expect each to exit 0 with no findings.

9. Commit: `feat(remote-libvirt): add the external-boot direct-kernel XML projection`.

### Acceptance criteria

- The three ADR-0583 golden digests are asserted literally and pass.
- The projection changes only `<os><kernel>`, `<os><initrd>`, and `<os><cmdline>`, and the preserved
  digest is identical across the projection of a fully-featured remote domain.
- Malformed, entity-bearing, non-NFC, and non-`domain` XML all raise
  `INFRASTRUCTURE_FAILURE`.

---

## Task 2 — remote source admission

Modifies `src/kdive/providers/remote_libvirt/lifecycle/external_boot.py` and
`tests/providers/remote_libvirt/lifecycle/test_external_boot.py`.

Where it fits: Task 3 calls this before capturing any XML as a source baseline.

### Interfaces

Consumes from Task 1: `parse_domain_xml`, `boot_projection_identity`.

Provides to Task 3:

```python
def require_disk_grub_source(domain_xml: str, *, system_id: UUID, pool: str) -> None
```

Raises `CategorizedError(ErrorCategory.CONFLICT)` on the first failed rule, with
`details={"system_id": str(system_id), "rule": <rule name>}`. Returns `None` on success.

### Steps

1. Append the failing tests. One passing case, then one failing case per rule:

```python
def _source_xml(**overrides: object) -> str:
    return render_domain_xml(
        _SYSTEM_ID,
        _profile(),
        pool=str(overrides.get("pool", "kdive")),
        volume=str(overrides.get("volume", overlay_volume_name(_SYSTEM_ID))),
        gdb_addr="10.0.0.5",
        gdb_port=1234,
    )


def test_admission_accepts_the_provisioned_disk_grub_baseline() -> None:
    require_disk_grub_source(_source_xml(), system_id=_SYSTEM_ID, pool="kdive")


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (lambda xml: render_target_xml(xml, kernel="k", initrd=None, cmdline="c"), "boot-projection"),
        (lambda xml: xml.replace(str(_SYSTEM_ID), str(_OTHER_SYSTEM_ID)), "system-metadata"),
        (lambda xml: xml.replace('pool="kdive"', 'pool="other"'), "boot-disk"),
        (lambda xml: xml.replace('dev="vda"', 'dev="sda"'), "boot-disk"),
        (lambda xml: xml.replace('type="qcow2"', 'type="raw"'), "boot-disk"),
        (lambda xml: xml.replace('<boot dev="hd" />', '<boot dev="hd" /><boot dev="network" />'), "boot-selection"),
        (lambda xml: xml.replace("<os>", '<os firmware="efi">'), "firmware"),
        (lambda xml: xml.replace("<os>", "<os><loader>/x</loader>"), "firmware"),
    ],
)
def test_admission_rejects_a_source_that_is_not_the_owned_baseline(
    mutate: Callable[[str], str], rule: str
) -> None:
    with pytest.raises(CategorizedError) as caught:
        require_disk_grub_source(mutate(_source_xml()), system_id=_SYSTEM_ID, pool="kdive")
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == rule
```

   `_OTHER_SYSTEM_ID` is a second module-level `UUID`. Add `from collections.abc import Callable`.

2. Run the test file. Expect `ImportError: cannot import name 'require_disk_grub_source'`.

3. Add the implementation to the module, after `boot_projection_identity`:

```python
_ALL_NULL_BOOT_PROJECTION = _digest(
    _BOOT_PROJECTION_PREFIX,
    json.dumps(
        {"cmdline": None, "initrd": None, "kernel": None, "schema": "libvirt-boot-projection-v1"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode(),
)


def _conflict(reason: str, *, system_id: UUID, rule: str) -> CategorizedError:
    return CategorizedError(
        f"remote-libvirt external-boot source is not the owned disk/GRUB baseline: {reason}",
        category=ErrorCategory.CONFLICT,
        details={"system_id": str(system_id), "rule": rule},
    )


def require_disk_grub_source(domain_xml: str, *, system_id: UUID, pool: str) -> None:
    """Prove an inactive definition is this System's owned disk/GRUB baseline (ADR-0583).

    Raises ``CONFLICT`` on the first failed rule. A source already carrying external-boot fields
    fails the first rule: ADR-0583 admits one only while a matching durable activation row owns
    it, and that row is state this module cannot read.
    """
    root = parse_domain_xml(domain_xml)
    if boot_projection_identity(domain_xml) != _ALL_NULL_BOOT_PROJECTION:
        raise _conflict("it already carries external-boot fields", system_id=system_id, rule="boot-projection")
    recorded = root.findtext(f"./metadata/{{{KDIVE_METADATA_NS}}}system")
    if recorded != str(system_id):
        raise _conflict("its kdive metadata names another System", system_id=system_id, rule="system-metadata")
    disks = root.findall("./devices/disk[@device='disk']")
    expected_volume = overlay_volume_name(system_id)
    if len(disks) != 1 or not _is_expected_overlay(disks[0], pool=pool, volume=expected_volume):
        raise _conflict("its boot disk is not the System overlay volume", system_id=system_id, rule="boot-disk")
    os_element = root.find("os")
    boots = os_element.findall("boot") if os_element is not None else []
    if len(boots) != 1 or boots[0].get("dev") != "hd":
        raise _conflict("disk boot is not its only boot selection", system_id=system_id, rule="boot-selection")
    if os_element is not None and (
        os_element.get("firmware") is not None
        or os_element.find("loader") is not None
        or os_element.find("nvram") is not None
    ):
        raise _conflict("it carries loader, firmware, or NVRAM fields", system_id=system_id, rule="firmware")


def _is_expected_overlay(disk: ET.Element, *, pool: str, volume: str) -> bool:
    source = disk.find("source")
    driver = disk.find("driver")
    target = disk.find("target")
    return (
        source is not None
        and driver is not None
        and target is not None
        and source.get("pool") == pool
        and source.get("volume") == volume
        and driver.get("type") == "qcow2"
        and target.get("dev") == "vda"
        and target.get("bus") == "virtio"
    )
```

   Add `from uuid import UUID`, `KDIVE_METADATA_NS` to the `libvirt_xml` import, and
   `from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name`.

4. Run the test file. Expect every test to pass.

5. Run `just format`, `just lint`, `just type`. Expect exit 0.

6. Commit: `feat(remote-libvirt): admit only the owned disk/GRUB external-boot source`.

### Acceptance criteria

- The provisioned baseline is admitted; each of the five rules has at least one rejecting case that
  asserts both `CONFLICT` and the rule name.
- No rule is checked after an earlier one has failed.

---

## Task 3 — the closed definition value and its pure builder

Modifies the same two files.

Where it fits: Tasks 4 and 5 both take a `RemoteExternalBootDefinition`.

### Interfaces

Consumes from Tasks 1 and 2: `render_target_xml`, `preserved_definition_identity`,
`boot_projection_identity`, `require_disk_grub_source`.

Provides to Tasks 4 and 5:

```python
class RemoteExternalBootDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    source_xml: str
    source_definition: Digest
    source_boot: Digest
    target_xml: str
    target_definition: Digest
    target_boot: Digest
    expected_running: RunningKernelObservation
    expected_cmdline: str


def prepare_target_definition(
    source_xml: str,
    *,
    plan: ExternalBootPlan,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
    pool: str,
    kernel_path: str,
    initrd_path: str | None,
) -> RemoteExternalBootDefinition
```

### Steps

1. Append the failing tests: a happy path asserting every field; ownership mismatch between
   `binding` and `plan.ownership`; mismatch between `binding` and `materialization.ownership`;
   `materialization.plan_identity != plan.identity`; an `initrd_path` supplied when the plan has no
   initrd and the reverse; a model instance whose `target_definition` is edited to a wrong digest
   (construct via `model_validate` on a mutated dump) rejected by the validator. Build the plan and
   materialization with small module-level factory helpers rather than fixtures, so each test reads
   on its own.

2. Run the test file. Expect `ImportError: cannot import name 'RemoteExternalBootDefinition'`.

3. Implement. The validator recomputes `preserved_definition_identity` and
   `boot_projection_identity` over `source_xml` and `target_xml` and requires the four recorded
   digests to equal them, raising `ValueError` otherwise. `prepare_target_definition` validates
   ownership and plan/materialization agreement first (raising
   `CategorizedError(ErrorCategory.CONFLICT)` with a `rule` detail), calls
   `require_disk_grub_source`, then renders the target from `kernel_path`, `initrd_path`, and
   `plan.cmdline` verbatim, and sets `expected_running = materialization.kernel_observation` and
   `expected_cmdline = plan.cmdline`.

   `initrd_path` must be supplied exactly when `plan.initrd is not None` and
   `materialization.artifacts.initrd is not None`; a disagreement among the three raises `CONFLICT`
   with `rule="initrd-presence"`.

4. Run the test file. Expect every test to pass.

5. Run `just format`, `just lint`, `just type`. Expect exit 0.

6. Commit: `feat(remote-libvirt): derive the external-boot target definition`.

### Acceptance criteria

- The value round-trips through `model_dump_json` / `model_validate_json`.
- A definition whose recorded digest does not recompute from its own XML cannot be constructed.
- `expected_cmdline` is `plan.cmdline` with no tokenizing, quoting, or normalization.

---

## Task 4 — the activation operation

Modifies the same two files.

Where it fits: this is the compare-and-set write #2118 calls and #2140 fences.

### Interfaces

Consumes from Task 3: `RemoteExternalBootDefinition`.

Provides:

```python
class ActivationDomain(Protocol):
    def isActive(self) -> int: ...                              # noqa: N802 - binding name
    def XMLDesc(self, flags: int = 0) -> str: ...               # noqa: N802 - binding name
    def create(self) -> int: ...


class ActivationConn(Protocol):
    def lookupByName(self, name: str) -> ActivationDomain: ...  # noqa: N802 - binding name
    def defineXML(self, xml: str) -> ActivationDomain: ...      # noqa: N802 - binding name


def activate_definition(conn: ActivationConn, definition: RemoteExternalBootDefinition) -> None
```

### Steps

1. Append the failing tests. Write a `_FakeDomain` that stores the XML it was defined with, returns
   it from `XMLDesc`, flips `isActive` on `create()`, and records every call; and a `_FakeConn`
   whose `defineXML` replaces the stored XML. The double must model libvirt's real behavior — it
   stores and returns what libvirt would, and `create()` genuinely changes power state. Then a
   parametrized matrix over observed XML in `{source, target, other}` crossed with power in
   `{inactive, active}`:

   | observed XML | power | expected |
   | --- | --- | --- |
   | source | inactive | defines target, then starts; both recorded |
   | source | active | `CONFLICT`, no define, no create |
   | target | inactive | starts only; no define |
   | target | active | no define, no create, returns |
   | other | inactive | `CONFLICT`, no define, no create |
   | other | active | `CONFLICT`, no define, no create |

   Plus: a define that does not read back as the target raises `CONFLICT` after the define, and a
   domain lookup raising `libvirt.libvirtError` propagates as `INFRASTRUCTURE_FAILURE`.

2. Run the test file. Expect `ImportError: cannot import name 'activate_definition'`.

3. Implement, comparing `XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)` against the recorded XML strings
   exactly. Every conflict carries
   `details={"system_id": …, "run_id": …, "activation_id": …, "observed_definition": <preserved
   digest>, "observed_boot": <boot projection digest>, "active": <bool>}` — digests, never XML
   bytes. Document in the docstring that ADR-0583's pre-write authority gate is the caller's
   obligation (#2140) and that this function performs the state half of the gate.

4. Run the test file. Expect every test to pass.

5. Run `just format`, `just lint`, `just type`. Expect exit 0.

6. Commit: `feat(remote-libvirt): activate the external-boot target definition`.

### Acceptance criteria

- Only the two admitted cells write; the other four record no `defineXML` and no `create`.
- Re-running against an already-active target domain is a no-op that raises nothing.
- No conflict detail contains domain XML.

---

## Task 5 — guest identity observation

Modifies the same two files.

Where it fits: the last step of activation, and the proof criterion 4 of the spec names.

### Interfaces

Consumes from Task 3: `RemoteExternalBootDefinition`.

Provides:

```python
MAX_GUEST_READ_BYTES = 65_536
PROC_CMDLINE_PATH = "/proc/cmdline"
KERNEL_NOTES_PATH = "/sys/kernel/notes"


def observe_guest_identity(
    agent_command: AgentCommand,
    domain: GuestDomain,
    definition: RemoteExternalBootDefinition,
    *,
    call_timeout_s: int = 30,
) -> RunningKernelObservation
```

### Steps

1. Append the failing tests. Write a `_FakeAgent` that maps a QGA command name to a canned JSON
   reply, so each test states exactly what the guest returned. Cases: a happy path returning the
   expected observation; a wrong `kernel-release`; a wrong `machine`; a `machine` the shared
   contract does not name; a wrong build ID; `/proc/cmdline` differing by one byte;
   `/proc/cmdline` with no trailing newline; `/proc/cmdline` with two trailing newlines (only one
   is stripped, so it must fail); a read exceeding `MAX_GUEST_READ_BYTES`; empty or malformed ELF
   notes; a non-JSON agent reply; a reply missing `return`; and a
   `libvirt.libvirtError` raised by the agent. Each asserts the category from the spec's failure
   table, and each identity-mismatch case asserts that no guest bytes appear in
   `caught.value.details`.

   Also assert the file handle is closed on the failure paths: the fake records
   `guest-file-close` calls, and every case that opened a file must have closed it.

2. Run the test file. Expect `ImportError: cannot import name 'observe_guest_identity'`.

3. Implement:

   - `_agent_call(agent_command, domain, command, call_timeout_s) -> dict[str, Any]` wraps one
     round trip: on `libvirt.libvirtError` raise `classify_agent_libvirt_error(domain, exc)`; on a
     non-JSON or non-object reply raise `INFRASTRUCTURE_FAILURE`.
   - `_read_guest_file(agent_command, domain, path, call_timeout_s) -> bytes` runs
     `guest-file-open` with `{"path": path, "mode": "rb"}`, then one `guest-file-read` with
     `{"handle": h, "count": MAX_GUEST_READ_BYTES}`, then `guest-file-close` in a `finally`.
     Base64-decodes `buf-b64`; a reply that is not `eof` means the file exceeded the cap and raises
     `READINESS_FAILURE`.
   - Read `guest-get-osinfo` for `machine` and `kernel-release`.
   - Read `PROC_CMDLINE_PATH`, remove exactly one trailing `b"\n"`, and require the result to equal
     `definition.expected_cmdline.encode()`.
   - Read `KERNEL_NOTES_PATH` and call `parse_gnu_build_id`.
   - Build a `RunningKernelObservation` through its own validators, so an out-of-contract
     architecture, release, or build ID is rejected by the shared model; wrap the resulting
     `ValidationError` as `READINESS_FAILURE`.
   - Require the built observation to equal `definition.expected_running` exactly; otherwise
     `READINESS_FAILURE`.
   - Every `READINESS_FAILURE` detail carries the System, Run, and activation ids and a
     `mismatch` field naming which of `architecture`, `release`, `gnu_build_id`, or `cmdline`
     differed — never the observed value.

4. Run the test file. Expect every test to pass.

5. Run `just format`, `just lint`, `just type`. Expect exit 0.

6. Run `just test-changed`. Expect exit 0.

7. Commit: `feat(remote-libvirt): prove external-boot kernel and command-line identity`.

### Acceptance criteria

- A guest that reports a different release, architecture, build ID, or command line fails
  `READINESS_FAILURE`, which the taxonomy already marks non-retryable.
- An unreachable or transiently failing agent fails `TRANSPORT_FAILURE`, which stays retryable.
- No observed guest byte reaches an error message or `details` payload.
- Every opened guest file handle is closed on every path.

---

## Verification

After Task 5, run `just ci` bare from the worktree root. Expect exit 0. Note that `just
check-mermaid` needs `npm ci` run once in `.github/scripts/mermaid-check/` in a fresh worktree
(#2156).

## Deferrals

None recorded at plan time. Review deferrals are appended here.
