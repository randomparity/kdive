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
strings `system_id`, `run_id`, and `build_generation`; `bundle` with NFC object-store `key` and
`version`, complete-object `sha256`, extracted-kernel `vmlinuz_sha256`, `member_count`,
`uncompressed_bytes`, `vmlinuz_size_bytes`, `decoded_kernel_size_bytes`, `elf_metadata_bytes`, and
`gnu_build_id_size_bytes`;
`initrd`, either null or the same key/version/digest shape plus `size_bytes`; complete `cmdline` string;
`debug_cmdline`, null or the preserved caller extra; ordered `platform_arguments`; `module_obligation`
with `mode`, `release`, `source_manifest`, `member_count`, and `uncompressed_bytes`; and the closed
`root` shape defined below. Unknown keys are
rejected. An initrd is valid only as part of this set; it has no independent activation identity.
External-build finalization streams the assembled bytes of each exact bundle and optional-initrd
VersionId through server-owned SHA-256, persists those complete-byte digests in the
InvestigationBuild artifact record, and includes them in its ADR-0531 canonical document and
`content_digest`. Chunk manifests and their ordered verified chunk digests remain transfer-integrity
evidence; their caller-supplied advisory whole-object hash is never copied into this plan. A valid
chunk vector with an incorrect advisory whole hash therefore finalizes with the server-computed
digest. Plan construction copies only those persisted trusted digests.

External-boot v1 admits an optional initrd of at most 536,870,912 bytes (512 MiB). Unit is bytes,
scope is the one exact initrd VersionId in one plan, and there is no reference clock. Finalization
counts the complete server-streamed bytes, persists `size_bytes` with the trusted digest, and on
excess records terminal `BUILD_FAILURE`, publishes no generation, and directs the producer to remove
unneeded content or rebuild a smaller initrd. Every provider advertising external-boot v1 guarantees
that capacity. Each materializer counts and digest-verifies the exact VersionId before publication or
provider mutation; a size or digest mismatch is terminal `INSTALL_FAILURE` and directs
re-finalization. Caller-supplied sizes are never accepted.

The same finalization pass streams the pinned bundle without filesystem extraction. It accepts
exactly one member named byte-for-byte `boot/vmlinuz`, requires that member to be a regular file,
and rejects duplicate names, normalized aliases such as `./boot/vmlinuz`, absolute names, links, and
other noncanonical or nonregular representations of that path. Every other member must be a canonical
member of the exact matching `lib/modules/<release>/` subtree; unrelated members and structural tar
metadata entries are rejected. The 200,000-member ceiling below counts every tar header, including
directories and rejected structural metadata, so zero-sized junk cannot evade the work bound. It derives and persists that member's
SHA-256, architecture, release, and GNU build ID, applies the version-1 safe topology rules below to
the matching release subtree, computes `module-source-manifest-v1`, and persists its digest, member
count, and sum of regular-file sizes plus the whole-archive count and byte total on the
InvestigationBuild generation before publication. Boot-image parsing is bounded separately: on both
x86_64 and ppc64le v1 permits at most 536,870,912 boot-member bytes, 2,147,483,648 decoded-kernel
bytes, and 16,777,216 distinct bytes read while parsing ELF headers, section tables, and note records;
the one unambiguous GNU build ID is 4 through 64 bytes. Units are bytes, scope is the one
`boot/vmlinuz` member, and there is no reference clock. Finalization and materialization decode only
to a quota-backed temporary object while streaming, never an unbounded memory buffer, and stop before
reading or writing limit plus one. Exceeding a parse limit records `BUILD_FAILURE` before publication
or `INSTALL_FAILURE` before provider mutation respectively and directs the producer to rebuild a
normal boot image. Finalization persists all four measured sizes above, plan construction copies
them, and materialization requires exact equality in addition to kernel digest and metadata.

Schema version 1
allows at most 200,000 whole-archive members and 8 GiB (8,589,934,592 bytes) of uncompressed regular
file content across the accepted kernel and module members per exact bundle VersionId; these are
fixed per-bundle limits with no reference clock.
Exceeding either records terminal `BUILD_FAILURE`, publishes no generation, and directs the producer
to remove unnecessary files or split/rebuild the bundle. Retry for the same VersionId reuses the
persisted completed scan or resumes its server-owned bounded stream; caller manifest values are never
accepted. Plan construction copies the persisted kernel digest, both whole-archive totals, and all
three module-obligation values.

