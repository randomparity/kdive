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
(`_render_volume`) and requires exactly one owner element on readback (`_metadata`, three call
sites). Libvirt accepts the element and discards it, so the readback always raises and every
module attempt fails closed. The suite does not catch it because the `Pool.createXML` double in
`tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py` stores the
submitted XML and returns it verbatim from `XMLDesc`.

`remote_module_operation.py` carries the same defect twice more, and it is the file the original
issue did not name:

- it reads the same `urn:kdive:remote-module-volume:v1` owner element in `_read_file` and in
  `inventory()`, so the durable reads fail closed even once `remote_module_volumes.py` is fixed;
- it implements a **second** channel, `_REAP_METADATA_NS = "urn:kdive:remote-module-reap:v1"`, for
  the reap-marker journal volumes `_reap_marker_name` renders as
  `kdive-module-<system>-<run>-<nonce>-{reaping,reaped}.journal`. Their whole payload is a
  nineteen-attribute `attempt-reap` element inside the discarded `<metadata>`, read back in three
  places, so a journal volume in production carries nothing but its name.
- `inventory()` recognises volumes with `name.startswith("kdive-module-")`, a prefix match this
  design replaces with an anchored parse.

## Design

### The name is the channel

The provider already renders the whole owner tuple into the volume name. That shape is kept and
given a parser; nothing about the naming changes, only what the code trusts:

```text
kdive-module-<system-uuid>-<run-uuid>-<operation-nonce>-<kind>
```

- `system-uuid`, `run-uuid` — canonical lowercase UUIDs, 36 characters each.
- `operation-nonce` — 32 lowercase hex characters, the form `RemoteModuleOperation` already
  validates in `remote_module_operation.py`.
- `kind` — exactly one of `source.ext4`, `scratch.ext4`, `reaping.journal`, `reaped.journal`.

All four are shapes the provider already renders. The journal kinds are in the grammar because a
kind it omits is a volume the sweep classifies as foreign and never reclaims: every completed
attempt would leak one or two journal volumes into the pool permanently.

Longest rendered name: 135 bytes (`13 + 36 + 1 + 36 + 1 + 32 + 1 + 15`, the last term being
`reaping.journal`). Measured `NAME_MAX` for a dir-pool volume: 255 bytes, with 256 refused. The
120-byte margin is asserted by a test rather than left as a comment.

### Normative requirements

- **N1 — one renderer.** Every module volume name is produced by
  `render_module_volume_name(system_id, run_id, operation_nonce, kind)`. No other f-string in
  the provider builds one — `_names` and `_reap_marker_name` both call it.
- **N2 — one anchored parser.** Recognition is `parse_module_volume_name(name)`, a single
  `re.fullmatch` over the whole name returning `ModuleVolumeOwner | None`. No prefix match, no
  `startswith`, no substring test, no partial credit. The `startswith("kdive-module-")` in
  `inventory()` is replaced by this call.
- **N3 — an unparseable name is foreign.** A volume whose name does not parse is never read
  further, never counted as KDIVE's, and never deleted. This is the entire safety predicate for
  deletion, so it fails closed by construction: the default for an unrecognised name is "leave it
  alone".
- **N4 — no ownership readback from volume XML.** Both namespaces go: `_metadata`, `_identity`,
  `_METADATA_NS` and the `<metadata>` subtree in `_render_volume` in `remote_module_volumes.py`,
  and `_REAP_METADATA_NS` with its `attempt-reap` reads and writes plus the two
  `attempt-volume` reads in `remote_module_operation.py`. Deleted, not disabled. The readbacks
  keep only checks against fields libvirt does persist: `target/format` is `raw`, and capacity
  from `volume.info()[1]`.
- **N5 — intent precedes the volume.** The durable attempt row naming
  `(system_id, run_id, operation_nonce, kind)` is written before any of the attempt's volumes are
  created. A volume may never exist whose attempt has no row. A row with no volume is the ordinary
  crash residue and is benign.
