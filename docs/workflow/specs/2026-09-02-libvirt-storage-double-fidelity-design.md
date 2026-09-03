# libvirt storage double fidelity design

## Scope and authority

Issue #2164 makes the shared remote-libvirt storage test double discard exactly what libvirt
discards for a dir-pool volume, and lands the two proofs that hold that claim to account. It is
part of epic #2129.

The change is tests only. No `src/` file changes, no schema, no dependency, no migration, and no
ADR. The libvirt-discards fact this design rests on is recorded by #2157; this design records only
how the double models it and how the modelling is proved.

Migrating the six existing modules under `tests/providers/remote_libvirt/` that carry their own
local storage `createXML` double belongs to the children of #2129 that own those modules, not here.
A seventh storage double outside this provider (`tests/diagnostics/test_base_image_staging.py`) has
no such owner and is reported rather than adopted; see *Considered and rejected*.

The implementation targets x86_64 and ppc64le. x86_64 work and verification complete first; the
native ppc64le proof is deferred to a separate later run on native POWER hardware.

## Problem

`just ci` passed 15,304 tests on #2129's branch against a design that cannot work in production.
The branch's `Pool.createXML` double stored the submitted volume XML and returned it verbatim from
`XMLDesc`, so an implementation that wrote run ownership into a `<metadata>` child of a storage
volume read it straight back out. libvirt does not persist that element. A double that echoes its
input asserts nothing about what the platform keeps, and the green gate was the finding.

**Echoing is the visible form of a wider defect: a double that agrees with its input instead of
with the platform.** Verbatim echo is only its most obvious shape. Retaining a field the platform
*overrides* is the same defect wearing a modelled-looking coat — a document declaring `type='block'`
reads back `type='file'`, and `<capacity unit='KiB'>1024</capacity>` reads back
`<capacity unit='bytes'>1048576</capacity>`, so a double that hands either input straight back is
echoing under another name and a migrated test built on it is green and wrong. Accepting an input
the platform *rejects* is the third shape. The classification under *Field derivation* exists to
make each field's shape decidable rather than assumed.

**And the blind spot is not confined to this module.** A fidelity double only earns its cost if the
test drives the real entry point; a test that asserts against a convenient intermediate makes the
double's fidelity unobservable, however faithful the double is. #2163 found that shape elsewhere in
this provider: a schema cross-check validated already-parsed JSON rather than driving the
appliance's own reader, so a framing defect that cannot work in production sat under the same green
15,304-test suite. `tests/providers/remote_libvirt/fakes.py` is where this issue's work lands, not
the boundary of the problem. That is why the live-tier proof below drives the real
`virStorageVolCreateXML` and the real `XMLDesc` rather than comparing the double against a recorded
string: a transcription can only be checked by the thing it transcribes.

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
- `capacity` is **normalised to bytes**, and the rule is more permissive than a transcribed table:
  - A `<capacity>` with **no `unit` attribute** is accepted and reads back `unit='bytes'`. This is
    the case that matters most, because `render_volume_xml` emits exactly that — `ET.SubElement(volume,
    "capacity").text = str(capacity_bytes)`, no attribute. A double that rejected an absent unit
    would refuse the provider's own document.
  - `unit=''` is accepted the same way.
  - Suffix matching is **case-insensitive**: `k`, `kib`, `Kib`, `KIB` all give 1024, and `b` and
    `Bytes` both give 1.
  - The table runs past `GB`, and **every multiplier below was read off a readback, none inferred**:
    `b`/`byte`/`bytes` = 1, `K`/`KiB` = 1024, `KB` = 1000, `M`/`MiB` = 1048576, `MB` = 1000000,
    `G`/`GiB` = 1073741824, `GB` = 1000000000, `T`/`TiB` = 1099511627776, `TB` = 1000000000000,
    `P`/`PiB` = 1125899906842624, `PB` = 1000000000000000, `E`/`EiB` = 1152921504606846976,
    `EB` = 1000000000000000000.
  - Observing the suffixes from `T` up takes a second probe, and the first attempt is worth
    recording because its failure is easy to misread. A `<capacity unit='T'>1</capacity>` over a
    pool on `/tmp` fails with `VIR_ERR_SYSTEM_ERROR` (code 38) — the suffix parsed and the 1 TiB
    file would not fit. Re-probing over a pool on a real disk with
    `<allocation unit='bytes'>0</allocation>` creates each volume sparsely and returns the readbacks
    above. Code 38 versus code 8 is what separates a create failure from a parse refusal, and
    stopping at the first probe would have left six multipliers transcribed rather than seen.
  - Only a suffix outside that set is refused, with `VIR_ERR_INVALID_ARG` (code 8): `' K'` (leading
    space) and `'bogusUnit'` both take that path, and code 8 versus code 38 is what distinguishes a
    parse refusal from a create failure.
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