Every materializer streams and recomputes the same whole-archive validation and manifest before any write, enforcing those bounds
before counting or buffering the next member. Unexpected members, mismatch of either persisted
whole-archive total, or mismatch of the module manifest, count, or byte total is terminal
`INSTALL_FAILURE`, changes no System state, and directs re-finalization. Thus both planes reject a tar
bomb at the same schema boundary while the expected value has one authoritative producer.
Materialization must repeat the canonical-member and uniqueness checks, extract that one
`boot/vmlinuz` from the combined bundle, validate its architecture and release against the plan,
match the extracted bytes' SHA-256 digest to `bundle.vmlinuz_sha256`, and satisfy the plan's
module-install obligation. The
provider fetches the exact recorded object versions and stream-verifies the complete bundle and
optional initrd bytes against their plan digests before extraction, publication, or reuse. The
compressed bundle is never itself a bootable kernel.

Successful materialization produces an immutable `external-boot-materialization-v1` record. It uses
the same serializer and ASCII domain prefix `kdive-external-boot-materialization-v1` plus NUL. It has
exactly: `schema`; `architecture`; NFC `provider_kind`; `ownership` with canonical UUID `system_id`
and `run_id`; `plan_identity`; `extracted_vmlinuz_sha256`; `source_module_manifest`;
`installed_module_tree`; `verified_bundle_sha256`; `verified_initrd_sha256`, null exactly when the
plan initrd is null; `kernel_observation` with architecture, release, and lowercase even-length
GNU `gnu_build_id` hex; and `artifacts`, whose `kernel` and `modules` each contain one deterministic
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

`ownership.build_generation` is exactly the selected `InvestigationBuild.generation` from ADR-0531;
its public lookup handle remains `build_ref`. It is not `BuildStepResult.build_id`, which is the GNU
kernel build ID represented separately as `kernel_observation.gnu_build_id`. Legacy Run-owned build
records without an InvestigationBuild generation are ineligible for external boot until published
through the existing generation-backed external-build flow; their current install/boot path remains
unchanged.

When the selected generation's `BuildStepResult.build_id` is nonempty, core requires it to equal
`materialization.kernel_observation.gnu_build_id` before `preparing`. A mismatch is terminal
`INSTALL_FAILURE`, performs no System or recovery-store mutation, and names re-finalization with a
matching kernel bundle/vmlinux pair as the recovery action. When no persisted debug build ID exists,
the materializer-observed GNU ID still binds running-kernel proof, but debuginfo-dependent consumers
remain unavailable until their existing provenance contract is satisfied. Contract tests finalize
individually valid K1 bundle and K2 vmlinux inputs and prove the mismatch cannot reach preparation or
activation.

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
`READINESS_FAILURE` as internal attempt metadata, transitions to `recovery_failed`, retains the
recovery evidence and reservation, and exposes non-retryable `CONFLICT` with
`suggested_next_actions=["systems.teardown"]`. The recovery job is terminal, so the queue does not
redeliver it; `READINESS_FAILURE` is never used as this state’s agent-visible category. System
teardown is the only exit from `recovery_failed`.

An administrator resolving `recovery_conflict -> recovering` starts a new recovery attempt and
records a new deadline from that operation's `server_time`; time parked in conflict consumes no
recovery-attempt window. That explicit resolution is the only operation that may replace an expired
or interrupted recovery deadline. Ordinary job and worker retries never extend one.

These normative all-zero vectors also pin every key and absence representation:

```json
{"architecture":"x86_64","bundle":{"decoded_kernel_size_bytes":200,"elf_metadata_bytes":50,"gnu_build_id_size_bytes":20,"key":"bundles/k.tar","member_count":2,"sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","uncompressed_bytes":101,"version":"v1","vmlinuz_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","vmlinuz_size_bytes":100},"cmdline":"root=UUID=x","debug_cmdline":null,"initrd":null,"module_obligation":{"member_count":1,"mode":"system-root-tree","release":"6.1.0","source_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","uncompressed_bytes":1},"ownership":{"build_generation":"00000000-0000-0000-0000-000000000001","run_id":"00000000-0000-0000-0000-000000000002","system_id":"00000000-0000-0000-0000-000000000003"},"platform_arguments":["root=UUID=x"],"root":{"architecture":"x86_64","arguments":["root=UUID=x"],"authority":"stage-inspection","root":"UUID=x","schema":"root-spec-v1","source":{"identity":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kind":"staged-image"}},"schema":"external-boot-plan-v1"}
```

```json
{"architecture":"x86_64","artifacts":{"initrd":null,"kernel":{"ref":"kernel/ref"},"modules":{"ref":"modules/ref"}},"extracted_vmlinuz_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","installed_module_tree":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kernel_observation":{"architecture":"x86_64","gnu_build_id":"0000000000000000000000000000000000000000","release":"6.1.0"},"ownership":{"run_id":"00000000-0000-0000-0000-000000000002","system_id":"00000000-0000-0000-0000-000000000003"},"plan_identity":"sha256:a526825f6daf93774d3892c515332ce86390d914c1ff8faf1d994f24a9ea061b","provider_kind":"local-libvirt","schema":"external-boot-materialization-v1","source_module_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_bundle_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_initrd_sha256":null}
```