- **N6 — retention comes from the durable store, keyed on obligation.** The sweep takes the
  retained-owner set as a parameter, in the shape `reap_orphaned_boot_artifacts` already takes
  `live_owners`; the provider does not query Postgres itself. That set is the set of attempts with
  an **un-discharged durable obligation**, not the set of running attempts. ADR-0585 retains a
  scratch volume "until recovery or successful baseline commitment makes it unnecessary" and
  retains it "for diagnosis" on a failed restoration, so an obligation is discharged only at
  ADR-0585's durable `restored` or at baseline commitment. Keying on the running attempt deletes
  the recovery point on the ordinary path.
- **N7 — attachment guard before deletion.** A volume whose backing path is referenced by any
  active or inactive domain definition on the host is never deleted; it is reported as a conflict.
  No existing function performs this at whole-pool scope: `protected_volume_paths(conn, pool_name,
  names)` resolves *caller-supplied* names to paths and enumerates no domains, and
  `inspect_module_attachments` / `_inspect_definition` enumerate domains but need an
  `ExpectedAttachmentState` naming one attempt's volumes in advance. The sweep discovers its
  candidates from the pool and has no expected state, so the whole-pool referenced-path builder is
  new code this design owes.
- **N8 — deletion is idempotent.** `VIR_ERR_NO_STORAGE_VOL` on delete is an achieved post-state,
  counted as removed, never an error.
- **N9 — doubles discard what libvirt discards.** Any test double standing in for a libvirt
  storage pool renders `XMLDesc` from a modelled field set and drops everything else it was
  handed. See *Test doubles* below.
- **N10 — enumerate, then read the retained set.** The sweep enumerates and parses the pool
  *before* it reads the retained-owner set, and resolves the referenced-path set after the
  candidates are known and immediately before the deletions. Reading the retained set first lets a
  volume created after that read be judged against a set that predates it. See the interleaving
  below.

### Ownership recovery when the writing worker is gone

The sweep runs in the reconciler (ADR-0021), in this order:

1. `pool.refresh(0)`, `listAllVolumes(0)`, and `parse_module_volume_name(volume.name())` for each.
   `None` ends that volume's evaluation (N3).
2. Each parse result *is* the recovery. `(system_id, run_id, operation_nonce, kind)` comes off the
   name, so nothing is reconstructed from a dead worker's memory, its console output, or the
   volume's bytes.
3. **Then** read the retained-owner set (N6, N10).
4. Owner in the retained set → retained, no further action.
5. Owner absent → no outstanding claim. Resolve the referenced-path set (N7) and drop any
   candidate whose path is in it, reporting it as a conflict.
6. Delete the rest under N8.

**Why the read order matters (N10).** A volume observed at enumeration time only proves it was
created before the enumeration. It proves nothing about its row versus an earlier read of the
retained set. Take a worker that writes its row at t1 and creates its source volume at t2, both
after the reconciler resolved the retained set at r1: t1 > r1, so the row is not in the set the
sweep holds; t2 < r2, so the volume is in the listing. The owner parses, is absent from the stale
set, is attached to no domain yet — the appliance starts later — and gets deleted underneath a
running attempt. Enumerating first makes every judged volume older than the set it is judged
against.

**Why the intent ordering matters (N5).** With the reads ordered, a volume in the listing has a
row written before the retained-set read *provided* the row precedes the volume. Without N5 the
worker can create the volume first and die before the row exists, and the sweep correctly sees no
claim on a volume a live attempt is about to use.

The two requirements close the two halves of one race and neither is sufficient alone.

### Threat model

The change parses names it did not produce, off a host KDIVE does not exclusively own, and uses
the parse to authorise deletion. That makes it security-relevant.

**Boundaries.** One boundary is widened and none is added. The widened one is the remote libvirt
storage pool: volume names enter from a host where an operator, another tool, or another KDIVE
deployment may also create volumes, and the value that crosses is a name that decides a deletion.

