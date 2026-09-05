# Remote external-boot native live proof

## Scope

Issue #2121 adds an operator-run x86_64 `live_vm_remote` proof for the already accepted
remote-libvirt external-boot contract. It changes test and operator documentation only. Native
ppc64le execution and production behavior are excluded.

The proof must demonstrate, on a genuinely remote libvirt host, that an ordinary disk/GRUB System
can boot a matched kernel and initrd through the external-boot path and then return to its recorded
baseline without reprovisioning. It also proves that only `<kernel>`, `<initrd>`, and `<cmdline>`
change in the persistent definition; disk, network, guest-agent, console, gdbstub, and capture
devices remain byte-semantically equivalent.

## Design

Add one live-test module with one end-to-end carrier and small private helpers:

1. The carrier gate resolves the existing remote contract plus operator-supplied local kernel,
   initrd, root-device, and ACL-bound gdb address inputs. An unset remote URI reports a pytest skip
   from inside each test. Once the URI is set, incomplete paths or an unusable remote connection
   fail loudly.
2. The primary test provisions a uniquely named System from the configured staged base volume,
   records the inactive disk/GRUB definition and the running baseline identity, creates a
   run-unique remote directory pool under the Ansible-provisioned confined artifact parent,
   materializes the supplied pair into that isolated pool, and constructs the production
   `RemoteExternalBootDefinition`.
3. Immediately after guest readiness, the test uses the hardened production `GuestAgentExec` seam
   for one bounded `/proc/cmdline` read, removes exactly one required trailing newline, and asserts
   exact command-line bytes. A dedicated assertion helper reports the expected and observed byte
   strings and the first differing byte offset, including the prefix case where one string ends
   first. Only after this assertion passes does the test call `observe_guest_identity` and assert
   both its complete running-kernel identity and its returned command-line value. The production
   observer therefore remains covered without allowing its identity gate to pre-empt the required
   command-line diagnostic.
4. The test compares normalized persistent XML before and after activation with the three owned boot
   fields removed. It additionally names the disk, interface/network, guest-agent channel, console,
   gdbstub command line, and capture devices in the diagnostic so a missing component is localizable.
5. Recovery uses a newly opened libvirt connection, representing loss of the activating worker's
   process and handles. After the primitive restores and starts the disk/GRUB definition, the test
   waits for the production guest-agent readiness signal and reads the baseline kernel identity
   again. Recovery passes only when that identity equals the pre-activation baseline. A second
   recovery call then proves the achieved state is idempotent and leaves the baseline usable.
6. The proof invokes the production boot-artifact reaper only on its run-unique pool, with an empty
   live-owner set, and asserts exactly its kernel and initrd disappear. The shared configured pool
   is never enumerated. Cleanup independently attempts recovery when applicable, domain/overlay
   teardown, isolated artifact reaping, pool destruction/undefinition, remote directory removal,
   and every connection close even after an earlier failure. It reports the primary failure plus
   all cleanup failures; one failure never suppresses later cleanup.
7. The carrier checks the ordinary storage pool and staged base volume by using them for a fresh
   provision and teardown. The corresponding runbook directs operators to run the owning Ansible
   roles on a clean host before the carrier; no hand-installed prerequisite is accepted as proof.

The live helper uses production render, storage, activation, observation, recovery, and reaping
primitives. The bridge into the closed plan/materialization models is explicit: SHA-256 values come
from the exact supplied kernel/initrd bytes; expected release, architecture, build id, and command
line come from bounded guest-agent reads of the baseline that those files were extracted from; the
root specification comes from the operator-declared root device; opaque artifact refs resolve by
their deterministic volume names to libvirt-reported absolute paths. Fields irrelevant to the
remote activation primitive (bundle container metadata and module-tree identity) use
domain-separated hashes over the exact live inputs and are labelled test-adapter evidence, never
claimed as server finalization evidence. The test asserts that the activation consumes only the
actual materialized artifact refs and observed identity. It does not create a provider facade.

## Error and cleanup behavior

Every resource name contains random UUID ownership and every mutation is paired with independently
attempted cleanup in reverse order. A missing environment means the remote family is absent and skips. Any partial
environment, TLS failure, absent pool or base volume, unreadable artifact, failed boot, mismatched
identity, failed recovery, or incomplete cleanup fails loudly. Guest-controlled bytes appear only
in the explicit live-test assertion diagnostic; production responses and logs remain unchanged.

## Verification

- Unit tests for the assertion helper cover equal values, first-byte mismatch, interior mismatch,
  and unequal-length prefix values, checking both strings and the exact first offset.
- Gate tests parameterize missing and partial kernel, initrd, root-device, and GDB-address companion
  variables. Root device must be an absolute `/dev/` path without whitespace; GDB address must be a
  nonempty IP address accepted by `ipaddress.ip_address`.
- Marker tests prove both carriers retain `live_vm` and `live_vm_remote` and call the gate inside the
  test body.
- `just test-live-remote` without remote environment reports skips rather than an empty collection.
- The runbook names all four companion variables and the same root-device/GDB-address validation.
- An operator-authorized x86_64 run against the configured remote host is required before handoff.
- `just lint`, `just type`, focused tests, and the full `just ci` pre-push gate remain green.

## Existing decisions

ADR-0425 owns the remote native-live family and skip-versus-fail contract. ADR-0583 owns the exact
external-boot definition, byte-exact command line, recovery, and artifact lifecycle. This change
selects no new architecture alternative, so it adds no ADR.