Their respective identities are
`sha256:a526825f6daf93774d3892c515332ce86390d914c1ff8faf1d994f24a9ea061b` and
`sha256:dc2cdf6635a5caca475257f6c62c886cdd763e1858c5fb63a95346d800b54361`.

The non-null boundary vectors use an initrd exactly at the v1 byte limit:

```json
{"architecture":"x86_64","bundle":{"decoded_kernel_size_bytes":200,"elf_metadata_bytes":50,"gnu_build_id_size_bytes":20,"key":"bundles/k.tar","member_count":2,"sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","uncompressed_bytes":101,"version":"v1","vmlinuz_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","vmlinuz_size_bytes":100},"cmdline":"root=UUID=x","debug_cmdline":null,"initrd":{"key":"initrd/i.img","sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","size_bytes":536870912,"version":"v1"},"module_obligation":{"member_count":1,"mode":"system-root-tree","release":"6.1.0","source_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","uncompressed_bytes":1},"ownership":{"build_generation":"00000000-0000-0000-0000-000000000001","run_id":"00000000-0000-0000-0000-000000000002","system_id":"00000000-0000-0000-0000-000000000003"},"platform_arguments":["root=UUID=x"],"root":{"architecture":"x86_64","arguments":["root=UUID=x"],"authority":"stage-inspection","root":"UUID=x","schema":"root-spec-v1","source":{"identity":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kind":"staged-image"}},"schema":"external-boot-plan-v1"}
```

```json
{"architecture":"x86_64","artifacts":{"initrd":{"ref":"initrd/ref"},"kernel":{"ref":"kernel/ref"},"modules":{"ref":"modules/ref"}},"extracted_vmlinuz_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","installed_module_tree":"sha256:0000000000000000000000000000000000000000000000000000000000000000","kernel_observation":{"architecture":"x86_64","gnu_build_id":"0000000000000000000000000000000000000000","release":"6.1.0"},"ownership":{"run_id":"00000000-0000-0000-0000-000000000002","system_id":"00000000-0000-0000-0000-000000000003"},"plan_identity":"sha256:3727eedd7d5a4b3740828f083229b7aa67ebca0497b959dcf9727a64ced6e488","provider_kind":"local-libvirt","schema":"external-boot-materialization-v1","source_module_manifest":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_bundle_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000","verified_initrd_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}
```

Their respective identities are
`sha256:3727eedd7d5a4b3740828f083229b7aa67ebca0497b959dcf9727a64ced6e488` and
`sha256:c1bcec0d307105434d087774bc6a3fa61ce9be5250fcdff0d2dfb6a8ae152aab`.
Boundary tests accept that size and reject 536,870,913 bytes before publication.

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
`module-installed-tree-v1` with the same walker over the final tree. Generated indexes such as
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

Installed-tree vector with an unsupported-xattr directory:
`{"entries":[{"gid":0,"mode":"0755","path":"kernel","type":"dir","uid":0,"xattrs":{},"xattrs_supported":false}],"schema":"module-installed-tree-v1"}`
has domain-separated digest
`sha256:a1af9de8164af0171acebef4b06cb74c512f06dac0613fd8c080cad794326e01`.

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

Materialization does not change the System, but it occurs only after admission. Core first creates
the unique activation and commits `preparing` with reservation state
`pending` only while a System-locked query proves that no DebugSession for that System is attaching
or live, regardless of owning Run or transport, and that no active lifecycle, control, force-crash,
snapshot, traffic-capture, vmcore-capture, or other debug job owns or depends on the System; otherwise
the request returns `CONFLICT` before reservation or provider work. The provider then creates one
reservation covering materialization plus recovery and core records it `ready`. Only then may the
provider materialize the plan and persist its immutable record. A materialization failure or
cancellation follows `preparing -> abandoned` and the common verified cleanup/release sequence.
No quiesce or other guest mutation is allowed before materialization succeeds. Immediately before the first stop or
other power mutation, the destination-side serialized lane rechecks the same System-wide session
condition through core's authority-bound snapshot; a present or unreadable result performs no provider
mutation and returns to conflict handling. A deterministic provider prepare journal records the source definition and prior power state,
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

System-locked admission uses this closed matrix while an external activation is not fully cleaned:

- `preparing`, `prepared`, `activating`, `recovering`, `recovery_conflict`, `recovery_failed`, and
  terminal cleanup admit only activation-owned continuation, reconciliation, conflict resolution, or
  authorized teardown; every new install, lifecycle, power/control, snapshot, capture, and debug
  operation is rejected.
- `active` admits read-only System/Run observation and owning-Run debug attach/detach, traffic capture,
  force-crash, and vmcore capture. It rejects every install or restage, unrelated-Run operation,
  generic power/control operation, snapshot, and mutation of definition, modules, attachments, or boot
  selection. Every admitted active-state operation that mutates power or provider state uses the
  activation's current generation/token through the destination executor; observation-only work may
  remain on its existing read-only seam.
