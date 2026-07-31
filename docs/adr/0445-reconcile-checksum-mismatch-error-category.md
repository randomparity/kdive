# ADR 0445 — A rootfs checksum mismatch is `infrastructure_failure` on every staging path

- **Status:** Accepted
- **Date:** 2026-07-25
- **Amends:** [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) §2, whose streaming
  strip-decode raised a single `configuration_error` for all three of its failure outcomes. The
  transport-checksum mismatch moves to `infrastructure_failure`; the gzip-bomb bound and the
  corrupt/truncated/multi-member stream keep `configuration_error`, and every other ADR-0438
  decision (the ranged single pass, the output cap, the qcow2 magic check, the declaration-time
  cap) is untouched.
- **Leaves standing:** [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) §2. Its
  choice of `infrastructure_failure` for the identity path's recomputed-hash rejection is
  **confirmed, not superseded** — this ADR extends it to the gzip path and records the reasoning
  that §2 asserted without argument.
- **Depends on:** [ADR-0118](0118-wait-on-resource-mechanisms.md) (the derived `retryable` field —
  a pure function of the failure category),
  [ADR-0437](0437-transport-encoding-canonical-object-model.md)
  (the transport-encoding model and the checksum the signed PUT binds),
  [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 (the staging gate order).
- **Spec:** [`../specs/2026-07-25-reconcile-checksum-mismatch-category-1523-design.md`](../specs/2026-07-25-reconcile-checksum-mismatch-category-1523-design.md)

## Context

An uploaded rootfs is staged by one of two paths, chosen by the declared transport encoding. Both
end by comparing a SHA-256 recomputed over the bytes they read against the checksum the signed PUT
bound. Until now they disagreed about what that comparison failing *means*:

- **identity** — `_stage_identity` raised `infrastructure_failure` ⇒ `retryable: true`
  (ADR-0434 §2).
- **gzip** — `strip_gzip_to_writer` raised `configuration_error` ⇒ `retryable: false`.

`mcp/responses.py`'s `_RETRYABLE_BY_CATEGORY` maps the category to an agent-visible `retryable`
boolean, so the codec a System's object happened to be uploaded under decided whether the calling
agent was told to try again. The object is byte-identically as damaged either way. This is a
contract the agent reads, not a job-queue auto-retry policy.

Neither record reconciled the other: ADR-0434 §2 predates the gzip path, and ADR-0438 §2 never
discussed the identity precedent.

## Decision

### 1. A checksum mismatch is `infrastructure_failure` on both paths

The identity path was right, and the gzip path changes.

The decisive fact is that `_decode_error()` was a **uniform helper** covering three outcomes: a
gzip bomb, a corrupt/truncated/multi-member stream, and the end-of-stream hash mismatch. The first
two are defects in the object the agent uploaded — retrying the same key re-reads the same defect,
so `configuration_error` is correct and stays. The hash mismatch is a claim of a different kind,
and it took its category from the helper it sat next to. The gzip path never *chose*
`configuration_error` for a checksum mismatch; it inherited it by proximity. That asymmetry is what
this ADR removes.

The substantive argument for the terminal reading is that `head.checksum_sha256` is the value S3
itself verified at PUT, so a mismatch proves the stored bytes changed *after* the PUT — bit rot or
tampering below the store's own checksum accounting — and no retry of the same key fixes either.
(An API-level overwrite is **not** in that set: it re-records the checksum, so it never reaches
this gate at all. See §6.) That is sound but partial. ADR-0434 §2's own stated rationale for
recomputing at all is that it "catches **transport corruption** and post-PUT bit-rot that the
PUT-time signature alone does not".
Transport corruption on the *GET* is transient, and it is precisely what a bare re-invocation
clears. Two failure modes share one observation, and only one of them is permanent.

ADR-0118 biases terminal when transience is ambiguous, on the grounds that the flag exists to stop
an agent hammering a permanent failure. That bias does not decide this case, because there is no
hammering to stop. **The category buys no automatic re-attempt at all.** Staging runs inside the
provision call, and the provision handler sets `terminal` on any `CategorizedError` before
re-raising it (`jobs/handlers/systems.py`), so the job dead-letters on the *first* attempt under
either category — `queue.py` dead-letters on `terminal or attempt >= max_attempts`. Nothing is
re-downloaded, and no in-flight provision is resumed; the System is already terminally `failed`
and the agent's retry is a fresh provision.

What the category controls is therefore exactly one thing: what the calling **agent is told**.
Under the terminal reading an agent that hit transient GET-side corruption is told the failure is
permanent and is pointed at re-uploading a multi-GiB object that was never wrong. Under the
retryable reading an agent that hit real bit rot re-provisions once, fails the same way, and is
told by the message to re-upload. The second error is cheap and self-correcting; the first is not.

A third precedent already sat on this side: the catalog path (`images/rootfs/fetch.py`) raises
`infrastructure_failure` when downloaded bytes do not match the registered row's digest. With this
change, every *checksum-gate* rejection in the tree reports one category — subject to the reach
limit §6 records, which is narrower than that sentence alone suggests.

The same principle was reached independently in ADR-0450 (#1525) for the staging free-space
precheck: one physical condition should not report two categories depending on which side of a
race window it was observed from. This is that principle across codecs rather than across a race.

### 2. The split is structural, not a category edit

`_decode_error` is replaced by two named constructors, so the category follows from what is being
asserted rather than from what is nearest in the file:

- `_object_error` → `configuration_error` — the uploaded object is defective. Used by the
  bomb-bound guard, the corrupt-deflate branch, the truncated-stream branch, and the
  trailing-data (concatenated/multi-member) branch.
- `_transport_error` → `infrastructure_failure` — the bytes read back are not the bytes signed at
  PUT. Used by the end-of-stream hash comparison, and only there.

A future failure mode added to this utility has to pick one, which is the drift this asymmetry
came from. Note the ordering this preserves: the trailing-data branch is checked *before* the hash
comparison precisely so a multi-member object reports the object defect rather than falling
through to the mismatch branch — that branch's comment already said so, and the split makes the
consequence (terminal, not retryable) explicit rather than incidental.

### 3. Both messages carry the same remediation

The gzip message dropped "re-upload the object (do not retry the same corrupt bytes)", which
directly contradicted the new category, and the identity message was bare
("uploaded rootfs object failed checksum verification"). Both now say the same thing: the stored
bytes do not match the checksum signed at upload; retry, and if it persists the stored object is
damaged and must be re-uploaded. That is the honest rendering of two failure modes behind one
observation, and it is what the agent needs in order to act — the `retryable` flag alone cannot
express "retry once, then re-upload".

### 4. The gzip path annotates on the way out, and the checksum gate is marked

`strip_gzip_to_writer` is consumer-agnostic, while every raise in the uploaded-rootfs fetch carries
`details={"system_id": ...}` — the field `worker.py` lifts into the job row's `failure_context` and
the one an operator pivots on to correlate a staging failure to a System. A gzip failure therefore
landed with only a message where the byte-identical identity failure landed with the id.
`_stage_gzip` annotates on the way out rather than the shared utility growing a consumer-specific
field, which keeps the module's seam intact.

The utility does own one marker, though: a checksum rejection carries
`details["gate"] = TRANSPORT_CHECKSUM_GATE`. A consumer cannot identify that gate from the category,
because `strip_gzip_to_writer` calls `store.get_range` **uncaught** and the store raises its own
`infrastructure_failure` for a connection reset — on a path that issues hundreds of ranged GETs for
a multi-GiB object, by far the likeliest failure there is. Keying the operator log on the category
would have reported every transport blip as stored-object damage, destroying the exact
discrimination the log exists to provide. The marker is a property of the gate, so the utility owns
it; `system_id` is a property of the caller, so the caller attaches it.

### 5. Gate precedence is unchanged

ADR-0441 §5 and ADR-0438 §3 put the checksum comparison ahead of the qcow2-magic gate, so
store-side corruption that also destroys the magic is reported as the checksum failure rather than
as a format failure. That ordering is untouched; only the category the checksum gate reports
changes on the gzip side. What changes downstream is that a **checksum-gate** rejection now
surfaces as retryable on the gzip path, as it already did on identity — for the damage that
actually reaches that gate. Framing-breaking damage still surfaces as `configuration_error` on the
gzip path; §6 measures how much that is.

### 6. The convergence's reach is narrower than §1 alone reads

On the gzip path the hash comparison is the **last** gate, and zlib's own framing trips first, so
damaged stored bytes usually never reach it. An exhaustive single-bit sweep of the deflate body of
the residual test's fixture, with a correct signed checksum, lands on three different branches:

| branch reached | share | category |
|---|---|---|
| corrupt deflate stream (`zlib.error`) | 225/248 | `configuration_error` |
| **gzip-bomb bound** (`_drain`'s output cap) | 13/248 | `configuration_error` |
| transport checksum mismatch | 10/248 | `infrastructure_failure` |

The exact split is a property of the object's content, not a constant — but the shape holds: the
digest is reached by a small minority. Trailer CRC/ISIZE rot and post-PUT truncation likewise
report `configuration_error`. The identity path, having no framing to trip, reports every one of
these as `infrastructure_failure`. So the codec still decides the verdict for most damage. §1 is
the decision this ADR makes and is correct as far as the gate order lets it reach; it is **not**
yet true that an agent gets the same advice regardless of codec for arbitrary damage.

What *does* reach the digest is damage that leaves the decoded stream and its framing intact: gzip
header fields (MTIME/XFL/OS) and deflate padding bits after the final end-of-block code (the
10/248 above — a flip there decodes byte-identically, so only the digest catches it).

An **out-of-band overwrite does not generally reach this gate at all**, on either path, and §1's
listing of it alongside bit rot conflates two mechanisms. The checksum compared here is not the
declared content address: `_stage_uploaded_object` re-`HEAD`s at provision time and passes
`head.checksum_sha256` down, which the store reads off the *live* object. An actor with bucket
credentials who re-`PUT`s a different object to the key updates that metadata too, so the new bytes
match the new object's own checksum and the gate passes. (The declared-vs-stored comparison exists
only at commit time, in `complete_rootfs_upload`.) What converges is a *storage-layer* substitution
that edits the backing bytes while leaving S3's recorded checksum stale — which is the same
mechanism as bit rot, not a distinct one. Closing the overwrite hole properly means comparing the
declared content address at stage time, which is a new gate rather than a category question and is
not attempted here.

The **bomb branch is the worst of the three**, and not only for its category. Its message reads
"the object is not a valid gzip of that size (a gzip bomb or a wrong `uncompressed_size`);
re-declare with the correct `uncompressed_size`" — affirmatively wrong advice when the declaration
was right and the stored bytes rotted. It also compounds: ADR-0450 makes `uncompressed_size` the
gzip path's free-space budget, so an agent that follows the advice and re-declares upward can have
its next provision refused by the free-space precheck for a base the volume can hold. §3's whole
bar is that the message says the right thing, and this is the one branch where it does not.

Closing the residual means consulting the transport hash *before* declaring an object defect —
those branches assert "the uploaded object is defective", a claim that is unfounded when the bytes
read back are not the bytes signed at PUT. That follows from §2's own principle, and on the
truncated branch it costs nothing (the hasher has already absorbed every stored byte); the
`zlib.error` and bomb branches need the remaining ranges drained hash-only first. It is out of
scope here only because it changes the bomb and corrupt/truncated branches' categories in a subset
of cases, which #1523's brief ruled out. **#1548** carries it, and
`test_stage_checksum_mismatch_on_gzip_corrupt_bytes_is_a_known_residual` pins both the `zlib.error`
and bomb shapes so the gap is visible rather than silent.

### 7. §6's residual is closed by ADR-0523 (2026-07-30)

Appended after the fact, so §6 above reads as the state of the tree between 2026-07-25 and
2026-07-30 rather than as current behaviour.

[ADR-0523](0523-transport-hash-precedes-the-gzip-object-defect-verdict.md) (#1548) does what §6's
closing paragraph sketched: `strip_gzip_to_writer` now compares the transport digest **before** any
object-defect branch raises. The three-way table in §6 collapses — every shape of stored-byte
damage reaches the digest and reports `infrastructure_failure`, matching the identity path, and an
object-defect category is only reported once the stored bytes have been proven to be the bytes
signed at PUT. The bomb branch no longer blames a correct `uncompressed_size`, so the ADR-0450
compounding §6 names is closed for the rotted-bytes case.

The reach caveat in the Consequences below moves with it, but only as far as the gate goes. The
gzip path's stored-object-damage WARNING is keyed on the gate, and the gate moved, so it now fires
for the widened set with no change to `_stage_gzip`, and the two paths log the same damage. It does
**not** follow that absence of the line proves an intact object — that inference was unsound before
and stays unsound, on both paths, because staging can exit before a digest exists at all: a
reusable staged base short-circuits before any fetch, the free-space precheck and the
missing-checksum branch raise before the first read, and a `get_range` fault aborts mid-object.
Silence means the digest agreed *or* verification never ran. What changed is that the gzip path no
longer adds a fourth case in which the object **was** read through, **was** damaged, and logged
nothing anyway.

§1–§5 are unaffected. ADR-0523 changes when the object-defect constructor is reachable, not the
split §2 made or the categories §1 assigned; the residual test §6 names is retired there, its flip
probe surviving inverted.

## Consequences

- An agent that hits a rootfs checksum mismatch on a gzip-encoded upload is now told
  `retryable: true`, and gets the same advice it would have got identity-encoded **for damage that
  leaves the decoded stream and its framing intact** (§6).
- No re-download is added. The provision handler marks a staging `CategorizedError` `terminal`, so
  the job dead-letters on the first attempt under either category; the change is to what the agent
  is told, not to how many times anything is fetched. `max_attempts` is untouched.
- The checksum branch now increments the `infrastructure_failure` bucket of
  `telemetry.record_job_failure(category)` rather than `configuration_error` — this tree's
  catch-all for every store, libvirt, disk and capacity fault. Since the category no longer
  distinguishes stored-object damage from routine infra noise, both staging paths now emit a
  `WARNING` naming the object key, System and encoding; that log, not the category, is what lets an
  operator see the bit-rot mode §1 names. Same reasoning as `_require_staging_free_space`'s warning
  in the same module, and it matters more here because the agent is *told* to retry, so without it
  the first observable consequence of real damage would be a silent extra multi-GiB download. The
  log is keyed on the *gate*, not the category — the store's own faults from `get_range` propagate
  through the decode utility as `infrastructure_failure` too, and a connection reset on one of the
  hundreds of ranged GETs a multi-GiB stage issues must not be reported as stored-object damage.
  **On the gzip path the log inherits §6's reach exactly**: framing-first damage raises an
  object-defect error and logs nothing, so absence of the line is not evidence of an intact object
  for a gzip upload. #1548 widens the log along with the category.
- A gzip staging failure now carries `system_id` in its `failure_context`, as the identity path
  already did (§4).
- No schema change, no migration, no MCP tool-surface change. The only externally visible changes
  are the `retryable` boolean, the message text, and the added detail field on one failure path.
- `artifacts/transport_encoding.py` is a shared module, but `strip_gzip_to_writer` has exactly one
  production consumer (the local-libvirt uploaded-rootfs fetch); the upload-declaration validator
  imports only the codec constants. No other caller depended on the old category.

## Considered & rejected

- **Converge the identity path onto `configuration_error` instead.** Would overturn ADR-0434 §2
  and diverge from the catalog precedent, and is wrong on the transport-corruption half of the
  failure modes.
- **A distinct category for object-store integrity failures.** `TRANSPORT_FAILURE` exists and is
  retryable, but it is the SSH/console transport's category throughout the tree; borrowing it here
  would blur an established vocabulary to gain nothing over `infrastructure_failure`, which both
  the identity and catalog precedents already use for this exact check.
- **Leave the disagreement and document it.** The issue asked for a decision precisely because the
  test pinning the identity side had to disclaim that it was pinning one side of an open question.
