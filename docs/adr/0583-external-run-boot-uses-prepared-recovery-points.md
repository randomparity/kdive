# 0583 — External Run boot uses prepared recovery points

## Status

Proposed

## Context

`InstallRequest` carries a combined kernel/modules bundle, an optional initrd, a composed command
line, and immutable object versions into every provider. The providers currently give those inputs
different meanings. Local-libvirt extracts `boot/vmlinuz`, injects modules, stages an optional
initrd, and writes direct-kernel domain XML. Remote-libvirt downloads the bundle inside the guest,
regenerates an initrd, and selects a GRUB entry. That difference prevents a finalized external
build's kernel/initrd pair from naming one portable Run boot.

The remote System must continue to provision and recover through its disk image and GRUB, while an
iterative Run may direct-kernel boot. Switching the domain definition is a durable state change: a
worker can die after changing the provider but before recording success. Recovery therefore needs
an exact pre-activation state and a reconciliation rule. The shared contract cannot carry libvirt
XML, storage-volume names, host paths, presigned URLs, or network-boot concepts because later
providers do not share them.

PR #2104 proposed a standalone System-profile INITRD input and permanent remote rejection. That PR
closed without merging and its decision is not part of the repository. This decision replaces
that unpublished direction: external Run builds supply an optional initrd paired with their kernel
bundle; System-baseline INITRD input remains absent.

## Decision

External Run boot is split into three provider-neutral operations: materialize a validated immutable
`ExternalBootPlan`, prepare a recovery point, and activate the materialization. Cleanup is a fourth
idempotent operation. Shared values contain only immutable artifact identities, architecture,
kernel release, the complete ordered kernel argument set, a versioned root specification, a module
installation obligation, and opaque provider references returned by those operations.

For external boot, this ADR refines ADR-0061 and ADR-0183: core performs their platform-owned
composition once when it creates the plan, and a provider renders the resulting `cmdline` without
prepending, appending, inheriting, or shell-parsing anything. Their existing build/install and GRUB
paths are unchanged.

The boot plan is one immutable set. Its exact envelope is `external-boot-plan-v1`, serialized with
the manifest JSON rules and hashed after ASCII `kdive-external-boot-plan-v1` plus NUL. It has exactly
these keys and shapes: `schema`; `architecture`; `ownership` with canonical lowercase hyphenated UUID
strings `run_id` and `build_id`; `bundle` with NFC object-store `key` and `version` plus `sha256`;
`initrd`, either null or the same key/version/digest shape; complete `cmdline` string;
`debug_cmdline`, null or the preserved caller extra; ordered `platform_arguments`; `module_obligation`
with `mode`, `release`, and `source_manifest`; and the closed `root` shape defined below. Unknown keys are
rejected. An initrd is valid only as part of this set; it has no independent activation identity.
Materialization must extract
`boot/vmlinuz` from the combined bundle, validate its architecture and release against the plan,
compute the extracted bytes' SHA-256 digest, and satisfy the plan's module-install obligation. The
provider fetches the exact recorded object versions and stream-verifies the complete bundle and
optional initrd bytes against their plan digests before extraction, publication, or reuse. The
compressed bundle is never itself a bootable kernel.

Successful materialization produces an immutable `external-boot-materialization-v1` record. It uses
the same serializer and ASCII domain prefix `kdive-external-boot-materialization-v1` plus NUL. It has
exactly: `schema`; `architecture`; NFC `provider_kind`; `ownership` with canonical UUID `system_id`
and `run_id`; `plan_identity`; `extracted_vmlinuz_sha256`; `source_module_manifest`;
`installed_module_tree`; `verified_bundle_sha256`; `verified_initrd_sha256`, null exactly when the
plan initrd is null; `kernel_observation` with architecture, release, and lowercase even-length
GNU `build_id` hex; and `artifacts`, whose `kernel` and `modules` each contain one deterministic
NFC opaque `ref` and whose `initrd` is null or contains one such `ref`. Core persists the complete
record, not only the references. Repeated materialization for a plan must reproduce every field; an
absent, unreadable, or different field fails closed and is neither reused nor activated. Observation
of a deterministic reference re-hashes its stored bytes; a same reference with different bytes is a
conflict, not a cache hit.

The materializer also extracts the kernel's architecture, release, and GNU build ID from the
extracted vmlinuz and binds that tuple to its byte digest in the materialization record. Missing or
ambiguous build notes reject external boot. The provider-neutral running-kernel observation returns
architecture, `uname` release, and the GNU build ID from the running kernel's notes. Local observes
it through its existing guest connection, remote through its fixed guest-agent helper, and the
non-libvirt test provider returns the same value type. Unavailable evidence is retryable before the
readiness deadline; a mismatch records terminal-on-this-attempt `READINESS_FAILURE` and triggers
recovery. Readiness, boot ID, and
persistent definition identity cannot substitute for this comparison.