- `recovered` or `abandoned` with `cleanup_complete=true` has no external-activation restriction.

Every listed operation's reverse admission uses the same System lock, so committing `preparing` or
`recovering` closes its gate before the destination recheck. Interleaving tests cover install/restage,
power, force-crash, snapshot, traffic capture, vmcore capture, and debug admission immediately before
and after `preparing`, during `active`, and against release. They include a different Run's install
racing release and prove exactly one side proceeds; allowed active force-crash/capture paths prove
their executor claim and cannot cross a takeover barrier.

After the pending row exists, the provider creates a deterministic reservation owned by this
System/Run/plan for exactly operator-configured `recovery_reserve_bytes`. The sum of retained
reservations in one provider instance's recovery store cannot exceed its
`recovery_max_bytes`; both values are byte counts, and availability is observed at the response
envelope's `server_time`. Each store has a durable identity; compare-and-debit, idempotent lookup,
and release serialize under a store-scoped advisory lock, independently of the System lock. The debit
and reservation row commit atomically, and the deterministic ownership key makes retries find the
same debit. Release deletes that row and credits its bytes exactly once, but only after every owned
recovery and materialization object has been deleted and verified absent. A terminal row with
`cleanup_complete=false` remains fully charged. For an absent activation row, reconciliation first
deletes and verifies absence of every object bearing its deterministic ownership key, then releases
the orphan reservation under the store lock. Because the activation row precedes allocation, a live
allocation cannot be mistaken for an orphan. A crash before `ready` resumes allocation or abandons the pending
row without guest mutation. Exhaustion is retryable `CAPACITY_EXHAUSTED`, changes no
guest state, and directs the operator to clean terminal artifacts or raise the cap before retry.

The fixed reservation bounds materialized kernel/modules/initrd artifacts, the captured definition,
prior module tree, journal, and verification metadata together. Materialization and offline capture
cannot exceed it. An overrun before guest mutation abandons and cleans the activation; an overrun
after preparation restores the source definition,
exact prior module tree, and recorded prior power state; a previously stopped System remains stopped.
After verification it commits `abandoned` with cleanup pending; the common cleanup sequence deletes
and verifies the owned objects, releases the reservation exactly once, commits cleanup complete, and
reports `INSTALL_FAILURE` with the required minimum observed bytes so the operator can raise
`recovery_reserve_bytes`. Prepared, recovery, and conflict states retain the reservation;
abandonment, recovery, and System teardown run that ordered release sequence idempotently.

`platform_arguments` contains 1 through 32 nonempty ASCII tokens, each at most 256 bytes and without
ASCII whitespace or NUL. Exactly one starts `root=`;
`root.arguments` is a nonempty ordered array whose complete sequence occurs
exactly once as a contiguous subsequence of `platform_arguments`. No other platform element may use a
key present in `root.arguments`. Specifically, the sole root token equals ASCII `root=` concatenated
with `root.root`. A token's key is the nonempty bytes before its first `=`, or the entire token when
`=` is absent; duplicate-key checks compare those bytes, and “other” excludes the one validated
`root.arguments` occurrence.

`debug_cmdline` preserves the current caller contract: core strips surrounding characters with
Python 3.14 `str.strip`, then accepts 1 through 4096 printable Unicode scalar values, including
non-ASCII, internal whitespace, quotes, and backslashes; NUL and non-printable values remain rejected.
Before composition, core also retains the existing `platform_owned_cmdline_token` rejection: any
occurrence of `root=`, `console=`, `crashkernel=`, or `fadump=` anywhere in `debug_cmdline` fails with
`CONFIGURATION_ERROR` / `cmdline_overrides_platform_args`. This check runs before plan hashing and is
not weakened to token parsing.
It is null when no caller extra exists. This field and the derived `cmdline` are exempt from the
serializer's NFC-input rule so the accepted scalar sequence is not rewritten. Core builds `cmdline` exactly as the one-space join of
`platform_arguments`, followed by one space and `debug_cmdline` when non-null. For external boot v1,
the final UTF-8 encoding is at most 2,047 bytes so it plus the terminating NUL fits the conservative
2,048-byte command-line buffer common to every supported v1 architecture; the broader legacy caller
limit remains unchanged on non-external paths. Providers pass this string directly to libvirt or a fixed argv parameter;
they do not tokenize, quote, normalize, or invoke a shell. This makes the provenance and rendered
direct-kernel bytes one composition result while preserving currently admitted local inputs.
Contract tests reject each platform-owned substring, including a later `root=/dev/evil`, and prove
ordinary quoted, backslash-containing, whitespace-containing, and non-ASCII debug text remains
byte-preserved.
After fresh boot, the fixed guest observation reads the kernel's saved command line through
`/proc/cmdline`, removes only its one trailing newline, and returns the remaining bytes. Core requires
those bytes to equal the plan's UTF-8 `cmdline` exactly alongside the running-kernel identity proof.
Unavailable evidence retries only to the activation readiness deadline; truncation or any changed
byte records terminal-on-this-attempt `READINESS_FAILURE` and triggers recovery. Boundary tests cover
2,047 accepted bytes, 2,048 rejected bytes, and multibyte text crossing that byte boundary on x86_64
and ppc64le.
They also reject a missing or repeated `boot/vmlinuz`, `./` and leading-slash aliases, links and
other nonregular members, and two kernels with equal metadata but different bytes. The accepted case
proves every materializer matches the server-produced `bundle.vmlinuz_sha256` before publication.