The widening is real and worth naming precisely. The existing deletion path,
`delete_owned_attempt_volume`, refuses unless `inspection.proves_detached(pool, name)` — a
positive proof, per attempt. The sweep cannot obtain that proof for a volume it discovered rather
than expected, so it substitutes a negative check against a referenced-path set. That is a weaker
control, and the residual it leaves is the window between resolving the referenced-path set and
issuing the delete, in which a domain referencing a candidate could be defined. N7 bounds the
window by resolving the set immediately before the deletions; the residual is accepted, because
closing it would need a host-wide lock KDIVE does not hold and ADR-0585 already places privileged
operator interference outside the boundary.

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
| Ownership claim → deletion | N6 obligation join against the durable store in N10's order, then the N7 referenced-path guard. Deletion needs all three to agree. |
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

`FakeStoragePool.createXML(xml, flags)` parses the submitted XML and retains **only** the fields a
real dir-pool readback carries, observed on libvirt 12.0.0:

- the `type='file'` attribute on `<volume>`
- `name`
- `key` (synthesised as `<pool-path>/<name>`, as libvirt does for a dir pool)
- `capacity`, and `allocation` and `physical` derived from it
- the whole `target` subtree: `path`, `format`, `permissions` (`mode`, `owner`, `group`, `label`),
  and `timestamps` (`atime`, `mtime`, `ctime`, `btime`)

The modelled set is the whole observed readback, not the subset today's code reads. A double that
models only what is currently read is the original defect one level down: the first check to read
a persisted field the double omits goes green against nothing. `permissions` and `timestamps` earn
their place concretely — ADR-0588's rejected grace-window alternative turns on `timestamps` being
present, so anyone revisiting that decision must find them in the double.

`FakeStorageVolume.XMLDesc(flags)` re-renders from that retained state. Every other submitted
element — `<metadata>` above all — is dropped. The double therefore models libvirt's projection
rather than echoing the caller.

**Fidelity test.** `tests/live_vm/test_libvirt_storage_double_fidelity.py`, marked `live_vm`,
creates a real dir-pool volume through a real libvirt connection with a `<metadata>` child and one
other unmodelled element present, then asserts the real `XMLDesc` and the double's `XMLDesc` agree
on the child tag set **at the top level and inside `target`**, and that both omit the unmodelled
elements. The `target` half is what makes the arm bite: top-level tags match by construction, so a
divergence inside the subtree is exactly what a top-level-only assertion cannot see. This is the
only arm that can catch the double drifting from libvirt, and it is the arm that would have caught
the original defect. It needs libvirt and a writable directory — a strict subset of the `live_vm`
environment contract, no KVM guest — and it carries its own skip guard (Task 6), since the tier
has no shared environment gate to inherit.

**How a wrong implementation is caught.** Each row names the failure and the single arm that goes
red for it:

| Wrong implementation | Arm that fails |
|---|---|
| Ownership written anywhere libvirt does not persist | The double drops it, so the reap unit tests fail on an unrecoverable owner instead of passing on an echo. |
| The double drifts from libvirt's real projection | `test_libvirt_storage_double_fidelity.py` on a host with libvirt. |
| Grammar overruns `NAME_MAX` | The name-budget test asserting the longest renderable name is 135 bytes and under 255. |
| Grammar admits a foreign shape | The negative table: a name one character off in each field, a prefix-only name, and a name with a path separator all parse to `None`. |
| Grammar omits a kind the provider renders | The kind-coverage test: every name `_names` and `_reap_marker_name` can produce parses, so a journal volume is never classified as foreign. |
| Renderer and parser disagree | The round-trip property test over generated tuples. |
| The sweep deletes an attached volume | The referenced-path guard test: a candidate whose path is in the referenced-path set is retained and reported. |
| The sweep deletes a live worker's volume | The N10 interleaving test: enumerate the pool, *then* insert the owner into the retained set, and assert the volume is retained. An implementation that reads the set first fails it. |
| The sweep deletes the recovery point | The obligation test: an attempt that completed its mutation but has not reached ADR-0585's `restored` keeps its scratch volume. An implementation keyed on "attempt running" fails it. |
| A second metadata channel survives | `rg -n 'urn:kdive:remote-module' src/` returns nothing, asserted by a test over the provider tree. |

