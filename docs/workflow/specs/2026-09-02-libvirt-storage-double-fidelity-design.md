# libvirt storage double fidelity design

## Scope and authority

Issue #2164 makes the shared remote-libvirt storage test double discard exactly what libvirt
discards for a dir-pool volume, and lands the two proofs that hold that claim to account. It is
part of epic #2129.

The change is tests only. No `src/` file changes, no schema, no dependency, no migration, and no
ADR. The libvirt-discards fact this design rests on is recorded by #2157; this design records only
how the double models it and how the modelling is proved.

Migrating the nine existing test modules that carry their own local `createXML` double belongs to
the children of #2129 that own those modules, not here.

The implementation targets x86_64 and ppc64le. x86_64 work and verification complete first; the
native ppc64le proof is deferred to a separate later run on native POWER hardware.

## Problem

`just ci` passed 15,304 tests on #2129's branch against a design that cannot work in production.
The branch's `Pool.createXML` double stored the submitted volume XML and returned it verbatim from
`XMLDesc`, so an implementation that wrote run ownership into a `<metadata>` child of a storage
volume read it straight back out. libvirt does not persist that element. A double that echoes its
input asserts nothing about what the platform keeps, and the green gate was the finding.

## Platform evidence

Reproduced on this host against libvirt 12.0.0 / libvirt-python 12.0.0, x86_64, 2026-09-02, by
creating a dir-pool volume through `virStorageVolCreateXML` whose XML carried a `<metadata>` child
and a `<bogusElement>` child:

- The create is accepted with no error.
- `XMLDesc(0)` returns a `<volume type='file'>` whose top-level children are exactly `name`, `key`,
  `capacity`, `allocation`, `physical`, and `target`, and whose `target` children are exactly
  `path`, `format`, `permissions`, and `timestamps`. Neither `metadata` nor `bogusElement` appears
  anywhere in the readback.
- `permissions` carries `mode`, `owner`, `group`, and `label`; `timestamps` carries `atime`,
  `mtime`, `ctime`, and `btime`.
- `grep -c metadata /usr/share/libvirt/schemas/storagevol.rng` matches nothing; the same grep
  against `domaincommon.rng` matches, so the element exists for domains and not for volumes.
- `virStorageVol` exposes `XMLDesc`, `delete`, `download`, `info`, `infoFlags`, `key`, `name`,
  `path`, `resize`, `storagePoolLookupByVolume`, `upload`, `wipe`, and `wipePattern` — no metadata
  accessor. `virDomain` exposes `metadata` and `setMetadata`.

That readback is the modelled set. Modelling only the subset the current provider code reads would
be the same defect one level down: a field a later change starts writing would be echoed back
because nothing modelled its absence.

## Components

### `FakeStorageVolume` (`tests/providers/remote_libvirt/fakes.py`)

Holds a frozen modelled state and renders `XMLDesc` from it. It never retains the submitted
document, so no unmodelled input can reach a readback. Its libvirt-shaped surface is `name()`,
`key()`, `path()`, `info()`, `XMLDesc(flags=0)`, and `delete(flags=0)` — the calls the remote
provider's storage paths make (`src/kdive/providers/remote_libvirt/lifecycle/storage.py` reads
`info()[1]` and `path()`; the `StorageVol` protocol there declares `delete`).

### `FakeStoragePool` (`tests/providers/remote_libvirt/fakes.py`)

Owns a target directory path and a name-keyed volume map. `createXML(xml, flags=0)` parses the
submitted document, derives the fields libvirt derives, retains the fields libvirt retains, and
returns a `FakeStorageVolume` over that state. `storageVolLookupByName` raises
`VIR_ERR_NO_STORAGE_VOL` through the suite's existing `libvirt_error` helper for an unknown or
deleted name. `listVolumes()` reports the live names. The submitted documents stay available as
`created_xml` for tests that assert on what the provider sent — that is an assertion about the
request, which is a separate question from what the platform keeps.

### Field derivation

From the submitted document the double retains `name`, `capacity` (with its `unit` attribute),
the root `type` attribute, and `target/format@type`. Everything else in the modelled set is
derived, matching libvirt: `key` and `target/path` are the pool target path joined with the name;
`allocation` and `physical` equal the retained capacity; `target/permissions` and
`target/timestamps` carry fixed placeholder values. Absent optional input takes a stated default —
root `type` defaults to `file`, `target/format@type` to `raw`, `capacity/@unit` to `bytes`, an
absent `capacity` to `0`.

The placeholder permission and timestamp values are deliberately not realistic. Their job is to
prove the tags are rendered, which is what the live-tier comparison checks; a test asserting a
double's mtime would be asserting the double against itself.

### `require_live_vm_storage_double` (`tests/live_vm/__init__.py`)

A new gate beside its five siblings, following the module's stated skip-versus-fail discipline. It
resolves the URI the same way the other families do — `KDIVE_LIBVIRT_URI` when set, otherwise the
caller's default of `qemu:///session` — then probes it by opening and immediately closing a
connection, and returns a frozen `StorageDoubleContract(libvirt_uri=...)`.

The probe is what makes the split:

- `KDIVE_LIBVIRT_URI` unset and the open fails — this host runs no session daemon, so the family's
  environment is absent and the gate skips.