Every field falls into exactly one of four classes. Getting the class wrong is not a lesser defect
than echoing — it is echoing, in the shape *Problem* names: a field classed retained that libvirt
actually overrides hands the input straight back, and a field classed defaulted that libvirt
rejects accepts an input the platform refuses.

**Retained** from the submitted document: `name`, `target/format@type`, `target/permissions/mode`,
and — when the document carries a `<backingStore>` — that branch's `path` and `format@type`. Absent
input takes a stated default: `target/format@type` to `raw`, `mode` to `0600`, and an absent
`<backingStore>` renders no `backingStore` element at all, which is what libvirt does.

**Derived**, matching libvirt: `key` and `target/path` are the pool target path joined with the
name. The root `type` attribute is always `file` — the dir-pool backend decides it, and a submitted
`type='block'` is overridden. `capacity` always renders `unit='bytes'` with the submitted value
converted through the suffix table observed under *Platform evidence*, matched
case-insensitively, with an absent or empty `unit` meaning bytes.

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

**Rejected**, matching libvirt's own refusals. All five were observed on this host:

| Input | Error | Code |
|---|---|---|
| a name already present in the pool | `VIR_ERR_STORAGE_VOL_EXIST` | 90 |
| no `<name>` element, or one with empty text | `VIR_ERR_XML_ERROR` | 27 |
| no `<capacity>` element | `VIR_ERR_XML_ERROR` | 27 |
| `<capacity>` text that is not a non-negative integer | `VIR_ERR_XML_ERROR` | 27 |
| a present, non-empty `capacity/@unit` outside the suffix table | `VIR_ERR_INVALID_ARG` | 8 |

An *absent* or empty `unit` is accepted, not refused, and the double must not confuse the two.

**Volume name length is deliberately not a sixth refusal.** ADR-0588 makes the volume name the
durable ownership channel and records that a dir-pool name round-trips to 255 bytes and is refused
at 256, so the question of whether this double should model that refusal is live rather than
academic. It should not. Observed on this host: a 255-byte name is created and reads back
byte-identically, and a 256-byte name fails with `VIR_ERR_SYSTEM_ERROR` (code 38) and the message
`Failed to create file ...: File name too long`. That is the filesystem's `NAME_MAX` refusing
`open()` after libvirt parsed the document — the same code-38 shape as a capacity that will not fit
the disk, and the same class as `allocation` or `timestamps`. `NAME_MAX` is a property of whatever
filesystem the pool target sits on, so a double asserting a 255-byte boundary would be asserting a
host fact it cannot know. The five modelled refusals are the ones libvirt decides itself: four
parse refusals (codes 27 and 8) and the duplicate-name refusal (code 90), which is libvirt's own
object-identity rule. ADR-0588's grammar renders at most 135 bytes, leaving 120 bytes of headroom,
so nothing in the ownership channel approaches the boundary.

The duplicate-name refusal is the one with a production consumer:
`src/kdive/providers/remote_libvirt/lifecycle/storage.py` guards `ensure_named_overlay` with an
existence check precisely because a duplicate create fails, and maps the libvirt error to
`PROVISIONING_FAILURE`. A double whose name-keyed map silently replaces an existing entry makes
that guard and that error path untestable — which is the third form of echoing the *Problem*
section names, and the reason a name-keyed dict is not on its own a model.

**This set is what the double models, not a claim that libvirt refuses nothing else.** libvirt's
storage-volume parser has refusals no proof here exercises. What the design owes is that every
refusal it *does* model matches the platform and that the ones it omits are omissions rather than
silent acceptances of things a migrating test would rely on. A double that accepts an input the platform refuses is over-permissive in
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