x86_64 work and verification complete first; the native ppc64le proof is deferred to a separate
later run on native POWER hardware.

## Implementation plan

Owned by #2129. It applies to the branch `feat/crash-resumable-remote-modules-2129`, not to this
one.

### Global constraints

- Python 3.14, `uv`-managed. Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs
  whole-tree with strict defaults.
- Module docstrings cite the ADRs they implement; cite ADR-0588 and ADR-0585.
- `adr-status-check` rejects a **Proposed** ADR cited from `src/` or `tests/`. ADR-0588 stays
  **Proposed** until the PR that implements its decision merges; that PR both adds the ADR-0588
  citations and flips the status, per `docs/adr/README.md`. Intermediate PRs cite `#2129` instead.
- Doc style: **Milestone**, never "Sprint"; no "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test`, and `just ci` before push. Run them bare.
- No new dependency. The grammar is `re` and `uuid` from the stdlib.

Expected implementation size: 900–1,300 changed lines (L) — derived from the six new files and the
five modified entries in the map below, counting the tests each task adds. The band is L rather
than M because two of the modified entries, `remote_module_operation.py` and the referenced-path
builder, were found during design review and each carries its own task.

### File map

| Path | Answerable for |
|---|---|
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volume_names.py` | **new.** Render and parse the grammar. Nothing else. |
| `src/kdive/providers/remote_libvirt/reaping/module_volumes.py` | **new.** The reconciler sweep and its referenced-path builder. |
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py` | **modified.** Drops the `attempt-volume` channel at all three readback sites; calls the renderer. |
| `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_operation.py` | **modified.** Drops the second `attempt-volume` readback pair and the whole `attempt-reap` channel; replaces the `inventory()` prefix match with the parse. |
| `tests/providers/remote_libvirt/fakes.py` | **modified.** Gains the projection-modelling storage double. |
| `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py` | **new.** Grammar tests. |
| `tests/providers/remote_libvirt/reaping/test_module_volumes.py` | **new.** Sweep tests. |
| `tests/live_vm/test_libvirt_storage_double_fidelity.py` | **new.** Double-versus-libvirt fidelity. |
| `tests/providers/remote_libvirt/test_fakes_storage.py` | **new.** Proves the double drops what it does not model. |
| `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py` | **modified.** Uses the shared double; metadata assertions go. |
| `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_operation.py` | **modified.** Same, for the second channel. |
| #2129's recovery-point attempt path | **modified.** Task 7's intent-before-volume ordering; not on `main`, so named by contract. |

### Task 1 — the grammar

Creates `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volume_names.py`,
tests `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py`.

**Interfaces produced** — every later task consumes these exact signatures:

```python
MODULE_VOLUME_NAME_MAX_BYTES = 255
MODULE_VOLUME_KINDS = ("source.ext4", "scratch.ext4", "reaping.journal", "reaped.journal")

@dataclass(frozen=True, slots=True)
class ModuleVolumeOwner:
    system_id: str
    run_id: str
    operation_nonce: str
    kind: str

def render_module_volume_name(
    system_id: str, run_id: str, operation_nonce: str, kind: str
) -> str: ...

def parse_module_volume_name(name: str) -> ModuleVolumeOwner | None: ...
```

`render_module_volume_name` raises `ValueError` on a malformed input rather than emitting a name
that will not parse. `parse_module_volume_name` returns `None` for anything that does not
full-match; it never raises on caller input.

**Interfaces consumed** — none.

Steps:

1. Write `test_round_trip_recovers_the_owner`: for a fixed System UUID, Run UUID, 32-hex nonce,
   and each kind in `MODULE_VOLUME_KINDS`,
   `parse_module_volume_name(render_module_volume_name(...))` equals the `ModuleVolumeOwner` those
   arguments describe.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volume_names.py -q`.
   Expect collection to fail with `ModuleNotFoundError` for `remote_module_volume_names`.
3. Write the module: a module-level `_NAME = re.compile(...)` anchored with `fullmatch`, using
   `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` for each UUID, `[0-9a-f]{32}`
   for the nonce, and `(source\.ext4|scratch\.ext4|reaping\.journal|reaped\.journal)` for the
   kind. Render with an f-string in the same field order. Validate render inputs against the same
   components.
4. Run the same command. Expect 1 passed.
5. Write `test_longest_name_fits_the_budget`: the longest renderable name (both UUIDs all-`f`,
   nonce all-`f`, kind `reaping.journal`) is 135 bytes when UTF-8 encoded and is
   `< MODULE_VOLUME_NAME_MAX_BYTES`.
6. Write `test_foreign_names_do_not_parse`, parametrised over: `""`; `"kdive-module-"`; a valid
   name with its kind suffix removed; with `source.ext4` replaced by `backup.ext4`; with
   `reaping.journal` replaced by `reaping.log`; with one UUID hex digit uppercased; with a
   31-character nonce; with a 33-character nonce; the valid name prefixed by `x`; suffixed by `x`;
   with `/` substituted for the final `-`; `"../" + valid`; and a 300-character name. Each must
   return `None`.
7. Write `test_render_rejects_malformed_input`, parametrised over a non-UUID system id, an
   uppercase nonce, and an unknown kind. Each must raise `ValueError`.
8. Run the same command. Expect all passed.
9. Commit.

Acceptance: this module is the only place in `src/` that builds or recognises a module *volume*
name. Check it with `rg -n 'kdive-module-\{|kdive-module-<' src/`, which must return hits only in
this file — a bare `rg -n 'kdive-module-' src/` matches thirteen unrelated strings across
`build_artifacts/validation.py`, `remote_module_operation.py` (including the appliance domain
name) and `remote_module_volumes.py` (tempfile prefixes and the manifest label), none of which is
a volume name.

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
   the returned volume's `XMLDesc(0)` parses to a `<volume type='file'>` root whose child tags are
   exactly `{name, key, capacity, allocation, physical, target}` and whose `target` child tags are
   exactly `{path, format, permissions, timestamps}`, and that neither `metadata` nor
   `backingStore` appears anywhere in it.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/test_fakes_storage.py -q`. Expect
   an `ImportError` for `FakeStoragePool`.
3. Implement both classes in `fakes.py`. `createXML` parses with
   `xml.etree.ElementTree.fromstring`, reads `name`, `capacity`, `target/path`, and
   `target/format/@type`, synthesises `key` and `path` as `f"{pool_path}/{name}"` when the XML
   gives no `target/path`, sets `allocation` and `physical` equal to `capacity`, fills
   `target/permissions` with fixed `0600`/`0`/`0`/`""` values and `target/timestamps` with fixed
   values, and stores only those. `XMLDesc` renders them from a fixed template carrying
   `type='file'` on the root. Nothing retains the submitted string.
4. Run the same command. Expect 1 passed.
5. Add `test_xmldesc_is_not_the_submitted_string`: assert the submitted XML string is not a
   substring of, and not equal to, the readback.
6. Run the same command. Expect 2 passed.
7. Commit.