- `KDIVE_LIBVIRT_URI` set and the open fails — an operator declared a host that does not answer.
  That is a mis-provisioned runner, not "no environment", so the gate fails loud, exactly as the
  throwaway family fails loud on a rootfs path that is set but not a file.

Only `libvirt.libvirtError` is treated as a failed probe. Any other exception is a defect in the
test environment and propagates.

The gate returns a URI rather than the open connection. A resolver that handed back a live
connection would put its lifetime in the caller's hands across a `pytest.skip`, and would not fit
the pure `EnvResolution[T]` shape the other five families share; the second open costs nothing in
a live-tier test.

## Proofs

### `tests/providers/remote_libvirt/test_fakes_storage.py` — the double itself

Not gated; runs in ordinary CI. It asserts:

1. A submitted `<metadata>` child does not appear in the readback.
2. A submitted unknown element does not appear in the readback.
3. The readback is not the submitted string — the direct guard against a return to echoing.
4. The readback's top-level child tag set and `target` child tag set equal the modelled set named
   under *Platform evidence*.
5. Retained fields round-trip: name, capacity and its unit, root `type`, `target/format@type`.
6. Derived fields derive: `key` and `target/path` from the pool target path; `allocation` and
   `physical` from capacity.
7. Defaults apply when the optional input is absent.
8. Attributes on a modelled element that libvirt does not keep are dropped.
9. Pool behaviour: lookup returns the created volume, an unknown name raises
   `VIR_ERR_NO_STORAGE_VOL`, delete removes it, `created_xml` records the submitted documents.

Assertion 3 is the one that fails if the double is reverted to echoing, and assertions 1, 2, 4, and
8 all fail with it.

### `tests/live_vm/test_libvirt_storage_double_fidelity.py` — the double against libvirt

Marked `live_vm`, behind `require_live_vm_storage_double()`. It defines and starts a `dir` pool
over `tmp_path`, creates one volume through the real `createXML` with a `<metadata>` child and a
`<bogusElement>` child, feeds the identical document to `FakeStoragePool.createXML`, and compares
the two readbacks on:

- the root tag and its `type` attribute,
- the top-level child tag set,
- the `target` child tag set,

then asserts neither readback contains `metadata` or `bogusElement` anywhere. It compares tag
structure, not values: `path`, `permissions`, and `timestamps` values are host facts a double
cannot and should not reproduce, while the tag set is the fidelity claim the unit proof rests on.

Teardown deletes every volume, then destroys and undefines the pool, then closes the connection,
under a `finally` that tolerates an already-absent object so a mid-test failure does not leave the
host holding a defined pool.

This test is the reason the unit proof can be trusted. Without it the modelled set is one author's
transcription of one readback, and nothing notices when a libvirt upgrade changes it.

## Testing

`just test-changed` and `just lint` and `just type` while iterating; `just ci` before delivery. The
live-tier file contributes a skip on any host without a session daemon and is excluded from
`just test` by the `live_vm` marker, so `just ci` exercises the unit proof and the gate's own unit
coverage in `tests/live_vm/test_gates.py`.

Beyond the guardrails, the design is proved by a controlled fault: reverting `XMLDesc` to return
the submitted document must turn the unit proof red, and a test that writes ownership into a volume
`<metadata>` child and reads it back must fail against the new double. A double that cannot be made
to fail that way is not modelling the discard.

## Considered and rejected

- **Keep echoing and assert on `created_xml` instead.** verified: that is what #2129's branch did —
  `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py:125-135` on
  `feat/crash-resumable-remote-modules-2129` builds `Volume(xml, ...)` from the submitted document
  — and `just ci` passed 15,304 tests over a production-fatal design. An assertion on the request
  cannot detect what the platform drops from it.
- **Model only the fields the provider reads today (`name`, `key`, `capacity`, `target/path`).**
  judgment: a later change that starts writing a field outside that subset gets it echoed back,
  which is the defect this issue exists to close, one level down.
- **Model the readback values as well as the tags — real permissions, real timestamps.** judgment:
  a double reproducing host filesystem facts would be asserted against itself, and the live-tier
  comparison already covers everything a value comparison could.
- **Have the gate return an open `libvirt.virConnect`.** judgment: it puts a connection's lifetime
  across a `pytest.skip` boundary in the caller's hands and breaks the pure `EnvResolution[T]`
  shape the five existing families share, to save one connection open in a live-tier test.
- **Add `createXMLFrom` to the double now.** judgment: no proof in this issue's scope exercises it,
  and an unproven clone double is the same over-permissive fidelity risk with no test holding it
  down. The module that needs it (`boot_artifact_volumes`) is migrated by a different child.
- **Write an ADR.** verified: the decision record for the libvirt-discards fact is ADR-0588, owned
  by #2157, and the campaign assigned no ADR number to #2164. The decisions here are test-fixture
  interface choices recorded above, not architecture.
- **Do nothing.** verified: nine test modules under `tests/` define their own `createXML` double
  and `tests/providers/remote_libvirt/fakes.py` models no storage at all, so the class of defect
  that shipped green on #2129's branch is reintroducible today with no red test.