**The probe latches, once per process, per resolved URI — ADR-0580 governs this and it is not
optional.** That record is accepted and unsuperseded, and its decision is that a skip gate probing
a live resource probes it once per process and reuses the verdict, latching in both directions.
It was written about `docker_available()` pinging a daemon and `require_issuer()` fetching JWKS,
both under xdist load, and its reasoning transfers without modification: a `libvirt.open` against a
unix socket on a host busy running the suite that gate belongs to can be slow, and a gate meant to
answer "this host has no session daemon" would instead answer "this connect was slow" — a test
dropping out of a green run with no trace but a skip count nobody diffs. So a module-level dict in
`tests/live_vm/__init__.py` holds the verdict keyed by resolved URI, exactly as `require_issuer()`
keys its latch by JWKS URI. Keying by URI rather than latching a bare boolean is what lets the
gate's own unit tests vary `KDIVE_LIBVIRT_URI`, and ADR-0580's consequence binds those tests too:
a test that fabricates a verdict puts the real one back, on entry **and** on exit.

**What the latch holds is the probe outcome, not the resolution.** `require_issuer` latches
`_ISSUER_REACHABLE: dict[str, bool]`, and the distinction is load-bearing rather than cosmetic:
the resolution carries the ABSENT-versus-MISCONFIGURED split, and that split is decided by whether
`KDIVE_LIBVIRT_URI` is *set*, which is not part of the key. `_resolved_uri` returns the same string
for an unset variable defaulting to `qemu:///session` and for one explicitly set to
`qemu:///session`, so latching the whole resolution would let a skip latched under the unset case
be served back under the set case — turning a mis-provisioned runner into a skip, which is the one
outcome the module's discipline exists to prevent. So the latch is `dict[str, bool]` keyed by
resolved URI, and the ABSENT/MISCONFIGURED choice is made from `LIBVIRT_URI_ENV` on every call.

The available side latches as hard as the unavailable side, which is the consequence worth naming:
once a session has latched "this URI answers", a daemon that dies mid-run reddens the fidelity test
instead of quietly skipping it.

**On the CI live job there is no skip path, by design.** `.github/workflows/live.yml` exports
`KDIVE_LIBVIRT_URI` from `load_published_libvirt_uri()` before running
`pytest -m "live_vm and not live_vm_tcg"`, so the variable is always set there — which routes a
failed probe to the loud failure rather than the skip. That is the correct behaviour on a job that
has just stood the daemon up: a published socket that does not answer is a mis-provisioned runner,
not an absent environment. The skip path serves a developer host with the variable unset. Both
published URIs are `qemu+unix:///session?socket=…` and satisfy `_is_local_session_uri`, so check 1
admits them; this was verified locally against
`qemu+unix:///session?socket=/run/user/1000/libvirt/virtqemud-sock`, which is the same shape.

One honest limit on check 2: `listStoragePools()` returns `[]` rather than raising when the driver
is present with no pools, so the probe catches a storage driver that *errors*, not one that is
merely silent, and a host that lists pools but cannot define one still errors inside the test body.
No cheap probe short of running the proof would catch that.

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
attribute.

**The base document deliberately submits a non-default value for every platform-determined field
the comparison checks** — root `type='block'`, `<capacity unit='KiB'>1024</capacity>`, and
`<permissions><mode>0640</mode></permissions>`. Without that the comparison is tautological: the
overlay comes from `render_volume_xml`, which submits no `type`, no `unit`, and no `permissions`,
so every compared field would carry libvirt's default on both sides and an echoing double would
agree by accident. Observed: libvirt reads those three back as `type='file'`,
`<capacity unit='bytes'>1048576</capacity>`, and mode `0640`, so a double that echoed the input
renders `block`, `KiB`, and — if it dropped the submitted mode — `0600`, and each of the three
assertions fails. A comparison that cannot fail is not a proof. The overlay document is the shape `render_volume_xml` produces, so the comparison
exercises the input the provider actually submits rather than a hand-written raw volume — a proof
run on a capacity-only volume would pass while the double silently dropped `backingStore`.