When core commits `activating`, it also persists `server_time` and an absolute UTC RFC 3339
`activation_readiness_deadline`, computed once from operator-configured
`activation_readiness_timeout_seconds`. Unit is seconds and scope is this System/Run activation.
Every worker retry reuses that deadline. Unavailable running-kernel evidence or readiness before it
is retryable; reaching it without both proofs records terminal-on-this-attempt `BOOT_TIMEOUT`,
transitions to `recovering`,
and suggests `jobs.wait` and `runs.get` while recovery runs.

The first `recovering` commit likewise persists `server_time` and an absolute
`recovery_readiness_deadline` from `recovery_readiness_timeout_seconds`, scoped to this recovery and
never extended by worker retry. Failure to restore and reach fresh readiness by that instant records
`READINESS_FAILURE`, transitions to `recovery_failed`, retains the recovery evidence and reservation,
and suggests the administrator call `systems.teardown`. System teardown is the only exit from
`recovery_failed`.

An administrator resolving `recovery_conflict -> recovering` starts a new recovery attempt and
records a new deadline from that operation's `server_time`; time parked in conflict consumes no
recovery-attempt window. That explicit resolution is the only operation that may replace an expired
or interrupted recovery deadline. Ordinary job and worker retries never extend one.

These normative all-zero vectors also pin every key and absence representation:

```json
{"architecture":"x86_64","bundle":{"key":"bundles/k.tar","sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","version":"v1"},"cmdline":"root=UUID=x","debug_cmdline":null,"initrd":null,"module_obligation":{"mode":"system-root-tree","release":"6.1.0","source_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"},"ownership":{"build_id":"00000000-0000-0000-0000-000000000001","run_id":"00000000-0000-0000-0000-000000000002"},"platform_arguments":["root=UUID=x"],"root":{"architecture":"x86_64","arguments":["root=UUID=x"],"authority":"build","root":"UUID=x","schema":"root-spec-v1","source":{"identity":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kind":"build"}},"schema":"external-boot-plan-v1"}
```

```json
{"architecture":"x86_64","artifacts":{"initrd":null,"kernel":{"ref":"kernel/ref"},"modules":{"ref":"modules/ref"}},"extracted_vmlinuz_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","installed_module_tree":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kernel_observation":{"architecture":"x86_64","build_id":"0000000000000000000000000000000000000000","release":"6.1.0"},"ownership":{"run_id":"00000000-0000-0000-0000-000000000002","system_id":"00000000-0000-0000-0000-000000000003"},"plan_identity":"sha256:0e8a1930e670dc87302d13bc07463ecd2805a45b6c5e3eb9bbe81575dd344b3b","provider_kind":"local-libvirt","schema":"external-boot-materialization-v1","source_module_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_bundle_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_initrd_sha256":null}
```

Their respective identities are
`sha256:0e8a1930e670dc87302d13bc07463ecd2805a45b6c5e3eb9bbe81575dd344b3b` and
`sha256:03d69bb7ddc381795419f76e77c26355a683b8a0832b7bcd89cb54aaa77ec6ab`.

The version-1 module obligation has one mode: `system-root-tree`. It names the kernel release and a
`module-source-manifest-v1` digest of the bundle's exact `lib/modules/<release>/` subtree. The
release is 1 through 64 ASCII bytes matching `[A-Za-z0-9][A-Za-z0-9._+-]{0,63}` and is neither `.`
nor `..`. The extracted kernel release must match it byte-for-byte. Providers select the directory
with a no-follow directory-relative lookup; separators, controls, NUL, and every noncanonical value
are rejected before a manifest walk or write. The
manifest applies one shared extraction normalization first: absolute symlinks named exactly
`build` or `source` at the release root are omitted, while every other absolute or escaping link is
rejected. This exact-name rule intentionally tightens the current local filter, which omits every
absolute symlink: #2107 must move local and remote materialization onto the shared validator before
either computes this manifest. Validation covers the complete topology before any write: every
ancestor of an entry must be a declared or implicit directory, no entry path may traverse a symlink,
and no two entries may resolve to the same destination. Extraction uses no-follow, directory-relative
operations so archive order cannot change the result. A path is one or more nonempty UTF-8 NFC POSIX
segments separated by exactly one `/`; leading or trailing `/`, repeated separators, `.`, `..`, NUL,
and empty segments are rejected rather than rewritten. Symlink targets use the same grammar after
lexically resolving their relative segments against the link's parent, and must remain beneath the
release root. The manifest then sorts paths by encoded bytes; rejects
duplicates, hard links, devices, sockets, and FIFOs; and admits only directories, regular files, and
contained relative symlinks. Each entry records normalized path, type, normalized permission bits,
and regular-file size/SHA-256 or symlink target; uid, gid, and timestamps are excluded.
Materialization validates that source manifest, stages the tree, runs required indexing, and computes
`installed-module-tree-v1` with the same walker over the final tree. Generated indexes such as
`modules.dep` belong to installed identity, not source identity. The provider returns the installed
digest, and target state identity binds it.