The root specification is a versioned, closed data shape with exactly `schema`, `architecture`,
`root`, `arguments`, `authority`, and `source`. Version 1 admits authority/source-kind pairs
`stage-inspection/staged-image` and `catalog-attestation/catalog-image`; no other pair is valid.
External-build client or build-document facts are not a root authority. `source` has exactly `kind`
and an immutable lowercase `sha256:<64-hex>` `identity` copied from a persisted source field, never
re-serialized from the root facts. For `stage-inspection/staged-image`, it is the staged provenance row's
new `staged_image_sha256`, computed over the complete image bytes before bounded inspection and stored
with the inspection result. For `catalog-attestation/catalog-image`, it is the typed catalog
attestation's new immutable `image_sha256`, computed over or supplied with the verified catalog image
and stored with the attested root facts. Records lacking the required digest are ineligible rather
than assigned a surrogate identity.

The caller never selects the authority row. Under the System lock, plan construction derives it from
the target System's immutable persisted provisioning provenance and verifies that the System,
Allocation, Run, InvestigationBuild generation, and authority row belong to the same project and
authorized Investigation. The System's recorded source kind and exact image identity must equal the
authority row and `root.source`, and `ownership.system_id` binds that proof into the plan. A System
without immutable provisioning provenance remains eligible for its existing disk/GRUB path but not
external boot.

Immediately before plan creation, core reloads that derived staged-provenance or catalog row and
requires its persisted digest and root facts to equal the candidate source and root shape. Plan retry
repeats the System binding and comparison; a changed/missing row is stale input. Bounded stage inspection is a verified
authority. Catalog data extends the existing typed
attestation path with the same root value, ordered arguments, architecture, schema version, and
immutable image identity; the current attestation fields alone are insufficient. No second untyped
declaration path is added.
Unknown versions, missing facts, stale source identities, conflicting root arguments, and an
architecture mismatch fail before materialization or activation and name the recovery action. A
pre-schema image remains eligible for its existing GRUB boot but not external Run boot.
Contract tests reject valid same-architecture root facts from another image, System, project, or
Investigation and prove no caller-provided authority identifier participates in lookup.

Before changing boot state, the provider prepares both sides of the compare-and-set. It creates a
durable recovery point representing the exact current persistent boot configuration and prior module
tree, renders but does not apply the target configuration, and returns provider-computed source- and
target-state identities plus an opaque recovery reference. A state identity covers both the boot
definition and the release-qualified module-tree identity. That component identity is the closed,
hashable tagged value `{"state":"absent"}` or
`{"manifest":"sha256:<lowercase-hex>","state":"present"}`. Recorded absence is therefore an exact
source or target match, not missing evidence; absence conflicts only when the corresponding recorded
component requires `present`. An unreadable observation remains distinct from both values. Libvirt definition identity is a
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

Core persists the plan identity on the unique pending activation, then reservation readiness and the
materialization, and finally the recovery reference and both provider state identities when provider
preparation completes. The state machine is
`preparing -> prepared -> activating -> active`, with
`activating|active -> recovering -> recovered`,
`prepared -> recovering`,
`recovering -> recovery_failed`,
`preparing -> abandoned`,
`preparing|prepared|activating|active|recovering -> recovery_conflict`, and
`recovery_conflict -> recovering`,
failure metadata on an operation attempt.
Core permits at most one external-boot activation that is not fully cleaned per System.
A partial unique database index enforces that invariant across `preparing`, `prepared`, `activating`,
`active`, `recovering`, `recovery_conflict`, and `recovery_failed`, plus `recovered` or `abandoned`
rows whose `cleanup_complete` is false; all providers use the same core
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
component belongs to this activation. An unproven mixture enters `recovery_conflict`. A recorded
`absent` module component matches source or target according to its tagged identity; observed absence
when that side requires `present`, unreadable evidence, or a third identity enters
`recovery_conflict` for operator resolution instead of overwriting provider state. The portable plan identity is never compared directly with provider
definition bytes. Runtime readiness and running-kernel identity are separate observations and never
decide which persistent definition won.

