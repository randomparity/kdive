# libvirt storage double fidelity implementation plan

Goal: make `tests/providers/remote_libvirt/fakes.py` model what libvirt keeps for a dir-pool
storage volume instead of echoing the submitted XML, and land the unit proof and live-tier fidelity
proof that hold that modelling to account.

Architecture: one shared fixture module gains a pool double and a volume double. The pool parses a
submitted volume document into a frozen modelled state; the volume renders `XMLDesc` from that
state and never retains the document. A new `live_vm` gate in `tests/live_vm/__init__.py` probes a
libvirt session URI so a fidelity test can compare the double's readback with a real one and skip
cleanly where no daemon answers.

Tech stack: Python 3.14 under `uv`, pytest, `libvirt-python`, `xml.etree.ElementTree` (stdlib).

Design: `docs/workflow/specs/2026-09-02-libvirt-storage-double-fidelity-design.md`.

## Global constraints

- Target architectures: x86_64 and ppc64le. x86_64 work and verification complete first; the
  native ppc64le proof is deferred to a separate later run on native POWER hardware.
- Tests only. No file under `src/` changes. No new dependency, no schema, no migration, no ADR.
- Do not modify `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_documents.py`
  (owned by #2163) or migrate any existing test module to the new double (owned by other children
  of #2129).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree over `src` and `tests`.
- libvirt binding method names are camelCase and need `# noqa: N802` where the repo's fakes carry
  it today.
- Only the registered pytest markers may be used. `live_vm` is registered in `pyproject.toml`;
  no new marker is introduced.
- Doc-style guard: use "Milestone", never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in prose, comments, and commit messages.
- Guardrails while iterating: `just test-changed`, `just lint`, `just type`. Before delivery:
  `just ci`, run bare with stdin closed
  (`just ci > <file> 2>&1 < /dev/null; echo $?`), never piped through `tail` or `head`.
- `just check-mermaid` needs `npm ci` run once in `.github/scripts/mermaid-check/` in a fresh
  worktree (known gap #2156).

Expected implementation size: 600–780 changed lines (M) — derived from the file map below: the two
double classes with their suffix table, rejection paths, and `backingStore` branch (~170 lines);
the unit proof's twenty-two tests (~250); the gate with its three-check resolver and contract
(~60); its eight unit tests (~90); and the live-tier proof over a base and a backed overlay (~110).

## File map

| File | Action | Answerable for |
|---|---|---|
| `tests/providers/remote_libvirt/fakes.py` | modify | `FakeStorageVolume`, `FakeStoragePool`, and the parse/render helpers |
| `tests/providers/remote_libvirt/test_fakes_storage.py` | create | proving the double drops what it does not model |
| `tests/live_vm/__init__.py` | modify | `StorageDoubleContract`, `resolve_storage_double_contract`, `require_live_vm_storage_double` |
| `tests/live_vm/test_gates.py` | modify | the new gate's skip/fail/available unit coverage |
| `tests/live_vm/test_libvirt_storage_double_fidelity.py` | create | proving the double agrees with real libvirt |

## The modelled set

Reproduced on Fedora 44 x86_64 against libvirt daemon and libs 12.0.0 with libvirt-python 12.5.0
in the project venv. A dir-pool overlay volume — the shape `render_volume_xml` produces — reads
back as:

```xml
<volume type='file'>
  <name>overlay.qcow2</name>
  <key>/pool/target/overlay.qcow2</key>
  <capacity unit='bytes'>1048576</capacity>
  <allocation unit='bytes'>200704</allocation>
  <physical unit='bytes'>196616</physical>
  <target>
    <path>/pool/target/overlay.qcow2</path>
    <format type='qcow2'/>
    <permissions>
      <mode>0600</mode>
      <owner>1000</owner>
      <group>1000</group>
      <label>unconfined_u:object_r:user_tmp_t:s0</label>
    </permissions>
    <timestamps>
      <atime>1788388647.584020998</atime>
      <mtime>1788388647.583170806</mtime>
      <ctime>1788388647.583980117</ctime>
      <btime>0</btime>
    </timestamps>
  </target>
  <backingStore>
    <path>/pool/target/base.qcow2</path>
    <format type='qcow2'/>
    <permissions>...</permissions>
    <timestamps>...</timestamps>
  </backingStore>
</volume>
```

Facts this pins, all observed in that run:

- A submitted `<metadata>` child, a submitted `<bogusElement>` child, and a submitted
  `kdive='owned'` attribute on `<name>` are accepted by `virStorageVolCreateXML` and appear nowhere
  in the readback.
- Root `type` is **overridden**, not merely defaulted: a document declaring `type='block'` still
  reads back `type='file'` from a dir pool. The double therefore always renders `file`.
- `capacity` is **normalised to bytes**. Observed suffix table: `bytes`/`B` = 1, `K`/`KiB` = 1024,
  `KB` = 1000, `M`/`MiB` = 1048576, `MB` = 1000000, `G`/`GiB` = 1073741824, `GB` = 1000000000. An
  unknown suffix raises `VIR_ERR_INVALID_ARG` (code 8). The double converts and renders
  `unit='bytes'`.
- A document with **no `<capacity>` is rejected**: `XML error: missing capacity element`,
  `VIR_ERR_XML_ERROR` (code 27). A document with no `<target>` is accepted and reads back a full
  `target` with `format type='raw'`.
- A submitted `target/permissions/mode` **is honoured** (`0640` in, `0640` out; `0600` when none is
  submitted), so the double retains it. A submitted `<label>` is **not** — the host's own security
  label replaces it — so `label`, `owner`, and `group` stay placeholders.
- `backingStore` is present only when the submitted document carried one. The base volume, created
  without it, reads back the same six top-level children minus `backingStore`.
- `allocation`, `physical`, and `timestamps` are host facts. `allocation` (200704) and `physical`
  (196616) differ from `capacity` (1048576) and from each other, and `info()` returns
  `[0, 1048576, 200704]` — so `info()[2]` is the allocation. The double renders these as
  **placeholders**: `0` for allocation and physical with `unit='bytes'`, `0` for every timestamp.
- `label` carries the file's security label. It is present on this SELinux host and is not
  universal, so the live proof excludes it from exact comparison (Task 4).

## Task 1: model the storage double

Files:

- Modify `tests/providers/remote_libvirt/fakes.py`.
- Create `tests/providers/remote_libvirt/test_fakes_storage.py`.

Interfaces this task defines, relied on by Tasks 3 and 4:

```python
CAPACITY_SUFFIXES: dict[str, int]   # bytes/B=1, K/KiB=1024, KB=1000, M/MiB=1048576,
                                    # MB=1000000, G/GiB=1073741824, GB=1000000000

@dataclass(frozen=True, slots=True)
class VolumeState:
    name: str
    capacity_bytes: int
    format_type: str
    mode: str
    path: str
    backing_path: str | None
    backing_format: str | None

class FakeStorageVolume:
    def __init__(self, state: VolumeState, pool: FakeStoragePool) -> None: ...
    def name(self) -> str: ...
    def key(self) -> str: ...
    def path(self) -> str: ...
    def info(self) -> list[int]: ...
    def XMLDesc(self, flags: int = 0) -> str: ...
    def delete(self, flags: int = 0) -> int: ...

class FakeStoragePool:
    def __init__(self, *, name: str = "default", target_path: str = "/var/lib/libvirt/images") -> None: ...
    created_xml: list[str]
    def name(self) -> str: ...
    def createXML(self, xml: str, flags: int = 0) -> FakeStorageVolume: ...
    def storageVolLookupByName(self, name: str) -> FakeStorageVolume: ...
    def listVolumes(self) -> list[str]: ...
```

This task consumes `libvirt_error` from `tests.providers.remote_libvirt.conftest`, already imported
by `fakes.py` at line 9, with signature `libvirt_error(code: int) -> libvirt.libvirtError`.

Steps:

1. Write `tests/providers/remote_libvirt/test_fakes_storage.py` with the failing tests below, in
   one file. Each imports `FakeStoragePool` from `tests.providers.remote_libvirt.fakes`.
   - `test_readback_drops_submitted_metadata_element`: submit a volume document carrying
     `<metadata><kdive:owner xmlns:kdive='https://kdive.invalid/ns'>run-1</kdive:owner></metadata>`;
     assert `"metadata" not in volume.XMLDesc(0)` and `"run-1" not in volume.XMLDesc(0)`.
   - `test_readback_drops_unknown_elements`: submit `<bogusElement>zzz</bogusElement>`; assert
     neither `bogusElement` nor `zzz` appears in the readback.
   - `test_readback_is_not_the_submitted_document`: assert `volume.XMLDesc(0) != submitted`.
   - `test_readback_renders_the_modelled_top_level_tags`: submit a backed overlay; parse the
     readback; assert the child tag tuple equals
     `("name", "key", "capacity", "allocation", "physical", "target", "backingStore")`.
   - `test_readback_omits_backing_store_when_none_was_submitted`: submit a document with no
     `<backingStore>`; assert the child tag tuple equals
     `("name", "key", "capacity", "allocation", "physical", "target")` and that `backingStore`
     appears nowhere in the readback.
   - `test_readback_renders_the_modelled_target_tags`: assert the `target` child tag tuple equals
     `("path", "format", "permissions", "timestamps")`, that `permissions` children are
     `("mode", "owner", "group", "label")`, and that `timestamps` children are
     `("atime", "mtime", "ctime", "btime")`.
   - `test_readback_renders_the_modelled_backing_store_tags`: assert the `backingStore` child tag
     tuple equals `("path", "format", "permissions", "timestamps")`, with the same `permissions`
     and `timestamps` child tuples as `target`.
   - `test_readback_retains_submitted_fields`: assert `name` text, `target/format@type`, and
     `target/permissions/mode` equal what was submitted. Do **not** assert root `type` or
     `capacity/@unit` round-trip: libvirt overrides both.
   - `test_readback_overrides_the_submitted_root_type`: submit `<volume type='block'>`; assert the
     readback root `type` is `file`. Real libvirt takes the type from the pool backend.
   - `test_readback_normalises_capacity_to_bytes`: parametrised over `("bytes", 1, 1)`,
     `("B", 1, 1)`, `("K", 1, 1024)`, `("KiB", 1, 1024)`, `("KB", 1, 1000)`, `("M", 1, 1048576)`,
     `("MiB", 1, 1048576)`, `("MB", 1, 1000000)`, `("G", 1, 1073741824)`, `("GiB", 1, 1073741824)`,
     `("GB", 1, 1000000000)`; assert the readback `capacity` carries `unit='bytes'` and the
     converted text. The table was read off real libvirt (see *The modelled set*).
   - `test_readback_retains_the_submitted_backing_store`: assert `backingStore/path` text and
     `backingStore/format@type` equal what was submitted.
   - `test_create_rejects_a_document_with_no_capacity`: submit `<volume><name>x</name></volume>`;
     assert `pytest.raises(libvirt.libvirtError)` with
     `exc.value.get_error_code() == libvirt.VIR_ERR_XML_ERROR`, and that `pool.listVolumes()` is
     empty afterwards.
   - `test_create_rejects_an_unknown_capacity_unit`: submit `unit='bogusUnit'`; assert
     `libvirt.VIR_ERR_INVALID_ARG`.
   - `test_readback_derives_key_and_path_from_the_pool`: build the pool with
     `target_path="/pool/target"`, create `disk.qcow2`, assert `key` text, `target/path` text,
     `volume.key()`, and `volume.path()` all equal `/pool/target/disk.qcow2`.
   - `test_readback_renders_host_facts_as_placeholders`: assert `allocation` and `physical` texts
     are `"0"` with `unit='bytes'`, `permissions/owner` and `permissions/group` are `"0"`,
     `permissions/label` is empty, and every `timestamps` child is `"0"`. Real libvirt fills these
     from the file on disk (observed: allocation 200704, physical 196616 against capacity 1048576;
     a submitted SELinux label replaced by the host's own), so the double states a placeholder
     rather than a plausible-looking wrong value.
   - `test_readback_applies_defaults_for_absent_optional_input`: submit
     `<volume><name>x</name><capacity unit='bytes'>65536</capacity></volume>` — no `<target>`, no
     `<backingStore>`, no `<permissions>`; assert `target/format@type` is `raw`,
     `permissions/mode` is `0600`, and no `backingStore` element is rendered. Real libvirt accepts
     a document with no `<target>` and reads back a full one.
   - `test_readback_drops_attributes_libvirt_does_not_keep`: submit `<name kdive='owned'>x</name>`
     and a root `kdive='owned'` attribute; assert neither appears in the readback. Grounded by the
     probe under *The modelled set*, which observed both dropped by real libvirt.
   - `test_info_reports_capacity_and_placeholder_allocation`: assert `len(volume.info()) == 3`,
     `volume.info()[1]` equals the submitted capacity, and `volume.info()[2]` is `0`. Do not assert
     `info()[2] == capacity`: real libvirt answers `info()[2]` with the allocation.
   - `test_lookup_returns_the_created_volume`: assert `pool.storageVolLookupByName("disk.qcow2")`
     is the object `createXML` returned, and `pool.listVolumes() == ["disk.qcow2"]`.
   - `test_lookup_raises_no_storage_vol_for_an_unknown_name`: assert
     `pytest.raises(libvirt.libvirtError)` and `exc.value.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL`.
   - `test_delete_removes_the_volume`: create, `volume.delete()`, then assert lookup raises
     `VIR_ERR_NO_STORAGE_VOL` and `listVolumes() == []`.
   - `test_created_xml_records_every_submitted_document`: create two volumes, assert
     `pool.created_xml` holds both submitted strings in order.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`.
   Expect a collection error: `ImportError: cannot import name 'FakeStoragePool'`.
3. Add `VolumeState`, `FakeStorageVolume`, and `FakeStoragePool` to `fakes.py` with the interface
   above. `createXML` parses with `xml.etree.ElementTree.fromstring` and reads only `./name`,
   `./capacity` and its `unit`, `./target/format`'s `type` attribute, `./target/permissions/mode`,
   and `./backingStore/path` with `./backingStore/format`'s `type` attribute. It raises
   `libvirt_error(libvirt.VIR_ERR_XML_ERROR)` when `./capacity` is absent and
   `libvirt_error(libvirt.VIR_ERR_INVALID_ARG)` when its `unit` is outside `CAPACITY_SUFFIXES`,
   otherwise converting the value to bytes through that table. It does **not** read the root `type`
   attribute — libvirt overrides it. The volume's `path` is `f"{pool_target_path}/{name}"`.
   `XMLDesc` renders the modelled document from that state alone: root `type='file'`, `capacity`
   with `unit='bytes'`, `allocation` and `physical` `0` with `unit='bytes'`, the retained `mode`,
   `owner` and `group` `0`, `label` the empty string, and every timestamp `0`. It renders the
   `backingStore` branch only when `state.backing_path` is not `None`, matching libvirt, and
   renders that branch's permissions and timestamps as placeholders. Add `# noqa: N802` on `XMLDesc`,
   `createXML`, `storageVolLookupByName`, and `listVolumes`, matching the file's existing
   libvirt-binding methods. Update the module docstring to say the storage double models the
   readback rather than echoing the request, and cite #2164.
4. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`.
   Expect every test to pass.
5. Run `just lint` and `just type`. Expect both to exit 0.
6. Commit: `test(remote-libvirt): model the libvirt storage readback in the shared double`.

Acceptance criteria:

- `XMLDesc` output contains no substring taken from an unmodelled part of the submitted document.
- The readback's top-level, `target`, and `backingStore` child tag tuples equal the modelled set
  above, and no `backingStore` is rendered when none was submitted.
- `fakes.py` retains no reference to the submitted document except `created_xml`.

## Task 2: prove the double fails when it echoes

Files: none changed. This is a controlled fault against Task 1's committed state. Run the two
faults **in this order** — the second must run against the restored double, so the restore comes
between them.

Steps:

1. In the working tree, add a `submitted: str` field to `VolumeState`, set it in `createXML`, and
   make `FakeStorageVolume.XMLDesc` return `self._state.submitted`. `VolumeState` is
   `@dataclass(frozen=True, slots=True)`, so this is a field addition and not a one-line edit to
   `XMLDesc`.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`. Expect
   red on at least `test_readback_drops_submitted_metadata_element`,
   `test_readback_drops_unknown_elements`, `test_readback_is_not_the_submitted_document`, and
   `test_readback_renders_the_modelled_top_level_tags`. Record the failure output.
3. `git restore tests/providers/remote_libvirt/fakes.py`. Re-run the file and expect green — the
   committed double is back.
4. Add a throwaway test to the file that creates a volume whose XML carries run ownership in a
   `<metadata>` child and asserts the ownership is readable back out of `XMLDesc`. Run it against
   the restored double and expect red: that is the pattern that shipped green on #2129's branch,
   and its failure is what proves the discard is modelled. Record the failure output.
5. Delete the throwaway test. Re-run the file and expect green.
6. Record both observed reds in the run report. Nothing is committed by this task, and
   `git status --short` must be clean when it ends.

Acceptance criteria: both faults were observed red, each against the state the step names, both
were reverted, and the file is green with a clean tree afterwards.

## Task 3: add the live_vm storage-double gate

Files:

- Modify `tests/live_vm/__init__.py`.
- Modify `tests/live_vm/test_gates.py`.

Interfaces this task defines, relied on by Task 4:

```python
@dataclass(frozen=True, slots=True)
class StorageDoubleContract:
    libvirt_uri: str

def resolve_storage_double_contract(default_uri: str) -> EnvResolution[StorageDoubleContract]: ...
def require_live_vm_storage_double(default_uri: str = "qemu:///session") -> StorageDoubleContract: ...
```

It consumes, from the same module: `EnvResolution`, `LiveVmEnvState`, `LIBVIRT_URI_ENV`
(`"KDIVE_LIBVIRT_URI"`), `_resolved_uri(default_uri: str) -> str`, and
`_is_local_session_uri(uri: str) -> bool` — all present in `tests/live_vm/__init__.py` today at
lines 30, 52, 118, 126, and 130.

The resolver runs three checks in order, and each failure carries its own reason string:

1. The resolved URI must satisfy `_is_local_session_uri`. A non-session URI is `MISCONFIGURED`
   whether or not it answers: this family's pool target is a client-side `tmp_path`, so the
   comparison is meaningless in system mode.
2. `libvirt.open(uri)` must succeed.
3. `conn.listStoragePools()` on the open connection must succeed — the proof needs the storage
   driver, which modular libvirt packages separately from the qemu driver, so a host that answers
   `open` can still have no storage backend.

A `libvirt.libvirtError` from check 2 or 3 is `ABSENT` when `LIBVIRT_URI_ENV` is unset and
`MISCONFIGURED` when it is set. The connection is closed in a `finally` on every path.

Steps:

1. Add to `tests/live_vm/test_gates.py` the failing tests below, importing the three new names.
   Each monkeypatches `libvirt.open` with `monkeypatch.setattr(libvirt, "open", ...)`.
   - `test_storage_double_absent_when_no_session_daemon`: `KDIVE_LIBVIRT_URI` unset,
     `libvirt.open` raising `libvirt.libvirtError`; assert the resolution state is
     `LiveVmEnvState.ABSENT` and the reason names `qemu:///session`.
   - `test_storage_double_absent_when_the_storage_driver_is_missing`: `KDIVE_LIBVIRT_URI` unset,
     `libvirt.open` returning a double whose `listStoragePools` raises `libvirt.libvirtError`;
     assert `LiveVmEnvState.ABSENT`, that the reason names the storage driver, and that the probe
     connection was closed.
   - `test_storage_double_misconfigured_when_declared_uri_does_not_answer`: `KDIVE_LIBVIRT_URI`
     set to `qemu+unix:///session?socket=/nonexistent/sock`, `libvirt.open` raising; assert
     `LiveVmEnvState.MISCONFIGURED` and that the reason names `KDIVE_LIBVIRT_URI`.
   - `test_storage_double_misconfigured_when_override_moves_off_a_local_session`:
     `KDIVE_LIBVIRT_URI` set to `qemu:///system`; assert `LiveVmEnvState.MISCONFIGURED`, that the
     reason names a local session URI, and that `libvirt.open` was never called.
   - `test_storage_double_available_closes_its_probe`: `libvirt.open` returning a recording
     double; assert the state is `AVAILABLE`, the contract URI is `qemu:///session`,
     `listStoragePools` was called, and the probe connection was closed.
   - `test_storage_double_available_honors_libvirt_uri_override`: set `KDIVE_LIBVIRT_URI` to
     `qemu+unix:///session?socket=/run/user/1000/libvirt/virtqemud-sock`; assert the contract URI
     is that override.
   - `test_storage_double_skips_when_absent`: assert `pytest.skip` fires, matching how
     `test_bzimage_skips_when_absent` is written in this file.
   - `test_storage_double_fails_loud_when_misconfigured`: the same shape against the fail path.
2. Run `uv run python -m pytest tests/live_vm/test_gates.py -q`. Expect an ImportError for
   `require_live_vm_storage_double`.
3. Add `StorageDoubleContract`, `resolve_storage_double_contract`, and
   `require_live_vm_storage_double` to `tests/live_vm/__init__.py`, beside the existing families
   and in the same order the file uses (contracts, then resolvers, then gates). Import `libvirt` at
   module scope. Implement the three checks above. Extend the module docstring's skip-versus-fail
   paragraph to cover the local-session requirement and the storage-driver probe.
4. Run `uv run python -m pytest tests/live_vm/test_gates.py -q`. Expect every test to pass.
5. Run `just lint` and `just type`. Expect both to exit 0.
6. Commit: `test(live-vm): add the storage-double fidelity gate`.

Acceptance criteria:

- With no `KDIVE_LIBVIRT_URI` and no session daemon, the gate skips.
- With `KDIVE_LIBVIRT_URI` set to a URI that does not answer, the gate fails loud.
- With `KDIVE_LIBVIRT_URI` set to a non-session URI, the gate fails loud without opening anything.
- A host whose storage driver does not answer a list call takes the same skip/fail split, not an
  error inside the test body. A host that lists pools but cannot define one is out of the probe's
  reach and still errors in the test body; that limit is stated in the spec, not fixed here.
- The probe connection is closed on the success and both failure paths.

## Task 4: prove the double against real libvirt

Files:

- Create `tests/live_vm/test_libvirt_storage_double_fidelity.py`.

Interfaces consumed: `require_live_vm_storage_double` (Task 3) and `FakeStoragePool` (Task 1),
with the exact signatures given in those tasks.

Steps:

1. Write the module with `pytestmark = [pytest.mark.live_vm]` and one test,
   `test_double_and_libvirt_agree_on_the_dir_pool_volume_readback(tmp_path)`:
   - `contract = require_live_vm_storage_double()`.
   - Bind `conn = None` and `pool = None` **before** the `try`, so a failure in
     `storagePoolDefineXML` cannot make the `finally` raise `UnboundLocalError` over the libvirt
     error that explains it.
   - Inside the `try`: `conn = libvirt.open(contract.libvirt_uri)`; define a `dir` pool named with
     a `uuid4().hex` suffix over `str(tmp_path)`; `pool.create(0)`.
   - Build `base_document` =
     `<volume><name>base.qcow2</name><capacity unit='bytes'>1048576</capacity>`
     `<target><format type='qcow2'/></target></volume>` — no `backingStore`, no root `type`.
   - `real_base = pool.createXML(base_document, 0)`.
   - **Now** build `overlay_document` from `real_base.path()`: the shape `render_volume_xml`
     produces (`<name>`, `<capacity>`, `<target><format type='qcow2'/></target>`,
     `<backingStore><path>real_base.path()</path><format type='qcow2'/></backingStore>`), plus a
     `<metadata><owner>run-1</owner></metadata>` child, a `<bogusElement>zzz</bogusElement>` child,
     and a `kdive='owned'` attribute on `<name>`. No root `type`.
   - `real = pool.createXML(overlay_document, 0).XMLDesc(0)`;
     `real_base_desc = real_base.XMLDesc(0)`.
   - `fake_pool = FakeStoragePool(target_path=str(tmp_path))`;
     `fake_base_desc = fake_pool.createXML(base_document).XMLDesc(0)`;
     `fake = fake_pool.createXML(overlay_document).XMLDesc(0)`.
   - Parse all four. For each pair (base with base, overlay with overlay) assert equal root tags,
     equal root `type` attributes, and equal top-level child tag tuples — which is also what
     distinguishes the two volumes, since only the overlay carries `backingStore`.
   - For `target` on both pairs, and for `backingStore` on the overlay pair, assert equal child tag
     tuples and equal `timestamps` child tag tuples. For `permissions`, make exactly two
     assertions: `real_tags - {"label"} == fake_tags - {"label"}` on the label-stripped sets, and
     `real_tags <= fake_tags` on the **unstripped** sets. `label` carries the file's security
     label, so a runner without SELinux emits three children where an SELinux host emits four; only
     `label` is optional, and the subset leg on the full sets keeps the comparison from letting the
     double render a child libvirt never emits.
   - Assert none of the four parsed readbacks contains an element tagged `metadata` or
     `bogusElement`, or any element carrying a `kdive` attribute key — by walking the trees, **not**
     by substring over the readback strings. Every readback carries the pool target path three
     times, and `tmp_path` honours `TMPDIR` and `--basetemp`, so a substring check for `kdive`
     false-reds on any runner whose temp root contains that token. Do assert by substring that the
     submitted payload values `run-1` and `zzz` appear in none of the four strings: those cannot
     occur in a path libvirt generates.
   - `finally`: skip each of `pool` and `conn` that is still `None`; otherwise delete each volume
     the pool lists, then `pool.destroy()`, `pool.undefine()`, `conn.close()`, each guarded so an
     already-absent object does not mask the real failure.
2. Run `uv run python -m pytest tests/live_vm/test_libvirt_storage_double_fidelity.py -q -m live_vm`.
   On this host a session daemon answers, so expect one passing test; on a host without one expect
   one skip and no error.
3. Run `uv run python -m pytest tests/live_vm/test_family_markers.py -q`. Expect it to pass — the
   new module carries the bare `live_vm` marker and no family sub-marker.
4. Confirm the host is clean: `virsh -c qemu:///session pool-list --all` names no
   `kdive-fidelity-*` pool.
5. Run `just lint` and `just type`. Expect both to exit 0.
6. Commit: `test(live-vm): compare the storage double with a real libvirt readback`.

Acceptance criteria:

- The test passes against the local session daemon, on both the base and the overlay pair.
- The test skips, and does not error, when the gate's probe cannot reach a daemon or a storage
  driver.
- The pool and its volumes are gone from the host after the run.

## Task 5: guardrails

Files: none changed unless a guardrail reports a defect.

Steps:

1. `cd .github/scripts/mermaid-check && npm ci` (known gap #2156, fresh worktree only).
2. `just ci > /tmp/ci-2164.log 2>&1 < /dev/null; echo $?`. Expect `0`.
3. If a recipe reports a defect in a file this plan owns, fix it and re-run the affected recipe
   before re-running `just ci`.

Acceptance criteria: `just ci` exits 0 with no piping and stdin closed.

## Deferrals

None yet. Any deferral a review disposes of as `deferred-tracked` is recorded here with its owning
record path or tracker issue.