Acceptance: `rg -n 'self.xml' tests/providers/remote_libvirt/fakes.py` returns nothing, and the
modelled field set matches the spec's *Test doubles* list exactly. The `permissions` and
`timestamps` values are fixed placeholders — the double models their *presence*, which is what the
fidelity test compares, not the host's actual values.

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
   request.operation_nonce, kind)` for `kind` in `("source.ext4", "scratch.ext4")`.
5. Replace the `_identity`/`_metadata` block in `validate_attempt_volumes` — `expected_identity`,
   `attributes`, `expected_attributes`, and the
   `_conflict("remote module volume ownership or digest mismatched", volume=name)` it raises —
   with `parse_module_volume_name(volume.name())` compared against the `ModuleVolumeOwner` the
   request describes. A `None` or mismatched parse raises
   `_conflict("remote module volume ownership mismatched", volume=name)`. Keep the capacity check
   against `volume.info()[1]` and the `target/format` is-raw check, moving both into a small
   `_readback_facts(volume, name) -> int` helper that reads only fields libvirt persists.
6. Do the same at the other two `_metadata` call sites, which the plan's readback rewrite does not
   otherwise reach and without which the module does not import:
   - `validate_scratch_volume` — replace its `attributes` comparison, including the
     `"identity": expected.identity` entry, with the same parse-against-expected check plus
     `_readback_facts`.
   - `delete_owned_attempt_volume` — this one is a **safety gate**, not a validation: it refuses
     to delete unless the readback attributes match, including `"identity": volume.identity`.
     Replace the attribute match with the parse-against-expected check, and keep its existing
     `inspection.proves_detached(volume.pool, volume.name)` guard exactly as it is. That positive
     detachment proof is the stronger control the sweep cannot obtain; the attempt-scoped path
     must not lose it.
7. Drop `PreparedVolume.identity`, whose only producer was `_identity`. Two literal
   `PreparedVolume(...)` constructions consume it — one in `_prepared`, one as
   `expected_prepared` in `validate_attempt_volumes` — plus the `volume.identity` read in
   `delete_owned_attempt_volume` and the `expected.identity` read in `validate_scratch_volume`.
   The digest binding it stood for is already carried by `_inspect_remote_source`, which downloads
   the source image and checks it against the manifest; the scratch volume's digest was the
   placeholder `"sha256:" + "0" * 64` and verified nothing.
8. Run the same command. Expect all passed.
9. Run `just lint` and `just type`. Expect both clean; fix any unused import the deletions left.
10. Commit.

Acceptance: `rg -n 'urn:kdive:remote-module-volume' src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py`
returns nothing, the module's docstring cites ADR-0588, and `delete_owned_attempt_volume` still
calls `proves_detached`.

### Task 4 — drop the second channel from the operation module

Modifies `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_operation.py` and
`tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_operation.py`. Without this
task the durable reads still fail closed after Task 3, so the defect the design exists to remove
survives.

**Interfaces consumed:** `render_module_volume_name`, `parse_module_volume_name`,
`ModuleVolumeOwner`, `MODULE_VOLUME_KINDS` from Task 1; `FakeStoragePool`, `FakeStorageVolume`
from Task 2.

Steps:

1. In the test module, swap the local storage doubles for `FakeStoragePool` /
   `FakeStorageVolume` and delete every assertion that reads an owner or reap attribute out of
   volume XML.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_operation.py -q`.
   Expect failures at the `attempt-volume` and `attempt-reap` readbacks. This is the red that
   proves the second and third occurrences.
3. Replace the two `attempt-volume` readbacks — the `root.findall("./metadata/{urn:kdive:remote-module-volume:v1}attempt-volume")`
   in `_read_file` and the one in `inventory()` that reconstructs the expected volume name from
   the metadata attributes — with `parse_module_volume_name(name)`. `inventory()` gets the whole
   owner tuple from the parse, so it no longer reconstructs anything.
4. Replace `inventory()`'s `if not name.startswith("kdive-module-")` filter with
   `if parse_module_volume_name(name) is None`, satisfying N2.
5. Delete `_REAP_METADATA_NS` and every `attempt-reap` read and write — the `findall` calls in
   `reap_marker`, `_validate_reap_marker`, `_operation_from_reap_marker` and `inventory()`, and
   the `SubElement` that writes it. A journal volume's durable content becomes its name plus the
   attempt row; the nineteen attributes the element carried come from that row.
6. Rewrite `_reap_marker_name` to call `render_module_volume_name(..., f"{state}.journal")`, and
   assert `f"{state}.journal" in MODULE_VOLUME_KINDS` so a new state cannot be introduced without
   extending the grammar.
7. Run the same command. Expect all passed.
8. Run `just lint` and `just type`. Expect clean.
9. Commit.

Acceptance: `rg -n 'urn:kdive:remote-module' src/` returns nothing, and
`rg -n 'startswith\("kdive-module-"\)' src/` returns nothing.

### Task 5 — the reconciler sweep

Creates `src/kdive/providers/remote_libvirt/reaping/module_volumes.py`, tests
`tests/providers/remote_libvirt/reaping/test_module_volumes.py`.