Immediately before activation's first target module-tree or definition write, the destination-side
executor holds the current System fence and serialized mutation lane, observes power, and requires
the domain to be inactive. It then re-observes the complete source definition/module identity in that
same lane before the write-ahead CAS entry. An active or unreadable power result, or a non-source
component, performs no provider write and transitions to `recovery_conflict` with the observations
retained. No observation made at `prepared` is fresh enough to satisfy this gate. The adversarial
suite pauses after `prepared`, starts the domain out of band, resumes activation, and proves that
neither modules nor definition are changed.

This guarantee covers provider-mediated actors. Deployment ACLs grant mutation of KDIVE-owned domains
and their volumes only to the fenced executor credential; worker, reconciler, and other service
credentials are read-only and cannot bypass its lane. A privileged host administrator can bypass
libvirt ACLs and mutate during the bounded interval between the final observation and commit. That
concurrent break-glass action is unsupported: KDIVE preserves changes observed before the interval
and detects drift afterward, but does not claim compare-and-set against root-equivalent interference
inside a libvirt call. The executor audit marks the interval and runbooks require administrators first
quiesce it. Tests inject ordinary-client start/redefine attempts after the final check and prove ACL
denial; a privileged-interference test records the documented unsupported outcome rather than claiming
recovery-conflict preservation.

The existing transaction-scoped System advisory lock protects each database transition only; it
cannot fence provider work across those commits. The System owns a durable monotonically increasing
`operation_generation` allocated under that lock; it never resets when an activation terminates.
Each new activation and every takeover increments the System value and copies the resulting generation
plus activation identity into the activation row. Recovery points, staged artifacts, maintenance
domains, and externally visible target components bind only the stable
System/activation/Run/plan identity and their content identities; takeover never retags or recreates
them. The provider-durable per-System fence and each append-only mutation-journal entry additionally
record the actor generation and claimant token.

Every live execution attempt requests a fresh System generation and a cryptographically random
canonical UUID claimant token through a role-bounded database transition before provider work. For a
worker, the security-definer transition authenticates its ADR-0533 incarnation credential, derives
the holder from that credential, and atomically verifies the exact current job ID, charged attempt,
active worker incarnation, activation state, and prior System generation before incrementing. The
caller cannot supply or substitute those authority fields. Reconciler, server conflict-resolution,
and teardown paths use separate role-specific transitions that verify their authorized durable
operation identity and permitted activation edge; none can claim a worker attempt. The allocation row
permanently records authority kind, holder or operation identity, job and attempt when applicable,
activation identity, generation, and claimant-token hash. A stale worker whose job was reclaimed can
therefore affect zero rows even if it resumes before its first allocation. The plaintext token belongs
to that authenticated live execution only and is never copied into a job payload for another worker. An actor
atomically claims `(activation identity, generation, claimant token)` in the provider fence.
Idempotence requires the same triple and is permitted only for retries within that live execution;
any replacement, redelivery, reconciliation pass, or restarted process allocates a higher generation
and new token. A claim appends a zero-mutation header containing that triple and compare-and-sets the
fence in one provider-durable transaction before returning. Implementations without a transactional
store use one fsynced write-ahead record and commit marker: recovery exposes either the prior fence or
the new fence plus header, never a new fence without its header. A partial uncommitted claim is rolled
back to the prior fence under the provider-local lock.

The atomic claim holds that lock and first validates the stable ownership and content
digests of every existing recovery object, then validates the mutation journal as an unbroken sequence
ending with the prior generation's claim header and any mutations before atomically appending the new
header and fence value.
Existing journal entries remain immutable; new work appends with the new generation and token. A crash
after the database increment but before the provider claim abandons that unused generation; the next
execution allocates a higher one rather than sharing its authority. Missing, changed, or
unowned evidence enters `recovery_conflict` and is never recaptured from the current System state.

Replacement workers, reconciliation, conflict resolution, teardown, and later Runs all use their
corresponding authority transition before claiming. The destination executor authenticates the
durable allocation and token hash through its fixed core channel before accepting a claim; a
caller-chosen UUID or greater integer has no authority by itself. Provider-local serialization makes a new claim wait for any bounded
publication critical section; long downloads, extraction, and helper execution write only private
staging and are cancelable or discardable.

All definition, module-tree, attachment, power, and cleanup mutations execute through a
provider-private destination-side fenced executor, not directly from the queue worker. Local-libvirt
runs it as a separately supervised host service; remote-libvirt runs the same fixed-operation service
beside its libvirt host. Its typed requests contain only the stable operation identity, generation,
claimant token, expected component identity, and immutable artifact references—never caller commands
or shared libvirt types. The non-libvirt test implementation exercises the same protocol. Deployment
provisioning owns the service and external boot is not advertised until its health and durable store
are verified.

