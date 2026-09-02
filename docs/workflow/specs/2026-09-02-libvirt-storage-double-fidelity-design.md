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

Reproduced on this host on 2026-09-02: Fedora 44 x86_64, libvirt daemon and libs 12.0.0,
libvirt-python 12.5.0 in the project venv (`importlib.metadata.version("libvirt-python")`; the
system binding is 12.0.0 and the venv shadows it). Two volumes were created in a `dir` pool over a
temporary directory through `virStorageVolCreateXML` and read back with `XMLDesc(0)`.

The submitted overlay document carried a `<metadata>` child, a `<bogusElement>` child, a
`kdive='owned'` attribute on `<name>`, no root `type` attribute, and the `<backingStore>` branch
`render_volume_xml` emits (`src/kdive/providers/remote_libvirt/lifecycle/xml.py:54-64`).

- Every create is accepted with no error.
- The readback root is `<volume type='file'>`. libvirt does not merely supply a missing `type`, it
  **overrides** a submitted one: a document declaring `type='block'` still reads back `type='file'`
  from a dir pool, because the type comes from the pool backend.
- `capacity` is **normalised to bytes**. A submitted `unit='KiB'` value of 1024 reads back as
  `unit='bytes'` 1048576. The observed suffix table is `bytes`/`B` = 1, `K`/`KiB` = 1024,
  `KB` = 1000, `M`/`MiB` = 1048576, `MB` = 1000000, `G`/`GiB` = 1073741824, `GB` = 1000000000; an
  unknown suffix is rejected with `VIR_ERR_INVALID_ARG` (code 8).
- A document carrying no `<capacity>` is **rejected**, not defaulted:
  `libvirtError: XML error: missing capacity element`, `VIR_ERR_XML_ERROR` (code 27). A document
  carrying no `<target>` is accepted and reads back a full `target` with `format type='raw'`.
- A submitted `target/permissions/mode` **is** honoured: `0640` in, `0640` out, against `0600` for
  a document that submits none. A submitted `label` is **not**: an SELinux label supplied in the
  document is replaced by the host's own label for the created file.
- Top-level children of the backed overlay are exactly `name`, `key`, `capacity`, `allocation`,
  `physical`, `target`, and `backingStore`. For the base volume, submitted with no
  `<backingStore>`, they are the same list without `backingStore`: libvirt renders that branch
  only when the volume has one.
- `target` and `backingStore` each carry exactly `path`, `format`, `permissions`, and
  `timestamps`. `permissions` carries `mode`, `owner`, `group`, and `label`; `timestamps` carries
  `atime`, `mtime`, `ctime`, and `btime`. The `label` element carries the file's security label, so
  it is present on this SELinux host and is not universal.
- `metadata`, `bogusElement`, and the `kdive` attribute appear nowhere in either readback.
- The qcow2 overlay reads back `capacity` 1048576, `allocation` 200704, `physical` 196616, and
  `info()` returns `[0, 1048576, 200704]`. Allocation and physical are host facts about the file on
  disk; they are neither equal to capacity nor to each other, and `info()[2]` is the allocation.
- `grep -c metadata /usr/share/libvirt/schemas/storagevol.rng` matches nothing (exit 1); the same
  grep against `domaincommon.rng` matches 3, so the element exists for domains and not for volumes.
- `virStorageVol` exposes `XMLDesc`, `delete`, `download`, `info`, `infoFlags`, `key`, `name`,
  `path`, `resize`, `storagePoolLookupByVolume`, `upload`, `wipe`, and `wipePattern` — no metadata
  accessor. `virDomain` exposes `metadata` and `setMetadata`.

That readback is the modelled set. Modelling only the subset the current provider code reads would
be the same defect one level down: a field a later change starts writing would be echoed back
because nothing modelled its absence. `backingStore` is the concrete instance of that trap — a
modelled set gathered from a raw capacity-only volume omits it, and every overlay the remote
provider creates carries one.

## Components

### `FakeStorageVolume` (`tests/providers/remote_libvirt/fakes.py`)

Holds a frozen modelled state and renders `XMLDesc` from it. It never retains the submitted
document, so no unmodelled input can reach a readback. Its libvirt-shaped surface is `name()`,
`key()`, `path()`, `info()`, `XMLDesc(flags=0)`, and `delete(flags=0)` — the calls the remote
provider's storage paths make (`src/kdive/providers/remote_libvirt/lifecycle/storage.py` reads
`info()[1]` and `path()`; the `Volume` protocol there declares `path`, `info`, and `delete`).