**Interfaces consumed:** `parse_module_volume_name`, `ModuleVolumeOwner` from Task 1;
`FakeStoragePool`, `FakeStorageVolume` from Task 2; the `_lookup_pool` error-wrapping pattern and
the `Protocol` shapes in `remote_module_volumes.py`; `CategorizedError` and `ErrorCategory` from
`kdive.domain.errors`; `reaping/boot_artifacts.py` for module structure.

**Interfaces produced:**

```python
class ModuleVolumeReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...

def list_owned_module_volumes(
    conn: ModuleVolumeReaperConn, pool_name: str
) -> list[tuple[str, ModuleVolumeOwner]]: ...

def referenced_volume_paths(conn: ModuleVolumeReaperConn) -> frozenset[str]: ...

def reap_orphaned_module_volumes(
    conn: ModuleVolumeReaperConn,
    pool_name: str,
    *,
    retained_owners: Callable[[], Collection[ModuleVolumeOwner]],
) -> int: ...
```

`retained_owners` is a **callable**, not a collection: N10 requires the set to be read after the
enumeration, and taking a collection would force the caller to resolve it first, which is the
race. `referenced_volume_paths` is the whole-pool guard N7 says no existing function provides —
`protected_volume_paths` resolves caller-supplied names to paths and enumerates no domains, and
`inspect_module_attachments` needs an attempt-scoped `ExpectedAttachmentState`. The sweep calls it
itself, after enumeration and immediately before the deletions.

Steps:

1. Write `test_foreign_volumes_are_never_touched`: a pool holding one valid module volume whose
   owner the `retained_owners` callable does not return, and four foreign names (an operator base
   image, a boot-artifact name, a near-miss module name with a 31-character nonce, and a
   `…-reaping.log` near-miss). Assert the return is 1 and that only the valid volume's `delete`
   was called.
2. Run `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_module_volumes.py -q`.
   Expect `ModuleNotFoundError`.
3. Implement `referenced_volume_paths`: `conn.listAllDomains(0)` for active and inactive domains,
   parse each `XMLDesc(0)`, and collect every `./devices/disk/source/@file`,
   `./devices/disk/source/@dev`, and, for `<disk type='volume'>`, the path the pool/volume pair
   resolves to. Wrap libvirt errors in `CategorizedError` with
   `ErrorCategory.INFRASTRUCTURE_FAILURE`. An unresolvable reference fails closed — it is added to
   the set, so the volume is protected rather than deleted.
4. Implement `list_owned_module_volumes` and `reap_orphaned_module_volumes` following
   `reaping/boot_artifacts.py`'s structure, in N10's order: look up the pool, `refresh(0)`,
   `listAllVolumes(0)`, parse each name and drop `None`; **then** call `retained_owners()`;
   drop owners it returns; **then** call `referenced_volume_paths` and drop candidates whose
   `path()` is in it, raising the existing conflict shape for each; delete the rest. Error
   details are limited to `pool` and `volume`.
5. Run the same command. Expect 1 passed.
6. Add: `test_retained_owner_is_kept`; `test_referenced_volume_is_kept_and_reported` (a domain
   references the candidate's path); `test_already_deleted_volume_counts_as_removed` (delete
   raises `VIR_ERR_NO_STORAGE_VOL`); `test_list_owned_returns_name_and_owner`;
   `test_journal_volumes_are_candidates` (a `…-reaped.journal` whose owner is not retained is
   deleted); `test_retained_set_is_read_after_enumeration` — the N10 arm: a `retained_owners`
   callable that records when it was called and inserts the owner at call time, asserting the
   volume is retained, so an implementation resolving the set before enumeration fails; and
   `test_unrestored_attempt_keeps_its_scratch_volume` — the obligation arm: an owner whose attempt
   has completed but whose ADR-0585 obligation is un-discharged is returned by `retained_owners`
   and its `scratch.ext4` volume survives the sweep.
7. Run the same command. Expect 8 passed.
8. Run `just lint` and `just type`. Expect clean.
9. Commit.

Acceptance: no path through the module can delete a volume whose name did not full-match; the
sweep never opens a volume's contents; and the module never calls `retained_owners()` before it
has finished enumerating.