The executor serializes each System at the destination and validates the generation/token immediately
before the actual commit point. A higher-generation claim queues behind an executing mutation and is
acknowledged only after that operation has completed and its result identity is durably journaled;
that acknowledgement is the positive-quiescence barrier for replacement writes. A lost client
response is resolved by the stable operation identity at the executor, never by redispatching the
side effect. Module publication and maintenance-helper commits perform the token check inside the
helper immediately before rename. Libvirt mutations remain inside the executor's serialized lane; if
the executor itself restarts with an operation lacking a completion record, it accepts no new claim
until a domain-scoped libvirt barrier has waited for the old connection to close, acquired the same
domain mutation lock, and recorded the resulting persistent definition, attachment, job, and power
observations. An unreadable or non-quiescent result remains unresolved and permits only observation
through the executor; replacement and teardown writes remain barred until positive quiescence, with
out-of-band operator repair required if the barrier cannot complete.

The worker checks the fence before dispatch and after the result, but those checks are diagnostic;
destination serialization and commit-point validation carry the safety guarantee. A stale actor can
finish private work but cannot publish, boot, restore, or delete after takeover. Teardown claims the
newest generation and passes through the same quiescence barrier before destroying anything.
Connection loss does not release authority to an older generation; only a durable higher claim
supersedes it. Tests lose responses during every mutation, pause an old actor before publication and
boot, claim replacement and teardown generations, and prove no late completion crosses the barrier.

The same triple fences core truth. Every actor-originated activation transition, deadline or attempt
metadata update, failure/result write, recovery-job completion, and `cleanup_complete` commit runs
under the database System lock and compare-and-sets the activation identity, current
`operation_generation`, and claimant token stored for that live execution. Job-result persistence uses
the same predicate in its transaction rather than committing independently. Allocating a replacement
generation verifies the role-specific current authority and compare-and-sets the prior row generation,
so two replacements cannot both become current and a reclaimed attempt cannot allocate later. A
predicate mismatch returns an internal `superseded` outcome and performs no activation,
job, cleanup, or audit-result write; the current actor or reconciler owns durable completion. Merely
re-observing the provider or holding the transaction-scoped lock never authorizes an old result.

Adversarial tests pause an actor after each representative provider/readiness result and immediately
before `prepared`, `active`, `recovered`, `recovery_failed`, attempt-failure, job-result, and
cleanup-complete commits. Replacement and teardown actors claim a higher generation, after which each
old commit must affect zero rows and leave current lifecycle truth unchanged.
The stale-actor suite also completes one activation, starts a later Run with a greater generation,
and proves that an actor from the earlier Run can never reclaim authority.
It separately loses a worker after `prepared` and after the first target-component write, claims the
next generation, consumes the original activation-owned evidence, and proves safe resume without
retagging or source recapture.
The suite also pauses a worker before generation allocation, reclaims its job, lets the new exact
job attempt allocate and claim the next generation, then resumes the old worker with its still-valid
incarnation credential and proves allocation affects zero rows and no executor claim is possible. It
separately pauses an actor after allocation but before its first claim, lets another authorized
execution allocate and claim the next generation, resumes the old actor, and proves its different
token/generation cannot mutate state.
It kills both an initial and replacement actor after the claim returns but before the first mutation;
the next worker and authorized teardown must validate the zero-mutation header and take over without
conflict, retagging, or source recapture.

`recovery_conflict` has two resolutions only. A project administrator may invoke the audited
`resolve_external_boot_conflict` operation with `restore-recorded-source` and the exact currently
observed composite state identity and an idempotency key. Under the System lock, core repeats the
read-only observation, then durably records the acknowledged identity, resolution operation identity,
new generation/token, recovery deadline, and `recovery_conflict -> recovering` transition before any
provider write. A changed or unreadable value leaves the conflict untouched. Provider restoration
then uses the normal per-component write-ahead CAS journal from the acknowledged identity. A crash
before the transition changes no provider state; a crash after it resumes the same recovering
operation, and a CAS mismatch returns to `recovery_conflict` with evidence retained. Alternatively,
ordinary authorized System teardown follows the limited claim and quarantine path below. There is no adopt-current-state,
force-overwrite, or automatic timeout edge, because a mixed external definition cannot become a
trusted reusable baseline by declaration. Reconciliation preserves evidence and performs no provider
write while the state remains `recovery_conflict`.

Teardown uses a distinct destination-fence claim. It waits for the same positive executor-quiescence
barrier and proves the domain's KDIVE System/Allocation ownership from core plus persistent domain
metadata, but it does not require the recovery journal, manifests, or artifact digests to validate.
Its authority is limited to stopping/destroying that domain, deleting its deterministic overlay, and
deleting recovery objects whose individual stable activation ownership remains provable. Missing or
corrupt ownership evidence is quarantined, never guessed: teardown completes the System destruction,
the reservation stays charged, and the response exposes non-retryable `CONFLICT` with an operator
repair reference. A platform administrator must use audited `ops.resolve_recovery_orphan` with the
quarantined object identities to delete or adopt them; only verified absence then releases capacity.
Thus corrupt recovery evidence cannot block destruction or authorize unsafe cleanup.