### `FakeStoragePool` (`tests/providers/remote_libvirt/fakes.py`)

Owns a target directory path and a name-keyed volume map. `createXML(xml, flags=0)` parses the
submitted document, derives the fields libvirt derives, retains the fields libvirt retains, and
returns a `FakeStorageVolume` over that state. `storageVolLookupByName` raises
`VIR_ERR_NO_STORAGE_VOL` through the suite's existing `libvirt_error` helper for an unknown or
deleted name. `listVolumes()` reports the live names. The submitted documents stay available as
`created_xml` for tests that assert on what the provider sent — that is an assertion about the
request, which is a separate question from what the platform keeps.

### Field derivation

Every field falls into exactly one of four classes, and getting the class wrong is the same defect
as echoing: a retained field libvirt actually overrides is an echo by another name.

**Retained** from the submitted document: `name`, `target/format@type`, `target/permissions/mode`,
and — when the document carries a `<backingStore>` — that branch's `path` and `format@type`. Absent
input takes a stated default: `target/format@type` to `raw`, `mode` to `0600`, and an absent
`<backingStore>` renders no `backingStore` element at all, which is what libvirt does.

**Derived**, matching libvirt: `key` and `target/path` are the pool target path joined with the
name. The root `type` attribute is always `file` — the dir-pool backend decides it, and a submitted
`type='block'` is overridden. `capacity` always renders `unit='bytes'` with the submitted value
converted through the suffix table observed under *Platform evidence*.

**Placeholders, not derivations**: `allocation`, `physical`, `permissions/owner`,
`permissions/group`, `permissions/label`, and every `timestamps` child. Real libvirt fills these
from the file on disk — the observed overlay reads back allocation 200704 and physical 196616
against a capacity of 1048576, and a submitted SELinux label is replaced by the host's own — and a
double cannot reproduce a host fact. So it renders `0` for allocation and physical (carrying
`unit='bytes'`), `0` for owner and group, the empty string for `label`, and `0` for every
timestamp. `backingStore` permissions and timestamps are placeholders on the same terms: they
describe the base file, which the submitted document never supplies. Their job is to prove the tags
are rendered, which is what the live-tier comparison checks. `info()` returns
`[0, capacity, allocation]`, so `info()[1]` is the one element a caller can rely on;
`src/kdive/providers/remote_libvirt/lifecycle/storage.py` reads exactly that.

**Rejected**, matching libvirt's own refusals: a document with no `<capacity>` raises
`VIR_ERR_XML_ERROR`, and a `capacity/@unit` outside the observed suffix table raises
`VIR_ERR_INVALID_ARG`. A double that accepts an input the platform refuses is over-permissive in
the same direction as one that echoes — a migrated test would build a capacity-less volume, watch
the double hand back a well-formed readback, and ship a provider path that raises in production.

A test asserting a double's mtime, allocation, owner, or security label would be asserting the
double against itself.

### `require_live_vm_storage_double` (`tests/live_vm/__init__.py`)

A new gate beside its five siblings, following the module's stated skip-versus-fail discipline. It
resolves the URI the same way the other families do — `KDIVE_LIBVIRT_URI` when set, otherwise the
caller's default of `qemu:///session` — and returns a frozen
`StorageDoubleContract(libvirt_uri=...)`.

Two checks stand in front of that, in order:

1. **The URI must be a local session URI**, tested with the module's existing
   `_is_local_session_uri`. This family's proof defines a pool whose target is a `tmp_path` on the
   client machine, under a `0700` directory owned by the invoking user, so it is a session-mode
   requirement rather than a generic libvirt one. `KDIVE_LIBVIRT_URI` is the shared override for
   every local family and defaults to `qemu:///system` for two of them, so an operator setting it
   for the throwaway family would otherwise silently retarget this one at a mode where the
   comparison is meaningless. A non-session URI fails loud, exactly as
   `require_live_vm_throwaway(session_required=True)` does for the same reason (#1258).
2. **The host's storage driver must answer.** The gate opens the URI and calls
   `listStoragePools()` before closing, because the proof needs the storage driver and not just a
   connection: modern libvirt packages the storage driver separately from the qemu driver, so a
   host can answer `libvirt.open` and have no storage backend. What the probe delivers is exactly
   what it does — a host whose storage driver does not answer a list call takes the skip-or-fail
   split instead of erroring inside the test body. It is not a guarantee that everything the proof
   does will work: a host that lists pools but cannot define one still errors in the test body, and
   no cheap probe short of running the proof would catch that. This premise is unreproduced on this
   host, which has the storage driver installed; the call was added because it costs one round trip
   and strictly widens what the gate can classify.

`libvirt.libvirtError` from either call is a failed probe, and the split is the family discipline:

- `KDIVE_LIBVIRT_URI` unset and the probe fails — this host runs no session daemon, so the
  family's environment is absent and the gate skips.
- `KDIVE_LIBVIRT_URI` set and the probe fails — an operator declared a host that does not answer.
  That is a mis-provisioned runner, not "no environment", so the gate fails loud, exactly as the
  throwaway family fails loud on a rootfs path that is set but not a file.

Only `libvirt.libvirtError` is treated as a failed probe. Any other exception is a defect in the
test environment and propagates. The probe connection is closed on both paths.

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
4. The readback's top-level child tag tuple, `target` child tag tuple, `backingStore` child tag
   tuple, and the `permissions` and `timestamps` child tag tuples under each equal the modelled set
   named under *Platform evidence*.
5. Retained fields round-trip: name, `target/format@type`, `target/permissions/mode`, and the
   `backingStore` path and format when one was submitted.
6. Derived fields derive: `key` and `target/path` from the pool target path; root `type` renders
   `file` even when the document declared `block`; `capacity` renders `unit='bytes'` with the
   submitted `KiB`/`MiB`/`KB` value converted.
7. Placeholder fields render as placeholders: `allocation` and `physical` are `0` carrying
   `unit='bytes'`, `owner` and `group` are `0`, `label` is empty, and `info()` is `[0, capacity, 0]`.
8. Defaults apply when the optional input is absent — no `<target>` still renders a full `target`
   with `format type='raw'`, and no `<backingStore>` renders no `backingStore` element.
9. Rejections match libvirt: no `<capacity>` raises `VIR_ERR_XML_ERROR`, and an unknown
   `capacity/@unit` raises `VIR_ERR_INVALID_ARG`.
10. Attributes on a modelled element that libvirt does not keep are dropped.
11. Pool behaviour: lookup returns the created volume, an unknown name raises
    `VIR_ERR_NO_STORAGE_VOL`, delete removes it, `created_xml` records the submitted documents.

Assertion 3 is the one that fails if the double is reverted to echoing, and assertions 1, 2, 4, 6,
and 10 all fail with it.

### `tests/live_vm/test_libvirt_storage_double_fidelity.py` — the double against libvirt

Marked `live_vm`, behind `require_live_vm_storage_double()`. It defines and starts a `dir` pool
over `tmp_path` and creates two volumes through the real `createXML`: a base, and an overlay backed
by it whose document also carries a `<metadata>` child, a `<bogusElement>` child, and an unknown
attribute. The overlay document is the shape `render_volume_xml` produces, so the comparison
exercises the input the provider actually submits rather than a hand-written raw volume — a proof
run on a capacity-only volume would pass while the double silently dropped `backingStore`.

Both documents are fed to `FakeStoragePool.createXML`, and each pair of readbacks is compared on:

- the root tag and its `type` attribute,
- the top-level child tag tuple — which distinguishes the two volumes, since only the overlay
  carries `backingStore`,
- the `target` child tag tuple, and the `backingStore` child tag tuple for the overlay,
- the `timestamps` child tag tuple under each of those,
- the `permissions` child tag sets under each of those, by two assertions:
  `real - {"label"} == double - {"label"}` on the label-stripped sets, and `real <= double` on the
  **unstripped** sets. `label` carries the file's security label, so a runner without SELinux emits
  three children where this host emits four; only `label` needs to be optional, and the subset leg
  on the full sets is what keeps the direction from going the permissive way.

It then asserts none of the readbacks contains an element tagged `metadata` or `bogusElement`, or
any element carrying the submitted unknown attribute key — by walking the parsed trees, not by
substring. A substring check over the readback strings would false-red: every readback carries the
pool target path three times, and `tmp_path` honours `TMPDIR` and `--basetemp`, so a runner whose
temp root happens to contain one of those tokens fails a test where the double and libvirt agree
perfectly. The submitted payload *values* (`run-1`, `zzz`) are checked by substring, because those
cannot appear in a path libvirt generates.

It compares tag structure, not values: `path`, `allocation`, `physical`, `permissions`, and
`timestamps` values are host facts a double cannot and should not reproduce, while the tag set is
the fidelity claim the unit proof rests on.

`conn` and `pool` are bound to `None` before the `try`, and teardown skips whichever is still
`None`. Otherwise a failure in `storagePoolDefineXML` leaves `pool` unbound and the `finally`
raises `UnboundLocalError` over the libvirt error that explains what went wrong. Teardown deletes
every volume the pool lists, then destroys and undefines the pool, then closes the connection, each
step guarded so an already-absent object does not mask the real failure.

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
  verified: `render_volume_xml` (`src/kdive/providers/remote_libvirt/lifecycle/xml.py:54-64`) emits
  a `<backingStore>` on every overlay, and submitting that document to real libvirt (qemu:///session,
  dir pool, daemon 12.0.0) returns a seventh top-level child `backingStore` with its own `path`,
  `format`, `permissions`, and `timestamps`. A double built from the raw-volume subset would drop a
  field libvirt keeps, so a test asserting an overlay has no backing chain would go green and be
  wrong in production — the same defect this issue closes, pointed the other way.
- **Gather the modelled set from a raw, capacity-only volume.** verified: the same probe. That
  input is the one class whose readback makes a six-child set look complete; the provider never
  submits it for an overlay.
- **Model the readback values as well as the tags — real permissions, real timestamps, real
  allocation.** verified: the observed overlay reads back capacity 1048576 with allocation 200704
  and physical 196616, and `info()` returns `[0, 1048576, 200704]` — values that come from the file
  on disk. judgment: a double reproducing them would be asserted against itself, and the live-tier
  comparison already covers everything a value comparison could.
- **Render `allocation` and `physical` equal to capacity, which reads as a derivation.** verified:
  the probe above shows all three differ on a qcow2 overlay. A plausible-looking wrong value is
  worse than an obvious placeholder, so the double renders `0` and the spec says it is a
  placeholder.
- **Retain the submitted root `type` and `capacity/@unit`.** verified: a document declaring
  `type='block'` reads back `type='file'` from a dir pool, and `<capacity unit='KiB'>1024</capacity>`
  reads back `<capacity unit='bytes'>1048576</capacity>` (qemu:///session, libvirt 12.0.0). For
  those two fields retaining the input *is* echoing — a migrated test could submit `unit='KiB'`,
  read `unit='KiB'` back from the double, and be wrong against libvirt.
- **Default an absent `<capacity>` to `0`.** verified: `pool.createXML("<volume><name>c.raw</name></volume>", 0)`
  raises `libvirtError: XML error: missing capacity element` (`VIR_ERR_XML_ERROR`, code 27). There
  is no readback to default, so a double that produces one accepts an input the platform refuses.
- **Treat `target/permissions` as a placeholder block.** verified: a submitted `<mode>0640</mode>`
  reads back `0640` where a document submitting none reads back `0600`, so `mode` is honoured and
  is retained. `label` is not — a submitted SELinux label is replaced by the host's own — so it
  stays a placeholder, and `owner`/`group` stay placeholders because honouring them requires a
  chown the test user cannot perform.
- **Assert the absence of `metadata`/`bogusElement`/`kdive` by substring over the readback.**
  verified: this reviewer's own probe pool lived under a path containing the token `kdive`, and
  `"kdive" in XMLDesc(0)` was `True` against a readback carrying no such attribute. `tmp_path`
  honours `TMPDIR` and `--basetemp`, so the substring form is a false-red waiting for the wrong
  runner. Structural absence over the parsed tree has no such failure mode.
- **Let the gate accept any `KDIVE_LIBVIRT_URI`.** verified: `tests/live_vm/__init__.py:12-13`
  records that variable as the shared override for every local family, and the throwaway and
  provisioned families default to `qemu:///system`. judgment: the proof's pool target is a
  client-side `tmp_path`, so an override set for another family would retarget this one at a mode
  where the comparison means nothing.
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