Both manifest hashes have exact bytes. The envelope is compact JSON with UTF-8 NFC strings, keys
sorted by Unicode code point, arrays in the path order above, JSON integers for sizes and uid/gid,
four-character lowercase octal strings for modes, lowercase `sha256:<hex>` content digests, and no
insignificant whitespace or trailing newline. The serializer emits every non-control Unicode scalar
as literal UTF-8 except `"` and `\`, which use `\"` and `\\`; emits solidus literally; uses the
two-character escapes `\b`, `\t`, `\n`, `\f`, and `\r`; and emits every other U+0000 through U+001F
control as lowercase `\u00xx`. Surrogates and non-scalar input are rejected. Paths and symlink targets that
are not already NFC are rejected rather than normalized into another filename. The source envelope
is `{"entries":[...],"schema":"module-source-manifest-v1"}` and its hash input is the ASCII
prefix `kdive-module-source-manifest-v1` plus NUL plus the JSON bytes. The installed envelope uses
schema/prefix `module-installed-tree-v1` / `kdive-module-installed-tree-v1` and additionally records
uid, gid, `xattrs_supported`, and an xattrs object whose sorted names map to unpadded base64 values.
An empty tree is an empty `entries` array, not absent input.

A non-ASCII source-manifest vector is
`{"entries":[{"mode":"0644","path":"kernel/café.ko","sha256":"sha256:` followed by 64 zero
hex digits, then `","size":1,"type":"file"}],"schema":"module-source-manifest-v1"}`. Its
domain-separated digest is
`sha256:6fbb113f8a57314352634354b53e9270dfe141984226a3bf22bef7d7de95e2cf`.

Source entries have exactly one of these shapes (shown in their sorted-key byte order):

```json
{"mode":"0755","path":"kernel","type":"dir"}
{"mode":"0644","path":"kernel/a.ko","sha256":"sha256:<64-hex>","size":1,"type":"file"}
{"mode":"0777","path":"weak-updates/a.ko","target":"../kernel/a.ko","type":"symlink"}
```

Installed entries add `gid`, `uid`, `xattrs`, and `xattrs_supported` to the corresponding source
shape; those keys are per entry. `xattrs` is always an object and is empty when support is false.
Xattr names must be UTF-8 NFC or observation is a third state; values are unpadded standard base64.
No displayed key is optional, no other key is admitted, and `size` is any non-negative JSON integer.

Before staging, providers normalize source metadata: uid/gid become `0`, directories `0755`, regular
files `0755` when any execute bit was present and `0644` otherwise, and symlinks `0777`; ACL and
xattr inputs are discarded. After publication the provider applies its configured filesystem label
policy, then computes installed identity from lstat ownership/mode and every resulting xattr,
including POSIX ACL and `security.*` values; absence and unsupported-xattr filesystems are distinct
recorded states through `xattrs_supported`. Recovery copies preserve those installed fields and
verifies the same installed manifest after restore. Thus portable source identity ignores host
metadata while provider-state CAS
detects metadata drift that can change module usability.

The prior tree is instead captured as `recovery-module-tree-v1`, with hash prefix
`kdive-recovery-module-tree-v1`. It uses the installed entry metadata and path grammar, but records
each symlink's lstat target verbatim as UTF-8 NFC and permits absolute targets; it never follows a
symlink while hashing, copying, or restoring. This provider-state identity is not portable input and
does not relax source validation. A prior tree containing a hard link, special file, undecodable
name/target, or noncanonical path is ineligible for replacement and fails before publication. The
recovery copy is created and restored with no-follow directory-relative operations, then verified
against this manifest. Conventional release-root `build` and `source` absolute symlinks are therefore
represented exactly rather than omitted from recovery CAS.