Recovery restores a usable disk/GRUB baseline, not only persistent bytes. Run build state is not its
usage lease: current Runs are already `succeeded` before install and boot. A new contributor operation,
`runs.release_external_boot`, is the explicit end-of-use event for an active external-boot activation.
Under the System lock it refuses while any lifecycle, control, force-crash, snapshot, capture, or
debug job for the System is active, regardless of owning Run, and also refuses while any DebugSession
for the System is attaching or live, regardless of owning Run or transport. It then atomically
records the release request, recovery deadline, generation and token,
`active -> recovering` transition, and recovery job before observing provider state. Repeating
it is idempotent and returns the same recovery job. `debug_sessions.detach` does not release implicitly;
its response suggests `runs.release_external_boot` when it detached the last live session. Authorized
System teardown bypasses release because destruction owns cleanup.

After that commit, the provider observes the recorded target. Complete target state, including a
tagged `absent` component when the target recorded absence, proceeds. Observed absence when the target
requires `present`, or any third component, enters `recovery_conflict` without stopping or overwriting
the System. An unreadable component retries only until the persisted recovery deadline, then also
enters `recovery_conflict` with its observation evidence; no preflight retry can remain unbounded.
Immediately before stopping, the destination-side serialized lane rechecks that the System has no
attaching or live DebugSession; presence or an unreadable result performs no power mutation and
returns the operation to conflict handling. The provider then stops the domain through the control
plane and verifies it is inactive. It then re-observes the complete target
state immediately before the first restore write. An unreadable or third component at that point
enters `recovery_conflict` without an overwrite. The provider then restores the prior module tree and persistent
definition, then restores the recorded prior power state. When it was running, the provider boots
that definition and requires a fresh boot plus the existing System readiness contract before
committing `recovered`; when it was stopped, verified inactive complete source state commits
`recovered` without boot or readiness. The exact GRUB-selected kernel is guest bootloader state and
is not knowable from an inactive domain definition, so it is not an identity gate; a recovery that
reaches its persisted recovery deadline without readiness transitions to `recovery_failed` and
retains all evidence; it does not remain `recovering`. A recovery retry before that deadline
re-observes each component: complete target resumes restoration,
complete source resumes restoration of the recorded power state and its conditional readiness rule,
and a source/target mixture may resume only when every
component matches one of those recorded identities and the journal proves the partial write belongs
to this recovery. Before each subsequent write, that journal records the expected component identity;
the write uses compare-and-set from that value and records its result. Any other mixture or third
value enters `recovery_conflict`. Concurrent terminalization
serializes under the System lock. The recovery point and materialized artifacts cannot be deleted
before `recovered`. Core commits `recovered` with `cleanup_complete=false`, then deletes those objects
idempotently, verifies them absent, releases the reservation under the store lock, and commits
`cleanup_complete=true`; reconciliation finishes any interrupted step in that order.
System teardown instead destroys the domain before cleaning the recovery point
and materialization, because a definition that will be destroyed need not be restored or rebooted.
Artifacts remain while the Run can retry, are deleted idempotently on those ordered paths, and are
swept by deterministic ownership after worker death. A partial materialization is either atomically
published under its final identity or discoverable as an owned partial and removed; it is never
activated.

Adversarial recovery tests record an initially absent release tree, lose the worker before target
publication and after recovery removes the target tree, and prove both observations match the exact
source `absent` value without conflict. Session races attach other-Run and other-transport sessions
before initial prepare, between admission and its first stop, before release, and between release
admission and its first stop; every race performs no boot-state mutation until the System-wide
session is detached.

`preparing -> abandoned` is the pre-preparation disposal path. The provider journal first restores
the captured source definition and prior power state. Cancellation or reconciliation may take this
edge only after retries are no longer possible. Once `prepared`, cancellation instead takes
`prepared -> recovering` and uses the same recorded-power-state, deadline, readiness, terminalization,
and cleanup rules as post-activation recovery; recovery evidence remains until those rules complete.
After source verification in the preparing path, core commits
`abandoned` with `cleanup_complete=false` while retaining deterministic cleanup ownership. Cleanup
then removes the journal or recovery point and materialization idempotently, verifies them absent,
releases the reservation, and commits `cleanup_complete=true`;
reconciliation resumes any interrupted step. Worker loss at a deletion
boundary therefore retains the terminal proof, and admission remains blocked until cleanup completes.
A target or third identity fails closed as
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
- A provider-side change observed outside a fenced executor commit interval is preserved as
  `recovery_conflict`. Managed service identities cannot mutate inside that interval; privileged host
  interference during it is an audited, unsupported break-glass race. Recovery otherwise remains
  fail-closed and requires an administrator either to compare-and-set restoration of the recorded
  point from the acknowledged observed identity or to tear down the System.
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