### Task 6 — the fidelity proof

Creates `tests/live_vm/test_libvirt_storage_double_fidelity.py`.

**Interfaces consumed:** `FakeStoragePool` from Task 2.

Steps:

1. Write the skip guard first, as a module-level fixture, because the tier has no shared
   environment gate to inherit and `libvirt.open` raises `libvirt.libvirtError` on a host with no
   session daemon — which pytest reports as an error, not a skip:

   ```python
   @pytest.fixture(scope="module")
   def session_conn() -> Iterator[object]:
       libvirt = pytest.importorskip("libvirt")
       try:
           conn = libvirt.open("qemu:///session")
       except libvirt.libvirtError as unavailable:
           pytest.skip(f"no session libvirt: {unavailable}")
       try:
           yield conn
       finally:
           conn.close()
   ```

2. Write the test body, marked `live_vm`: define and start a `dir` pool over a `tmp_path` target,
   `createXML` a volume whose XML carries a `<metadata>` child and a `<backingStore>` child, and
   read it back through both the real pool and `FakeStoragePool`.
3. Assert the two readbacks agree on the root element's tag and `type` attribute, on the top-level
   child tag set, and on the `target` child tag set — and that neither contains `metadata` or
   `backingStore`. The `target` comparison is the load-bearing one: top-level tags match by
   construction.
4. Tear down: delete the volume, then destroy and undefine the pool, in a `finally`.
5. Run `just test-live` on a host with libvirt. Expect 1 passed. On a host without one, run
   `uv run python -m pytest tests/live_vm/test_libvirt_storage_double_fidelity.py -q` and expect
   1 skipped, not 1 error.
6. Commit.

Acceptance: the test fails if `FakeStoragePool` is changed to echo its input or to drop
`target/permissions`, and skips rather than errors on a host without libvirt.

### Task 7 — intent before volume

A contract note plus two tests, against #2129's recovery-point attempt path. That module does not
exist on `main`, so #2129 chooses the file; the contract and the tests are fixed here.

**The contract.** The durable attempt row naming `(system_id, run_id, operation_nonce, kind)` is
written before the first `createXML` of that attempt, and that row is what the sweep's
`retained_owners` callable reads (N5, N6).

**Interfaces consumed:** `render_module_volume_name` from Task 1.

Steps:

1. Write `test_attempt_row_precedes_volume_creation`: drive the attempt path with a durable-store
   double and a `FakeStoragePool` that both append to one shared call log, and assert the row
   write appears before the first `createXML`. An implementation that creates the volume first
   must make it red.
2. Write `test_row_without_volume_reconciles_cleanly`: an attempt whose row exists and whose
   volumes do not is reconciled without error and produces no orphan.
3. Run the test file the two tests were added to, bare. Expect both passed.
4. Commit.

Acceptance: `reap_orphaned_module_volumes`'s docstring names N5 as the precondition it relies on,
and these two tests are what hold it. Reversing the order in the attempt path turns them red.

### Verification for the whole change

Run bare, in this order: `just lint`, `just type`, `just test`, then `just ci`. Then `just
test-live` on the self-hosted native-KVM runner for Task 6's arm. `just ci` alone is not
sufficient evidence for this change — the defect it is fixing passed `just ci` in full — so the
`test-live` arm is what closes it on x86_64.

## Deferrals and follow-ups

- #2158 — migrate the remote boot-artifact path onto this channel. Until it lands, the two remote
  reap paths use different ownership channels.
- Wiring `reap_orphaned_module_volumes` into the reconciler's remote sweep belongs to #2129's
  recovery-point work; this spec fixes its contract, not its call site.
- The durable schema behind N6 — what an un-discharged obligation is, and the nineteen attributes
  the deleted `attempt-reap` element used to carry — is #2129's to define. This spec fixes the
  predicate (ADR-0585 `restored` or baseline commitment discharges it) and the read ordering
  (N10), which is what the sweep's correctness depends on; the columns are not a design decision
  this record needs to make. Task 7 carries N5's obligation into that work.