The overlay document is built by calling the production renderer,
`render_volume_xml(name, capacity_bytes=…, backing_path=real_base.path())`, and appending the
`<metadata>`, `<bogusElement>`, and unknown-attribute noise to its output. Hand-writing that string
is what let the capacity model diverge in the first place: the renderer emits `<capacity>` with no
`unit` attribute, and a design that only ever tested hand-written documents carrying an explicit
unit never noticed. Driving the real producer is the same rule the *Problem* section states for
entry points, applied to the input side.

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
- the platform-determined values: `capacity`'s text and its `unit` attribute,
  `target/format@type`, and `target/permissions/mode`.

It then asserts none of the readbacks contains an element tagged `metadata` or `bogusElement`, or
any element carrying the submitted unknown attribute key — by walking the parsed trees, not by
substring. A substring check over the readback strings would false-red: every readback carries the
pool target path three times, and `tmp_path` honours `TMPDIR` and `--basetemp`, so a runner whose
temp root happens to contain one of those tokens fails a test where the double and libvirt agree
perfectly. The submitted payload *values* (`run-1`, `zzz`) are checked by substring, because those
cannot appear in a path libvirt generates.

It compares tag structure **plus the platform-determined values** — `capacity`'s text and its
`unit`, `target/format@type`, and `target/permissions/mode`. Those are fixed by libvirt's own rules
rather than by the host, so they are identical on every runner, and they are exactly the class the
*Problem* section calls the wider defect: a value the platform determines is where a double most
easily agrees with its input instead of the platform. A tag-only comparison would pass a double
that echoed `unit='KiB'` straight back.

It deliberately excludes `key`, `path`, `allocation`, `physical`, `timestamps`, and `permissions`'
`owner`, `group`, and `label`. Those are host facts a double cannot and should not reproduce.

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
- **Transcribe the capacity suffix table from one probe and reject anything outside it.** verified:
  a `<capacity>` with no `unit` attribute is accepted and reads back `unit='bytes'`, and
  `render_volume_xml` (`src/kdive/providers/remote_libvirt/lifecycle/xml.py:59`) emits exactly
  that — so a table-only model refuses the provider's own document. Matching is also
  case-insensitive (`k`, `kib`, `KIB` → 1024) and the table runs to `EiB`. The rule is what has to
  be modelled, not one probe's output.
- **Build the proofs' volume documents by hand.** verified: the hand-written documents in the
  earlier draft all carried an explicit `unit`, which is why the absent-unit case went unnoticed
  through two review passes. Calling `render_volume_xml` makes the proof's input the provider's
  real output.
- **Probe the gate's URI on every call.** verified: ADR-0580 is accepted (2026-08-25), carries no
  supersession banner, and decides that a skip gate probing a live resource probes once per process
  and latches both directions. A per-call `libvirt.open` under xdist load is the exact shape
  #2074 recorded — a slow connect indistinguishable from an absent daemon, dropping a test from a
  green run.
- **Latch a bare boolean rather than keying by URI.** judgment: the gate's own unit tests vary
  `KDIVE_LIBVIRT_URI`, and `require_issuer()` already keys its latch by JWKS URI for the same
  reason.
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
- **Do nothing.** verified: `rg -ln 'def createXML' tests/` returns nine modules, but they are not
  one population and the count is worth stating precisely. **Six** are storage-volume doubles inside
  this provider — `lifecycle/test_provisioning.py:654`, `lifecycle/test_storage.py:81`,
  `reaping/test_domains.py:325`, `test_boot_artifact_volumes.py:82`, `test_staged_volumes.py:51`,
  `test_volume_upload.py:77` — and those are the ones the children of #2129 migrate. A **seventh**
  storage-volume double sits outside the provider at `tests/diagnostics/test_base_image_staging.py:160`;
  it discards the document entirely (`del xml, flags`), so it does not echo, but it models nothing
  either, and no child of #2129 owns it. The remaining **two** —
  `tests/providers/local_libvirt/lifecycle/rootfs/test_customization_boot.py:86` and
  `tests/testing/test_live_vm.py:227` — are `virConnect.createXML` domain doubles, a different API
  that this issue's defect class does not touch; the `local_libvirt` one is barred from adopting
  this double by ADR-0076 regardless. Against the six, plus a
  `tests/providers/remote_libvirt/fakes.py` that models no storage at all, the class of defect that
  shipped green on #2129's branch is reintroducible today with no red test.