Materialization does not change the System. Core first commits `preparing` with reservation state
`pending`; the provider then creates the reservation and core records it `ready`. No quiesce or other
guest mutation is allowed before `ready`. A deterministic provider prepare journal records the source definition and prior power state,
stops the domain, verifies it inactive, and only then records whether the release-qualified target is
absent or saves its exact prior tree. The completed journal becomes the opaque recovery reference and
allows core to commit `prepared`. A worker loss in `preparing` is resumed from that journal; abandoning
it restores the source definition and prior running state before removing staged data. Activation
from `prepared` publishes the staged tree at `/lib/modules/<release>`, applies the target definition,
and boots it. Failure to quiesce leaves `preparing` and mutates neither tree nor definition. An exact
existing tree may be reused; a different tree for the same release is replaced, not rejected. Recovery restores
the saved tree or removes the new tree when the target was previously absent. This preserves
same-release kernel iteration and prevents either running kernel from observing the other's modules.
A provider unable to quiesce, stage, replace, verify, and restore the tree rejects before recovery
preparation. “Preserve the disk overlay” below means keep the same attached overlay and device
definition; it does not promise that the guest filesystem is byte-immutable. The optional initrd
never substitutes for this obligation.

After the pending row exists, the provider creates a deterministic reservation owned by this
System/Run/plan for exactly operator-configured `recovery_reserve_bytes`. The sum of retained
reservations in one provider instance's recovery store cannot exceed its
`recovery_max_bytes`; both values are byte counts, and availability is observed at the response
envelope's `server_time`. Each store has a durable identity; compare-and-debit, idempotent lookup,
and release serialize under a store-scoped advisory lock, independently of the System lock. The debit
and reservation row commit atomically, and the deterministic ownership key makes retries find the
same debit. Release deletes that row and credits its bytes exactly once. Reconciliation releases a reservation only when its
durable activation row is terminal or absent; because the row precedes allocation, a live allocation
cannot be mistaken for an orphan. A crash before `ready` resumes allocation or abandons the pending
row without guest mutation. Exhaustion is retryable `CAPACITY_EXHAUSTED`, changes no
guest state, and directs the operator to clean terminal artifacts or raise the cap before retry.

The fixed reservation bounds the captured definition, prior module tree, journal, and verification
metadata together. Offline capture cannot exceed it. An overrun restores the source definition,
exact prior module tree, and recorded prior power state; a previously stopped System remains stopped.
After verification it commits `abandoned`, releases the reservation, and reports
`INSTALL_FAILURE` with the required minimum observed bytes so the operator can raise
`recovery_reserve_bytes`. Prepared, recovery, and conflict states retain the reservation;
abandonment, recovery, and System teardown release it idempotently.

`platform_arguments` contains 1 through 32 nonempty ASCII tokens, each at most 256 bytes and without
ASCII whitespace or NUL; their one-space join is at most 4096 bytes. Exactly one starts `root=`;
`root.arguments` is a nonempty ordered array whose complete sequence occurs
exactly once as a contiguous subsequence of `platform_arguments`. No other platform element may use a
key present in `root.arguments`. Specifically, the sole root token equals ASCII `root=` concatenated
with `root.root`. A token's key is the nonempty bytes before its first `=`, or the entire token when
`=` is absent; duplicate-key checks compare those bytes, and “other” excludes the one validated
`root.arguments` occurrence.

`debug_cmdline` preserves the current caller contract: core strips surrounding characters with
Python 3.14 `str.strip`, then accepts 1 through 4096 printable Unicode scalar values, including
non-ASCII, internal whitespace, quotes, and backslashes; NUL and non-printable values remain rejected.
It is null when no caller extra exists. This field and the derived `cmdline` are exempt from the
serializer's NFC-input rule so the accepted scalar sequence is not rewritten. Core builds `cmdline` exactly as the one-space join of
`platform_arguments`, followed by one space and `debug_cmdline` when non-null. The final UTF-8 encoding
is bounded to 20,480 bytes. Providers pass this string directly to libvirt or a fixed argv parameter;
they do not tokenize, quote, normalize, or invoke a shell. This makes the provenance and rendered
direct-kernel bytes one composition result while preserving currently admitted local inputs.

The root specification is a versioned, closed data shape with exactly `schema`, `architecture`,
`root`, `arguments`, `authority`, and `source`. Version 1 admits authority/source-kind pairs
`build/build`, `stage-inspection/staged-image`, and `catalog-attestation/catalog-image`; no other pair
is valid. `source` has exactly `kind` and an immutable lowercase `sha256:<64-hex>` `identity` over,
respectively, the build output, inspected staged-image version, or catalog image version. Build facts
and bounded stage inspection are verified authorities. Catalog data extends the existing typed
attestation path with the same root value, ordered arguments, architecture, schema version, and
immutable image identity; the current attestation fields alone are insufficient. No second untyped
declaration path is added.
Unknown versions, missing facts, stale source identities, conflicting root arguments, and an
architecture mismatch fail before materialization or activation and name the recovery action. A
pre-schema image remains eligible for its existing GRUB boot but not external Run boot.

