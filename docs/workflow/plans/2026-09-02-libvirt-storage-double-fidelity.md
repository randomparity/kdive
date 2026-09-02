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

Expected implementation size: 450–600 changed lines (M) — derived from the file map below: the two
double classes plus their rendering helper, the unit proof, the gate and its contract, the
live-tier proof, and the gate's unit coverage.

## File map

| File | Action | Answerable for |
|---|---|---|
| `tests/providers/remote_libvirt/fakes.py` | modify | `FakeStorageVolume`, `FakeStoragePool`, and the parse/render helpers |
| `tests/providers/remote_libvirt/test_fakes_storage.py` | create | proving the double drops what it does not model |
| `tests/live_vm/__init__.py` | modify | `StorageDoubleContract`, `resolve_storage_double_contract`, `require_live_vm_storage_double` |
| `tests/live_vm/test_gates.py` | modify | the new gate's skip/fail/available unit coverage |
| `tests/live_vm/test_libvirt_storage_double_fidelity.py` | create | proving the double agrees with real libvirt |

## The modelled set

Reproduced against libvirt 12.0.0 / libvirt-python 12.0.0 on x86_64. A dir-pool volume readback is:

```xml
<volume type='file'>
  <name>verify.raw</name>
  <key>/pool/target/verify.raw</key>
  <capacity unit='bytes'>1048576</capacity>
  <allocation unit='bytes'>1048576</allocation>
  <physical unit='bytes'>1048576</physical>
  <target>
    <path>/pool/target/verify.raw</path>
    <format type='raw'/>
    <permissions>
      <mode>0600</mode>
      <owner>1000</owner>
      <group>1000</group>
      <label>unconfined_u:object_r:user_tmp_t:s0</label>
    </permissions>
    <timestamps>
      <atime>1788387700.839537862</atime>
      <mtime>1788387700.839448004</mtime>
      <ctime>1788387700.839448004</ctime>
      <btime>0</btime>
    </timestamps>
  </target>
</volume>
```

A submitted `<metadata>` child and a submitted `<bogusElement>` child are accepted by
`virStorageVolCreateXML` and appear nowhere in that readback.

## Task 1: model the storage double

Files:

- Modify `tests/providers/remote_libvirt/fakes.py`.
- Create `tests/providers/remote_libvirt/test_fakes_storage.py`.

Interfaces this task defines, relied on by Tasks 3 and 4:

```python
@dataclass(frozen=True, slots=True)
class VolumeState:
    name: str
    volume_type: str
    capacity: int
    capacity_unit: str
    format_type: str
    path: str

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
   - `test_readback_renders_the_modelled_top_level_tags`: parse the readback; assert the child tag
     tuple equals `("name", "key", "capacity", "allocation", "physical", "target")`.
   - `test_readback_renders_the_modelled_target_tags`: assert the `target` child tag tuple equals
     `("path", "format", "permissions", "timestamps")`, that `permissions` children are
     `("mode", "owner", "group", "label")`, and that `timestamps` children are
     `("atime", "mtime", "ctime", "btime")`.
   - `test_readback_retains_submitted_fields`: assert root `type`, `name` text, `capacity` text and
     its `unit` attribute, and `target/format@type` equal what was submitted.
   - `test_readback_derives_key_and_path_from_the_pool`: build the pool with
     `target_path="/pool/target"`, create `disk.qcow2`, assert `key` text, `target/path` text,
     `volume.key()`, and `volume.path()` all equal `/pool/target/disk.qcow2`.
   - `test_readback_derives_allocation_and_physical_from_capacity`: assert both equal the submitted
     capacity and both carry the submitted unit.
   - `test_readback_applies_defaults_for_absent_input`: submit only `<volume><name>x</name></volume>`;
     assert root `type` is `file`, `target/format@type` is `raw`, `capacity` text is `0` with unit
     `bytes`.
   - `test_readback_drops_attributes_libvirt_does_not_keep`: submit `<name kdive='owned'>x</name>`
     and a root `kdive='owned'` attribute; assert neither appears in the readback.
   - `test_info_reports_capacity_and_allocation`: assert `volume.info()[1]` and `[2]` equal the
     submitted capacity.
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
   above. `createXML` parses with `xml.etree.ElementTree.fromstring`, reads only `./name`,
   `./capacity` and its `unit`, the root `type` attribute, and `./target/format` and its `type`
   attribute, and builds a `VolumeState`; the volume's `path` is `f"{pool_target_path}/{name}"`.
   `XMLDesc` renders the modelled document from that state alone, with `mode` `0600`, `owner` and
   `group` `0`, `label` the empty string, and every timestamp `0`. Add `# noqa: N802` on
   `XMLDesc`, `createXML`, `storageVolLookupByName`, and `listVolumes`, matching the file's
   existing libvirt-binding methods. Update the module docstring to say the storage double models
   the readback rather than echoing the request, and cite #2164.
4. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`.
   Expect every test to pass.
5. Run `just lint` and `just type`. Expect both to exit 0.
6. Commit: `test(remote-libvirt): model the libvirt storage readback in the shared double`.

Acceptance criteria:

- `XMLDesc` output contains no substring taken from an unmodelled part of the submitted document.
- The readback's top-level and `target` child tag tuples equal the modelled set above.
- `fakes.py` retains no reference to the submitted document except `created_xml`.

## Task 2: prove the double fails when it echoes

Files: none changed. This is a controlled fault against Task 1's committed state.

Steps:

1. Edit `FakeStorageVolume.XMLDesc` in the working tree to `return self._submitted` after
   temporarily retaining the submitted document on the state.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`. Expect
   red on at least `test_readback_drops_submitted_metadata_element`,
   `test_readback_drops_unknown_elements`, `test_readback_is_not_the_submitted_document`, and
   `test_readback_renders_the_modelled_top_level_tags`.
