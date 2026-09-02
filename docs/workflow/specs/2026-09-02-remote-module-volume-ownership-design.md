# Remote module volume ownership — design

Issue: [#2157](https://github.com/randomparity/kdive/issues/2157). Decision:
[ADR-0588](../../adr/0588-remote-module-volume-ownership-lives-in-the-volume-name.md), amending
[ADR-0585](../../adr/0585-remote-offline-module-restoration-appliance.md). Implemented by
[#2129](https://github.com/randomparity/kdive/issues/2129); the sibling boot-artifact occurrence
is [#2158](https://github.com/randomparity/kdive/issues/2158).

## Goal

Give remote module volumes a durable ownership fact that libvirt actually persists, so a reaper
can decide — with the creating worker gone — whether a volume is KDIVE's and whether its attempt
is over. ADR-0588 settles the channel as the volume name. This spec is what #2129 implements
against; it is meant to require no further design decision.

## What is wrong today

`remote_module_volumes.py` writes ownership into a `<metadata>` child of the volume XML
(`_render_volume`) and requires exactly one owner element on readback (`_metadata`). Libvirt
accepts the element and discards it, so the readback always raises and every module attempt fails
closed. The suite does not catch it because the `Pool.createXML` double in
`tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py` stores the
submitted XML and returns it verbatim from `XMLDesc`.

## Design

### The name is the channel

The provider already renders the whole owner tuple into the volume name. That shape is kept and
given a parser; nothing about the naming changes, only what the code trusts:

```text
kdive-module-<system-uuid>-<run-uuid>-<operation-nonce>-<purpose>.ext4
```

- `system-uuid`, `run-uuid` — canonical lowercase UUIDs, 36 characters each.
- `operation-nonce` — 32 lowercase hex characters, the form `RemoteModuleOperation` already
  validates in `remote_module_operation.py`.
- `purpose` — `source` or `scratch`.

Longest rendered name: 132 bytes. Measured `NAME_MAX` for a dir-pool volume: 255 bytes, with 256
refused. The 123-byte margin is asserted by a test rather than left as a comment.

### Normative requirements

- **N1 — one renderer.** Every module volume name is produced by
  `render_module_volume_name(system_id, run_id, operation_nonce, purpose)`. No other f-string in
  the provider builds one.
- **N2 — one anchored parser.** Recognition is `parse_module_volume_name(name)`, a single
  `re.fullmatch` over the whole name returning `ModuleVolumeOwner | None`. No prefix match, no
  `startswith`, no substring test, no partial credit.
- **N3 — an unparseable name is foreign.** A volume whose name does not parse is never read
  further, never counted as KDIVE's, and never deleted. This is the entire safety predicate for
  deletion, so it fails closed by construction: the default for an unrecognised name is "leave it
  alone".
- **N4 — no ownership readback from volume XML.** `_metadata`, `_identity`, `_METADATA_NS`, and
  the `<metadata>` subtree in `_render_volume` are deleted, not disabled. The readback keeps only
  the checks against fields libvirt does persist: `target/format` is `raw`, and capacity from
  `volume.info()[1]`.
- **N5 — intent precedes the volume.** The durable attempt row naming
  `(system_id, run_id, operation_nonce, purpose)` is written before either volume is created.
  A volume may never exist whose attempt has no row. A row with no volume is the ordinary crash
  residue and is benign.
- **N6 — liveness comes from the durable store.** The sweep takes the live-owner set as a
  parameter, in the shape `reap_orphaned_boot_artifacts` already takes `live_owners`. The provider
  does not query Postgres itself.
- **N7 — attachment guard before deletion.** A volume whose path is referenced by any active or
  inactive domain definition is never deleted; it is reported as a conflict. The existing
  `protected_volume_paths(conn, pool_name, names)` performs the resolution.
- **N8 — deletion is idempotent.** `VIR_ERR_NO_STORAGE_VOL` on delete is an achieved post-state,
  counted as removed, never an error.
- **N9 — doubles discard what libvirt discards.** Any test double standing in for a libvirt
  storage pool renders `XMLDesc` from a modelled field set and drops everything else it was
  handed. See *Test doubles* below.

### Ownership recovery when the writing worker is gone

The sweep runs in the reconciler (ADR-0021). Per volume, after `pool.refresh(0)`:

1. `parse_module_volume_name(volume.name())`. `None` ends this volume's evaluation (N3).
2. The parse result *is* the recovery. `(system_id, run_id, operation_nonce, purpose)` comes off
   the name, so nothing is reconstructed from a dead worker's memory, its console output, or the
   volume's bytes.
3. Owner in the live set → retained, no further action.
4. Owner absent from the live set → the attempt is over. Apply N7, then delete under N8.

The case the mechanism exists for is a worker that died between creating a volume and finishing
its attempt. N5 is what makes step 4 sound: because the row is written first, "absent from the
live set" means the attempt ended, never that it has not started yet. Without N5 the sweep has a
race against a live worker in the window between `createXML` and the durable write — a race the
discarded metadata channel had identically and never addressed.

### Threat model

The change parses names it did not produce, off a host KDIVE does not exclusively own, and uses
the parse to authorise deletion. That makes it security-relevant.

**Boundaries.** One boundary is widened and none is added. The widened one is the remote libvirt
storage pool: volume names enter from a host where an operator, another tool, or another KDIVE
deployment may also create volumes, and the value that crosses is a name that decides a deletion.

**Actors.** The remote libvirt host operator is trusted for host administration and untrusted as
a source of well-formed names — anything in that pool may have been created by something other
than this deployment. Volume names are attacker-influenced only to the extent that someone with
pool write access can choose them; someone with that access can already delete volumes directly,
so the design does not treat name forgery as an escalation. ADR-0585 already places privileged
operator interference outside the trust boundary. There is no anonymous or tenant-facing path
into this code.

**Controls.**

| Boundary | Control |
|---|---|
| Volume name → ownership claim | N2 anchored full-match against a fixed grammar; UUID and 32-hex-nonce shapes are structural, so a name cannot smuggle a path, a separator, or a length overrun through the parse. |
| Ownership claim → deletion | N6 liveness join against the durable store, then N7 attachment guard. Deletion needs all three to agree. |
| Unrecognised input | N3: the default is no action. An unparseable, over-long, or wrong-shaped name is left untouched. |
| Failure disclosure | Errors carry `ErrorCategory` plus pool and volume name only, matching `reaping/boot_artifacts.py`. No host paths, no XML, no volume content. |

**Out of scope.** A privileged operator on the remote host racing or forging volumes — ADR-0585
places that outside the boundary. Multi-tenancy inside one pool: KDIVE assumes the configured
module pool is its own, and a second KDIVE deployment sharing one pool with overlapping System and
Run UUIDs is not a supported deployment. Encryption of volume contents, which is unchanged.

### Test doubles

The double is now load-bearing, so it is specified rather than improvised, and it lives once in
`tests/providers/remote_libvirt/fakes.py` so the module path and #2158's boot-artifact path share
it.

`FakeStoragePool.createXML(xml, flags)` parses the submitted XML and retains **only**:

- `name`
- `key` (synthesised as `<pool-path>/<name>`, as libvirt does for a dir pool)
- `capacity`, and `allocation` and `physical` derived from it
- `target/path`, `target/format`

`FakeStorageVolume.XMLDesc(flags)` re-renders from that retained state. Every other submitted
element — `<metadata>` above all — is dropped. The double therefore models libvirt's projection
rather than echoing the caller.

**Fidelity test.** `tests/live_vm/test_libvirt_storage_double_fidelity.py`, marked `live_vm`,
creates a real dir-pool volume through a real libvirt connection with a `<metadata>` child and one
other unmodelled element present, then asserts the real `XMLDesc` and the double's `XMLDesc` agree
on the retained element set and that both omit the unmodelled elements. This is the only arm that
can catch the double drifting from libvirt, and it is the arm that would have caught the original
defect. It needs libvirt and a writable directory — a strict subset of the `live_vm` environment
contract, no KVM guest — so it skips cleanly wherever the tier does.

**How a wrong implementation is caught.** Each row names the failure and the single arm that goes
red for it:

| Wrong implementation | Arm that fails |
|---|---|
| Ownership written anywhere libvirt does not persist | The double drops it, so the reap unit tests fail on an unrecoverable owner instead of passing on an echo. |
| The double drifts from libvirt's real projection | `test_libvirt_storage_double_fidelity.py` on a host with libvirt. |
| Grammar overruns `NAME_MAX` | The name-budget test asserting the longest renderable name is 132 bytes and under 255. |
| Grammar admits a foreign shape | The negative table: a name one character off in each field, a prefix-only name, and a name with a path separator all parse to `None`. |
| Renderer and parser disagree | The round-trip property test over generated tuples. |
| The sweep deletes an attached volume | The attachment-guard test: a volume in no live-owner set whose path a domain references is retained and reported. |
| The sweep deletes a live worker's volume | The N5 ordering test: an owner present in the live set is retained. |

x86_64 work and verification complete first; the native ppc64le proof is deferred to a separate
later run on native POWER hardware.

## Implementation plan

Owned by #2129. It applies to the branch `feat/crash-resumable-remote-modules-2129`, not to this
one.

### Global constraints

- Python 3.14, `uv`-managed. Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs
  whole-tree with strict defaults.
- Module docstrings cite the ADRs they implement; cite ADR-0588 and ADR-0585.
- `adr-status-check` rejects a **Proposed** ADR cited from `src/` or `tests/`. ADR-0588 is
  Proposed until this record's own PR merges, so the PR that adds these citations is the one that
  flips ADR-0588 to **Accepted**; intermediate PRs cite `#2129` instead.
- Doc style: **Milestone**, never "Sprint"; no "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test`, and `just ci` before push. Run them bare.
- No new dependency. The grammar is `re` and `uuid` from the stdlib.

Expected implementation size: 600–900 changed lines (M) — derived from the four new source and
test files, the three modified files in the map below, and Task 6's ordering test, counting the
tests each task adds.

### File map

| Path | Answerable for |
|---|---|
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volume_names.py` | **new.** Render and parse the grammar. Nothing else. |
| `src/kdive/providers/remote_libvirt/reaping/module_volumes.py` | **new.** The reconciler sweep. |
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py` | **modified.** Drops the metadata channel; calls the renderer. |
| `tests/providers/remote_libvirt/fakes.py` | **modified.** Gains the projection-modelling storage double. |
| `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py` | **new.** Grammar tests. |
| `tests/providers/remote_libvirt/reaping/test_module_volumes.py` | **new.** Sweep tests. |
| `tests/live_vm/test_libvirt_storage_double_fidelity.py` | **new.** Double-versus-libvirt fidelity. |
| `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py` | **modified.** Uses the shared double; metadata assertions go. |
| `tests/providers/remote_libvirt/test_fakes_storage.py` | **new.** Proves the double drops what it does not model. |
| #2129's recovery-point attempt path | **modified.** Task 6's intent-before-volume ordering; not on `main`, so named by contract. |

### Task 1 — the grammar

Creates `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volume_names.py`,
tests `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py`.

**Interfaces produced** — every later task consumes these exact signatures:

```python
MODULE_VOLUME_NAME_MAX_BYTES = 255
MODULE_VOLUME_PURPOSES = ("source", "scratch")

@dataclass(frozen=True, slots=True)
class ModuleVolumeOwner:
    system_id: str
    run_id: str
    operation_nonce: str
    purpose: str

def render_module_volume_name(
    system_id: str, run_id: str, operation_nonce: str, purpose: str
) -> str: ...

def parse_module_volume_name(name: str) -> ModuleVolumeOwner | None: ...
```

`render_module_volume_name` raises `ValueError` on a malformed input rather than emitting a name
that will not parse. `parse_module_volume_name` returns `None` for anything that does not
full-match; it never raises on caller input.

**Interfaces consumed** — none.

Steps:

1. Write `test_round_trip_recovers_the_owner`: for a fixed System UUID, Run UUID, 32-hex nonce,
   and each purpose, `parse_module_volume_name(render_module_volume_name(...))` equals the
   `ModuleVolumeOwner` those arguments describe.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py -q`.
   Expect collection to fail with `ModuleNotFoundError` for `remote_module_volume_names`.
3. Write the module: a module-level `_NAME = re.compile(...)` anchored with `fullmatch`, using
   `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` for each UUID, `[0-9a-f]{32}`
   for the nonce, and `(source|scratch)` for the purpose, followed by `\.ext4`. Render with an
   f-string in the same field order. Validate render inputs against the same components.
4. Run the same command. Expect 1 passed.
5. Write `test_longest_name_fits_the_budget`: the longest renderable name (both UUIDs all-`f`,
   nonce all-`f`, purpose `scratch`) is 132 bytes when UTF-8 encoded and is
   `< MODULE_VOLUME_NAME_MAX_BYTES`.
6. Write `test_foreign_names_do_not_parse`, parametrised over: `""`; `"kdive-module-"`; the valid
   name with `.ext4` removed; with `source` replaced by `backup`; with one UUID hex digit
   uppercased; with a 31-character nonce; with a 33-character nonce; the valid name prefixed by
   `x`; the valid name suffixed by `x`; the valid name with `/` substituted for the final `-`;
   `"../" + valid`; and a 300-character name. Each must return `None`.
7. Write `test_render_rejects_malformed_input`, parametrised over a non-UUID system id, an
   uppercase nonce, and an unknown purpose. Each must raise `ValueError`.
8. Run the same command. Expect all passed.
9. Commit.

Acceptance: the module is the only place in `src/` that builds or recognises a module volume
name, and `rg -n 'kdive-module-' src/` returns hits only in this file.

### Task 2 — the storage double

Modifies `tests/providers/remote_libvirt/fakes.py`.

**Interfaces produced:**

```python
class FakeStorageVolume:
    def name(self) -> str: ...
    def key(self) -> str: ...
    def path(self) -> str: ...
    def info(self) -> list[int]: ...          # [type, capacity, allocation]
    def XMLDesc(self, flags: int = 0) -> str: ...
    def delete(self, flags: int = 0) -> int: ...
    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...

class FakeStoragePool:
    def __init__(self, path: str = "/var/lib/libvirt/images") -> None: ...
    def refresh(self, flags: int = 0) -> int: ...
    def createXML(self, xml: str, flags: int = 0) -> FakeStorageVolume: ...
    def storageVolLookupByName(self, name: str) -> FakeStorageVolume: ...
    def listAllVolumes(self, flags: int = 0) -> list[FakeStorageVolume]: ...
```

**Interfaces consumed** — none.

Steps:

1. Write `tests/providers/remote_libvirt/test_fakes_storage.py::test_created_volume_drops_unmodelled_elements`:
   `createXML` a volume whose XML carries a `<metadata>` child and a `<backingStore>` child; assert
   the returned volume's `XMLDesc(0)` contains `<name>`, `<key>`, `<capacity`, `<allocation`,
   `<physical`, and `<target>` and contains neither `metadata` nor `backingStore`.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`. Expect
   an `ImportError` for `FakeStoragePool`.
3. Implement both classes in `fakes.py`. `createXML` parses with
   `xml.etree.ElementTree.fromstring`, reads `name`, `capacity`, `target/path`, and
   `target/format/@type`, synthesises `key` and `path` as `f"{pool_path}/{name}"` when the XML
   gives no `target/path`, sets `allocation` and `physical` equal to `capacity`, and stores only
   those. `XMLDesc` renders them from a fixed template. Nothing retains the submitted string.
4. Run the same command. Expect 1 passed.
5. Add `test_xmldesc_is_not_the_submitted_string`: assert the submitted XML string is not a
   substring of, and not equal to, the readback.
6. Run the same command. Expect 2 passed.
7. Commit.

Acceptance: `rg -n 'self.xml' tests/providers/remote_libvirt/fakes.py` returns nothing.

### Task 3 — drop the metadata channel from the provider

Modifies `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py` and
`tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py`.

**Interfaces consumed:** `render_module_volume_name`, `parse_module_volume_name`,
`ModuleVolumeOwner` from Task 1; `FakeStoragePool`, `FakeStorageVolume` from Task 2.

**Interfaces produced:** `_names(request: VolumeRequest) -> tuple[str, str]` keeps its signature
and returns `(source_name, scratch_name)` rendered by Task 1's renderer.

Steps:

1. In the test module, replace the file-local `Volume` and `Pool` classes with imports of
   `FakeStorageVolume` and `FakeStoragePool`. Delete every assertion reading owner attributes out
   of volume XML and every `fail_metadata_read` path.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py -q`.
   Expect failures inside `_metadata` — the double now returns XML with no owner element, which is
   the production behaviour. This is the red that proves the defect.
3. Delete `_METADATA_NS`, `_identity`, and `_metadata` from the provider module. Remove the
   `<metadata>` subtree from `_render_volume` and drop its now-unused `request`, `purpose`,
   `digest`, and `identity` parameters, leaving `_render_volume(name: str, capacity: int) -> str`.
4. Rewrite `_names` to call `render_module_volume_name(request.system_id, request.run_id,
   request.operation_nonce, purpose)` for each purpose.
5. In the readback verification loop in `prepare_module_volumes`, replace the
   `_identity`/`_metadata` block — `expected_identity`, `attributes`, `expected_attributes`, and
   the `attributes != expected_attributes` conflict — with
   `parse_module_volume_name(volume.name())` compared against the `ModuleVolumeOwner` the request
   describes. A `None` or mismatched parse raises the existing
   `_conflict("remote module volume ownership mismatched", volume=name)`. Keep the capacity check
   against `volume.info()[1]` and the `target/format` is-raw check, moving both into a small
   `_readback_facts(volume, name) -> int` helper that reads only fields libvirt persists.
6. Drop `PreparedVolume.identity`, whose only producer was `_identity`, and update the three
   construction sites plus `PreparedModuleVolumes` consumers. The digest binding it stood for is
   already carried by `_inspect_remote_source`, which downloads the source image and checks it
   against the manifest; the scratch volume's digest was the placeholder `"sha256:" + "0" * 64`
   and verified nothing.
7. Run the same command. Expect all passed.
8. Run `just lint` and `just type`. Expect both clean; fix any unused import the deletions left.
9. Commit.

Acceptance: `rg -n 'metadata' src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py`
returns nothing, and the module's docstring cites ADR-0588.

### Task 4 — the reconciler sweep

Creates `src/kdive/providers/remote_libvirt/reaping/module_volumes.py`, tests
`tests/providers/remote_libvirt/reaping/test_module_volumes.py`.

**Interfaces consumed:** `parse_module_volume_name`, `ModuleVolumeOwner` from Task 1;
`FakeStoragePool`, `FakeStorageVolume` from Task 2; `protected_volume_paths(conn, pool_name,
names)` and `_lookup_pool` patterns from `remote_module_volumes.py`; `CategorizedError` and
`ErrorCategory` from `kdive.domain.errors`.

**Interfaces produced:**

```python
class ModuleVolumeReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...

def list_owned_module_volumes(
    conn: ModuleVolumeReaperConn, pool_name: str
) -> list[tuple[str, ModuleVolumeOwner]]: ...

def reap_orphaned_module_volumes(
    conn: ModuleVolumeReaperConn,
    pool_name: str,
    *,
    live_owners: Collection[ModuleVolumeOwner],
    protected_paths: frozenset[str],
) -> int: ...
```

`protected_paths` is passed in rather than computed inside, so the sweep stays a pure decision
over inputs the caller has already resolved through `protected_volume_paths`.

Steps:

1. Write `test_foreign_volumes_are_never_touched`: a pool holding one valid module volume absent
   from `live_owners` and three foreign names (an operator base image, a boot-artifact name, and a
   near-miss module name with a 31-character nonce). Assert the return is 1 and that only the
   valid volume's `delete` was called.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_module_volumes.py -q`.
   Expect `ModuleNotFoundError`.
3. Implement the module following `reaping/boot_artifacts.py`'s structure: look up the pool,
   `refresh(0)`, iterate `listAllVolumes(0)`, parse each name, skip `None`, skip owners in
   `live_owners`, skip volumes whose `path()` is in `protected_paths`, and delete the rest.
   Wrap libvirt errors in `CategorizedError` with `ErrorCategory.INFRASTRUCTURE_FAILURE` and
   details limited to `pool` and `volume`.
4. Run the same command. Expect 1 passed.
5. Add `test_live_owner_is_retained`, `test_attached_volume_is_retained` (the orphan's path is in
   `protected_paths`), `test_already_deleted_volume_counts_as_removed` (delete raises
   `VIR_ERR_NO_STORAGE_VOL`), and `test_list_owned_returns_name_and_owner`.
6. Run the same command. Expect 5 passed.
7. Run `just lint` and `just type`. Expect clean.
8. Commit.

Acceptance: no path through the module can delete a volume whose name did not full-match, and the
sweep never opens a volume's contents.

### Task 5 — the fidelity proof

Creates `tests/live_vm/test_libvirt_storage_double_fidelity.py`.

**Interfaces consumed:** `FakeStoragePool` from Task 2.

Steps:

1. Write the test: mark it `live_vm`, connect with `libvirt.open("qemu:///session")`, define and
   start a `dir` pool over a `tmp_path` target, `createXML` a volume whose XML carries a
   `<metadata>` child and a `<backingStore>` child, and read it back.
2. Assert the real readback's top-level element tags equal the double's readback's top-level
   element tags, and that neither contains `metadata` or `backingStore`.
3. Tear down: delete the volume, destroy and undefine the pool, in a `finally`.
4. Run `just test-live` on a host with libvirt. Expect 1 passed; expect a clean skip where the
   tier's environment contract is unmet.
5. Commit.

Acceptance: the test fails if `FakeStoragePool` is changed to echo its input, and skips rather
than errors on a host without libvirt.

### Task 6 — intent before volume

Modifies whichever module of #2129's recovery-point work performs the durable attempt write. That
module does not exist on `main`, so this task names the contract and its test rather than a file
path the plan cannot ground.

**Interfaces consumed:** `render_module_volume_name` from Task 1.

Steps:

1. Establish that the durable attempt row naming `(system_id, run_id, operation_nonce, purpose)`
   is written before the first `createXML` for that attempt, and that the write is what the
   sweep's `live_owners` set is derived from.
2. Write a test that drives the attempt path with a durable-store double recording call order,
   and asserts the row write precedes the first volume creation. A double that creates the volume
   first must make the test red.
3. Write a test for the crash window the ordering closes: an attempt whose row exists and whose
   volumes do not is reconciled without error, and no orphan is produced.
4. Run the module's test file bare. Expect both passed.
5. Commit.

Acceptance: the sweep's docstring in Task 4 names N5 as the precondition it relies on, and this
task's tests are what hold that precondition. Reversing the order in the attempt path turns them
red.

### Verification for the whole change

Run bare, in this order: `just lint`, `just type`, `just test`, then `just ci`. Then `just
test-live` on the self-hosted native-KVM runner for Task 5's arm. `just ci` alone is not
sufficient evidence for this change — the defect it is fixing passed `just ci` in full — so the
`test-live` arm is what closes it on x86_64.

## Deferrals and follow-ups

- #2158 — migrate the remote boot-artifact path onto this channel. Until it lands, the two remote
  reap paths use different ownership channels.
- Wiring `reap_orphaned_module_volumes` into the reconciler's remote sweep, and the durable
  live-owner query behind N6, belong to #2129's recovery-point work; this spec fixes their
  contracts, not their call sites. Task 6 carries N5's obligation into that work.