Before changing boot state, the provider prepares both sides of the compare-and-set. It creates a
durable recovery point representing the exact current persistent boot configuration and prior module
tree, renders but does not apply the target configuration, and returns provider-computed source- and
target-state identities plus an opaque recovery reference. A state identity covers both the boot
definition and the release-qualified module-tree identity. Libvirt definition identity is a
versioned two-part comparison over persistent/inactive XML. The **preserved digest** canonicalizes
the entire inactive definition after removing only the provider-owned external-boot fields
`/domain/os/kernel`, `/domain/os/initrd`, and `/domain/os/cmdline`; no other subtree, attribute,
alias, address, device, firmware field, backing/auth/encryption field, or QEMU argument is excluded.
The **boot projection** is canonical JSON for those three fields, distinguishing absence from an
empty value. Safe defused parsing forbids DTDs and entities. Before canonicalization, whitespace-only
`.text` on elements with children and every whitespace-only `.tail` are removed; other character data
is unchanged and must already be NFC. The result is W3C Canonical XML 2.0 as implemented by Python
3.14 `xml.etree.ElementTree.canonicalize`, with `with_comments=False`, `strip_text=False`, and
`rewrite_prefixes=True`, encoded UTF-8 without a trailing newline. The preserved hash input is ASCII
`kdive-libvirt-preserved-v1`, NUL, then those bytes. Preparation reads the source inactive XML,
computes its preserved digest, clones it, changes only the three boot fields, and computes the target
boot projection. Observation repeats the same split. A changed preserved digest is always a third
state; source or target requires both the shared preserved digest and the matching boot projection.
Live XML is never an identity input. Remote's recovery point stores the exact inactive disk/GRUB
definition behind the provider seam, so shared state never interprets its XML. Deterministic
identifiers make repeated prepare calls for the same System, Run, plan identity, and source state
return the same point and target identity.

The boot projection is compact sorted-key UTF-8 JSON with exact shape
`{"cmdline":null|string,"initrd":null|string,"kernel":null|string,"schema":"libvirt-boot-projection-v1"}`
and the manifest JSON rules. Its hash prefix is `kdive-libvirt-boot-projection-v1` plus NUL. Golden
vector: `<domain><os><type arch="x86_64">hvm</type></os></domain>` preserves to
`<n0:domain xmlns:n0=""><n0:os><n0:type arch="x86_64">hvm</n0:type></n0:os></n0:domain>`;
its digest is `sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea`.
The all-null boot projection digest is
`sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925`.
A non-ASCII projection
`{"cmdline":"root=LABEL=café","initrd":null,"kernel":"/var/lib/kdive/café","schema":"libvirt-boot-projection-v1"}`
has digest `sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb`.

Remote preparation also proves that the source is an owned disk/GRUB baseline. Its inactive boot
projection has no kernel, initrd, or cmdline; KDIVE metadata binds it to this System; its sole boot
disk uses the System's deterministic overlay volume, expected pool, target and bus; disk boot is the
only `<os><boot>` selection; and loader, firmware, and NVRAM fields match the remote provisioning
profile. A source carrying external-boot fields is admissible only while a matching durable
activation row owns it; that row must recover under the System lock before another prepare. Any
other mismatch or unowned external definition enters `recovery_conflict` and is never captured as a
new source point.