3. Write a throwaway test in the same file that creates a volume whose XML carries run ownership in
   a `<metadata>` child and asserts the ownership is readable from `XMLDesc`. Run it against the
   committed double and expect red — that is the pattern that shipped green on #2129's branch.
4. `git restore tests/providers/remote_libvirt/fakes.py` and delete the throwaway test. Re-run the
   file and expect green.
5. Record the observed red output in the run report. Nothing is committed by this task.

Acceptance criteria: both faults were observed red and both were reverted, with the file green
afterwards.

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
(`"KDIVE_LIBVIRT_URI"`), and `_resolved_uri(default_uri: str) -> str`.

Steps:

1. Add to `tests/live_vm/test_gates.py` the failing tests below, importing the three new names.
   Each monkeypatches `libvirt.open` on the `tests.live_vm` module namespace.
   - `test_storage_double_absent_when_no_session_daemon`: `KDIVE_LIBVIRT_URI` unset,
     `libvirt.open` raising `libvirt.libvirtError`; assert the resolution state is
     `LiveVmEnvState.ABSENT` and the reason names `qemu:///session`.
   - `test_storage_double_misconfigured_when_declared_uri_does_not_answer`: `KDIVE_LIBVIRT_URI`
     set to `qemu:///system`, `libvirt.open` raising; assert `LiveVmEnvState.MISCONFIGURED` and
     that the reason names `KDIVE_LIBVIRT_URI`.
   - `test_storage_double_available_closes_its_probe`: `libvirt.open` returning a recording
     double; assert the state is `AVAILABLE`, the contract URI is `qemu:///session`, and the probe
     connection was closed.
   - `test_storage_double_available_honors_libvirt_uri_override`: assert the contract URI is the
     override.
   - `test_storage_double_skips_when_absent`: assert `pytest.raises(Exception)` under
     `pytest.skip`'s own outcome, matching how `test_bzimage_skips_when_absent` is written in this
     file.
   - `test_storage_double_fails_loud_when_misconfigured`: the same shape against the fail path.
2. Run `uv run python -m pytest tests/live_vm/test_gates.py -q`. Expect an ImportError for
   `require_live_vm_storage_double`.
3. Add `StorageDoubleContract`, `resolve_storage_double_contract`, and
   `require_live_vm_storage_double` to `tests/live_vm/__init__.py`, beside the existing families
   and in the same order the file uses (contracts, then resolvers, then gates). Import `libvirt` at
   module scope. The resolver opens `_resolved_uri(default_uri)`, closes the connection in a
   `finally`, and returns `AVAILABLE`; on `libvirt.libvirtError` it returns `ABSENT` when
   `LIBVIRT_URI_ENV` is unset and `MISCONFIGURED` when it is set. Extend the module docstring's
   skip-versus-fail paragraph to cover the probe.
4. Run `uv run python -m pytest tests/live_vm/test_gates.py -q`. Expect every test to pass.
5. Run `just lint` and `just type`. Expect both to exit 0.
6. Commit: `test(live-vm): add the storage-double fidelity gate`.

Acceptance criteria:

- On this host with no `KDIVE_LIBVIRT_URI` and no session daemon, the gate skips.
- With `KDIVE_LIBVIRT_URI` set to a URI that does not answer, the gate fails loud.
- The probe connection is closed on both the success and failure paths.

## Task 4: prove the double against real libvirt

Files:

- Create `tests/live_vm/test_libvirt_storage_double_fidelity.py`.

Interfaces consumed: `require_live_vm_storage_double` (Task 3) and `FakeStoragePool` (Task 1),
with the exact signatures given in those tasks.

Steps:

1. Write the module with `pytestmark = [pytest.mark.live_vm]` and one test,
   `test_double_and_libvirt_agree_on_the_dir_pool_volume_readback(tmp_path)`:
   - `contract = require_live_vm_storage_double()`; `conn = libvirt.open(contract.libvirt_uri)`.
   - Define a `dir` pool named with a `uuid4().hex` suffix over `str(tmp_path)`, `pool.create(0)`.
   - Build one volume document string carrying `<metadata>` and `<bogusElement>` children, a
     `<capacity unit='bytes'>1048576</capacity>`, and `<target><format type='raw'/></target>`.
   - `real = pool.createXML(document, 0).XMLDesc(0)`, where `pool` is the started dir pool.
   - `fake = FakeStoragePool(target_path=str(tmp_path)).createXML(document).XMLDesc(0)`.
   - Parse both. Assert equal root tags, equal root `type` attributes, equal top-level child tag
     tuples, and equal `target` child tag tuples.
   - Assert `metadata` and `bogusElement` appear in neither readback, by tag search over every
     element and by substring over both strings.
   - `finally`: delete each volume the pool lists, `pool.destroy()`, `pool.undefine()`,
     `conn.close()`, each guarded so an already-absent object does not mask the real failure.
2. Run `uv run python -m pytest tests/live_vm/test_libvirt_storage_double_fidelity.py -q -m live_vm`.
   On this host a session daemon answers, so expect one passing test; on a host without one expect
   one skip and no error.
3. Run `uv run python -m pytest tests/live_vm/test_family_markers.py -q`. Expect it to pass — the
   new module carries the bare `live_vm` marker and no family sub-marker.
4. Run `just lint` and `just type`. Expect both to exit 0.
5. Commit: `test(live-vm): compare the storage double with a real libvirt readback`.

Acceptance criteria:

- The test passes against the local session daemon.
- The test skips, and does not error, when `libvirt.open` cannot reach a daemon.
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