Core persists the plan identity and materialization before prepare, then the recovery reference and
both provider state identities when preparation completes. The state machine is
`preparing -> prepared -> activating -> active`, with
`activating|active -> recovering -> recovered`,
`recovering -> recovery_failed`,
`preparing|prepared -> abandoned`,
`preparing|prepared|activating|active|recovering -> recovery_conflict`, and
`recovery_conflict -> recovering`,
failure metadata on an operation attempt.
Core permits at most one external-boot activation that is not `recovered` or `abandoned` per System.
A partial unique database index enforces that invariant across `preparing`, `prepared`, `activating`,
`active`, `recovering`, `recovery_conflict`, and `recovery_failed`; all providers use the same core
admission path. A second Run receives `CONFLICT` before reservation or provider work, with the existing
activation identity and state plus `runs.get`; an active activation also suggests
`runs.release_external_boot`, while a failed/conflicted activation suggests `systems.teardown` when
its authorized recovery action is unavailable. No provider may capture another Run's external target
as a source baseline, and external activations never form a rollback stack. Adversarial admission
tests race two Runs against one System through local, remote, and non-libvirt runtimes.
Each transition is committed under the existing per-System advisory lock; provider calls use the
cross-transaction epoch fence defined below. Activation is
compare-and-set from the recovery point's source-state identity: the provider refuses changed state
or materialization/recovery references belonging to another System, Run, or plan. On an `activating`
record after worker loss, reconciliation compares the persistent definition and module-tree
identities with both recorded states. A complete target state resumes boot, fresh readiness, and
running-kernel identity proof before core may commit `active`; persistent equality alone never does.
A complete source state resumes restoration of the recorded prior power state and, when that state
was running, fresh baseline readiness before core may commit `recovered`. When it was stopped,
verified inactive source state is the recovered condition. A mixed state whose every component equals its recorded source
or target component may be restored to source only when the activation write-ahead journal recorded
the expected identity before each target write and its result afterward, proving every target-valued
component belongs to this activation. An unproven mixture enters `recovery_conflict`. Any absent,
unreadable, or third component identity enters `recovery_conflict` for operator resolution instead
of overwriting provider state. The portable plan identity is never compared directly with provider
definition bytes. Runtime readiness and running-kernel identity are separate observations and never
decide which persistent definition won.

The existing transaction-scoped System advisory lock protects each database transition only; it
cannot fence provider work across those commits. The System owns a durable monotonically increasing
`operation_generation` allocated under that lock; it never resets when an activation terminates.
Each new activation and every takeover increments the System value and copies the resulting generation
plus activation identity into the activation row. Recovery points, staged artifacts, maintenance
domains, and externally visible target components bind only the stable
System/activation/Run/plan identity and their content identities; takeover never retags or recreates
them. The provider-durable per-System fence and each append-only mutation-journal entry additionally
record the actor generation and claimant token.

Every live execution attempt allocates a fresh System generation and a cryptographically random
canonical UUID claimant token under the database System lock before provider work. The token belongs
to that in-memory execution only and is never copied into a job payload for another worker. An actor
atomically claims `(activation identity, generation, claimant token)` in the provider fence.
Idempotence requires the same triple and is permitted only for retries within that live execution;
any replacement, redelivery, reconciliation pass, or restarted process allocates a higher generation
and new token. Under the provider-local System lock, takeover validates the stable ownership and content
digests of every existing recovery object, then validates the mutation journal as an unbroken sequence
ending at the prior claimed generation before compare-and-setting the fence to the new generation.
Existing journal entries remain immutable; new work appends with the new generation and token. A crash
after the database increment but before the provider claim abandons that unused generation; the next
execution allocates a higher one rather than sharing its authority. Missing, changed, or
unowned evidence enters `recovery_conflict` and is never recaptured from the current System state.

Replacement workers, reconciliation, conflict resolution, teardown, and later Runs all allocate
before claiming. Provider-local serialization makes a new claim wait for any bounded
publication critical section; long downloads, extraction, and helper execution write only private
staging and are cancelable or discardable.

Immediately before every definition, module-tree, attachment, power, or cleanup mutation, and again
before recording its result, the provider holds that local fence and rejects a generation other than the
current claimed value. A stale actor can finish private work but cannot publish, boot, restore, or
delete after takeover. Teardown claims the newest generation before destroying anything. Connection loss
does not release authority to an older generation; only a durable higher claim supersedes it. Tests pause
an old actor before publication and boot, claim a replacement or teardown generation, and prove the stale
write is refused.
The stale-actor suite also completes one activation, starts a later Run with a greater generation,
and proves that an actor from the earlier Run can never reclaim authority.
It separately loses a worker after `prepared` and after the first target-component write, claims the
next generation, consumes the original activation-owned evidence, and proves safe resume without
retagging or source recapture.
The suite also pauses an actor before its first claim, lets another execution allocate and claim the
next generation, resumes the old actor, and proves its different token/generation cannot mutate state.

`recovery_conflict` has two resolutions only. A project administrator may invoke the audited
`resolve_external_boot_conflict` operation with `restore-recorded-source` and the exact currently
observed composite state identity. Under the System lock, the provider repeats that observation,
compare-and-sets every component from it, records the authority and before/after identities, and
transitions to `recovering`; a changed or unreadable value leaves the conflict untouched. The normal
recovery journal and readiness gates then apply. Alternatively, ordinary authorized System teardown
destroys the domain before releasing the reservation and artifacts. There is no adopt-current-state,
force-overwrite, or automatic timeout edge, because a mixed external definition cannot become a
trusted reusable baseline by declaration. Reconciliation preserves evidence and performs no provider
write while the state remains `recovery_conflict`.

Recovery restores a usable disk/GRUB baseline, not only persistent bytes. Run build state is not its
usage lease: current Runs are already `succeeded` before install and boot. A new contributor operation,
`runs.release_external_boot`, is the explicit end-of-use event for an active external-boot activation.
Under the System lock it refuses while that Run has a live DebugSession or another active lifecycle,
capture, or debug job, then atomically records the release request and enqueues recovery. Repeating it
is idempotent and returns the same recovery job. `debug_sessions.detach` does not release implicitly;
its response suggests `runs.release_external_boot` when it detached the last live session. Authorized
System teardown bypasses release because destruction owns cleanup.

On release, the provider first observes the complete recorded target state under the System lock.
Only that state may take the normal `active -> recovering` edge. An
absent or unreadable component remains retryable; any third component enters `recovery_conflict`
without stopping or overwriting the System. After committing `recovering`, the provider stops the
domain through the control plane and verifies it is inactive. It then re-observes the complete target
state immediately before the first restore write. An unreadable or third component at that point
enters `recovery_conflict` without an overwrite. The provider then restores the prior module tree and persistent
definition, boots that definition, and requires a fresh boot plus the existing System readiness
contract before committing `recovered`. The exact GRUB-selected kernel is guest bootloader state and
is not knowable from an inactive domain definition, so it is not an identity gate; a recovery that
reaches its persisted recovery deadline without readiness transitions to `recovery_failed` and
retains all evidence; it does not remain `recovering`. A recovery retry before that deadline
re-observes each component: complete target resumes restoration,
complete source resumes boot/readiness, and a source/target mixture may resume only when every
component matches one of those recorded identities and the journal proves the partial write belongs
to this recovery. Before each subsequent write, that journal records the expected component identity;
the write uses compare-and-set from that value and records its result. Any other mixture or third
value enters `recovery_conflict`. Concurrent terminalization
serializes under the System lock. The recovery point and materialized artifacts cannot be deleted
before `recovered`. System teardown instead destroys the domain before cleaning the recovery point
and materialization, because a definition that will be destroyed need not be restored or rebooted.
Artifacts remain while the Run can retry, are deleted idempotently on those ordered paths, and are
swept by deterministic ownership after worker death. A partial materialization is either atomically
published under its final identity or discoverable as an owned partial and removed; it is never
activated.

`preparing|prepared -> abandoned` is the pre-activation disposal path. From `preparing`, the provider
journal first restores the captured source definition and prior power state; from `prepared`, provider
observation must still equal recorded source state. Run terminalization or reconciliation may take
either edge only after retries are no longer possible. Cleanup then removes the journal or recovery
point and materialization and commits `abandoned`. A target or third identity fails closed as
`recovery_conflict`; absence or an unreadable identity remains retryable. The same per-System lock
serializes abandonment with activation and teardown.

Local-libvirt adapts its existing staging and direct-kernel XML behavior behind these operations.
Remote-libvirt uploads per-System/per-Run kernel and optional initrd artifacts and resolves
provider-local paths internally. It cannot mount the remote overlay from the worker, and its existing
guest-agent seam exists only while the System guest runs, so it gains one provider-private offline
disk-editor operation. After stopping the System domain, the provider attaches that same overlay,
plus one deterministic reservation-backed recovery volume, to a transient maintenance domain on the
remote libvirt host; no other disk is writable. The recovery volume capacity equals the activation's
remaining reserved bytes and is never attached to the System domain. The maintenance domain boots an operator-staged, architecture-matched immutable appliance,
has no network interface, and exposes only a guest-agent command whose fixed helper can capture,
stage, atomically publish, verify, or restore the release-qualified module tree. The helper receives
content through a provider-owned read-only libvirt volume, never a presigned URL or caller-composed
command. Capture writes a no-follow recovery archive and manifest to the recovery volume, hashes it
incrementally against the reservation, fsyncs its data, and atomically writes a committed footer
containing its length and digest. Retry accepts only that complete footer, resumes an owned incomplete
archive from its journaled chunk boundary or recreates it, and never treats guest-agent stdout or
worker memory as the durable copy. The provider verifies the System domain inactive and the maintenance domain destroyed before
each attachment or System boot; both domains cannot hold the overlay concurrently.

The prepare journal records the appliance, content-volume, and recovery-volume immutable identities,
reserved byte debit, maintenance-domain identity, overlay target identity, requested helper
operation, archive length/digest/footer, and helper result before advancing.
Retry observes those identities and either resumes the same operation or fails closed; an unknown
domain, attachment, volume, or helper result enters `recovery_conflict`. Recovery uses the same
editor and saved tree. The appliance and helper are a remote-libvirt deployment prerequisite owned
by its provisioning role, not a Profile initrd or part of the external boot plan. #2107 must unit-test
the injected editor seam and live-prove capture, replacement, worker-loss retry, restore, and teardown
against a remote libvirt host before remote external boot is enabled.

After offline module publication, remote-libvirt records its disk/GRUB recovery point and activates
direct-kernel XML without changing the disk overlay, networking, guest-agent channel, console,
gdbstub, or capture devices. A
test-only non-libvirt implementation consumes the shared value types and returns opaque references;
it proves the boundary contains no libvirt type without claiming that the shape is sufficient for
HTTP/iPXE.

## Consequences

- External Run boot has one artifact-pair and command-line meaning across providers. Remote initial
  provisioning remains disk/GRUB boot, and existing images without root provenance remain usable on
  that path.
- Catalog-attested root provenance requires a typed extension to the existing attestation model and
  its serialized inventory shape. Old records remain readable but cannot authorize external boot
  until restaged, rebuilt, or explicitly re-attested with the new versioned fields.
- Recovery stores the exact state being replaced, including a same-release module tree, so
  configuration drift cannot silently change the rollback target. Provider-specific recovery bytes
  require bounded storage, tenant ownership, redaction, retention, and reaping behind the provider
  seam.
- Core gains durable activation state and reconciliation work. This is necessary because provider
  activation and database commits cannot share a transaction.
- Retries compare immutable plan and materialization identities. A reused object key with another
  version, digest, architecture, release, root specification, initrd pairing, or module obligation
  is rejected rather than overwritten.
- Module source identity and installed identity are distinct. The portable source digest covers
  validated bundle input; the installed digest includes provider-generated indexes and is what
  activation and recovery compare.
- A provider-side change outside KDIVE's System lock is preserved as `recovery_conflict`. Recovery
  is therefore fail-closed and requires an administrator either to compare-and-set restoration of
  the recorded point from the acknowledged observed identity or to tear down the System.
- Libvirt state identity compares the canonical full preserved inactive definition plus the three
  external-boot fields and module-tree content, never live XML. External boot still requires the
  running-kernel identity proof; GRUB recovery requires fresh-boot readiness because its bootloader
  selection is not part of the inactive definition.
- ADR-0082's in-guest GRUB install remains the provisioning/recovery mechanism but no longer defines
  iterative remote Run boot once this decision is implemented.

## Considered & rejected

- **Keep provider-specific Run boot and defer a shared contract until a bare-metal provider exists.**
  judgment: issue #2105 requires the same finalized external build to have one meaning across both
  current libvirt providers and a non-libvirt boundary proof now; deferral leaves the existing
  kernel/initrd divergence and root ambiguity intact.
- **Add remote direct-kernel rendering but leave recovery implicit in disk/GRUB provisioning.**
  judgment: a worker death between provider activation and its database commit leaves no durable
  fact that distinguishes the intended external definition from the definition to restore, so this
  narrower adapter cannot meet the required crash-consistent recovery behavior.
- **Re-render disk/GRUB recovery state from the current profile and provider configuration.**
  verified: `src/kdive/providers/remote_libvirt/lifecycle/xml.py` renders network, machine, storage,
  gdbstub, SSH-forward, console, and guest-agent settings from live configuration, while teardown
  already reads provider facts from domain XML to survive configuration drift. Re-rendering later
  can therefore produce a different definition from the one activation replaced.
- **Store libvirt XML in the shared boot contract.** judgment: this makes a provider-neutral seam
  carry one provider's transport and prevents the non-libvirt boundary proof the issue requires.
- **Treat the combined kernel/modules bundle as the bootable kernel.** verified:
  `src/kdive/providers/local_libvirt/lifecycle/install.py` extracts `boot/vmlinuz` before assigning
  the direct-kernel XML `<kernel>` path; the bundle is an archive, not executable kernel bytes.
- **Give kernel and initrd independent activation identities.** judgment: independent identities
  permit a valid artifact from one finalized build to be paired with another and cannot enforce the
  issue's paired-artifact requirement.
- **Keep remote iterative Run boot on the in-guest GRUB helper.** judgment: it regenerates caller
  initrd bytes and preserves an implicit root command line, so the same finalized external build
  cannot have one meaning across providers.
- **Add a standalone System-profile INITRD input.** verified: closed PR #2104 demonstrates that
  shape and rejects it for remote-libvirt; issue #2105 and epic #1423 explicitly exclude the surface
  in favor of the existing external Run-build lane.
