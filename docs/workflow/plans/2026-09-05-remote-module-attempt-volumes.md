# Remote module attempt-volume preparation plan

Goal: land #2170's volume-name preparation slice and ADR-0603 provider-host identity adapter.

Architecture: attempt preparation uses the existing name, ADR-0605 verified-obligation,
attachment, and storage-double contracts. Server preparation composes ADR-0604's already-landed
Resource-bound identity adapter. One completion-owned offload keeps ADR-0605's transaction and
System lock alive until the synchronous two-volume operation actually completes.

Tech stack: Python 3.14, libvirt-python, pytest, `uv`, `just`.

## Global constraints

- Support x86_64 and ppc64le behavior; native ppc64le live testing is excluded for this campaign.
- Volume names are the ADR-0588 ownership channel and must round-trip through its existing parser.
- Every identity result is path-free, strictly typed, bounded to unsigned 64-bit components, and
  either `inode(st_dev, st_ino)` or `block(st_rdev, 0)`.
- The identity call uses the positive remaining enclosing deadline and all failures are redacted.
- No SSH, generic command execution, new listener, credentials, schema, migration, or public API.

Expected implementation size: 900–1,350 changed lines (L) — porting one mature provider module and
its focused tests plus one bounded adapter and regressions.

## Task 1 — compose the landed remote-host device identity port

Files:

- Modify the server-preparation composition site that constructs the volume operation.
- Modify its focused tests.

Interfaces:

Use `build_remote_device_identity(runtime.authority, preparation_deadline)` after Resource
rebinding. Do not add another identity model, serializer, transport, or provider capability.

Steps:

1. Add a composition test proving the already-bound authority sender and one captured preparation
   deadline are the only identity inputs; confirm failure before the composition call is present.
2. Compose ADR-0604's existing port and fail closed before storage mutation when the Resource has
   no authority route.
3. Run the existing ADR-0604 timeout, malformed-result, alias, block-device, and redaction tests
   alongside the new composition case; expect all passed.
4. Run `just lint` and `just type`; expect clean; commit.

## Task 2 — port attempt-volume preparation onto persisted names

Files:

- Create `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py`.
- Create `tests/providers/remote_libvirt/lifecycle/rootfs/test_remote_module_volumes.py`.

Interfaces: consume `render_module_volume_name`, `parse_module_volume_name`, `ModuleVolumeOwner`,
the shared remote-libvirt storage fakes, and `AttachmentInspection.proves_detached`. `VolumeRequest`
contains no repository callback or reusable verified-authorization value.

Steps:

1. Port the parent test module onto shared fakes and confirm its metadata readback fails because
   the fake correctly discards metadata.
2. Port the provider module without `_METADATA_NS`, `_identity`, `_metadata`, or
   `PreparedVolume.identity`; render both names through the existing renderer and validate the
   reopened name, raw format, and capacity.
3. Preserve source full-image verification, bounded streaming, cleanup, protected path lookup,
   and positive detached deletion proof; run the focused tests and expect all passed.
4. Add controlled faults for name, raw format, capacity, and detached proof, proving each new test
   fails when its corresponding guard is removed, then restore the implementation.
5. Run `just lint` and `just type`; expect clean; commit.

## Task 3 — prove durable intent, completion ownership, and wire framing

Files: update the two Task 2 files and their focused tests only.

Steps:

1. Add the async orchestration seam that passes ADR-0605's exact request/attempt to
   `run_verified_module_attempt_preparation` and supplies one inline two-volume consumer. Confirm a
   missing/mismatched/discharged receipt reaches no libvirt call.
2. Add one worker-service-owned four-thread executor and four non-waiting admission slots. Submit
   the synchronous primitive once, bind admission release to the concurrent future's actual
   completion, and fail exhausted admission as a redacted infrastructure error without starting
   work. Shield the future; on cancellation, drain it to actual completion while retaining the
   verifier transaction/System lock, temporarily removing and finally restoring the exact task
   cancellation count before re-raising. Add blocked-call, repeated-cancellation, lock-retention,
   exact-capacity, completion recovery, shutdown/new-work rejection, and verified-retry tests.
3. Capture one absolute provider-operation deadline and check it before every `createXML`, stream
   send/receive/finish/abort, repair, and cleanup delete. Fault each boundary and prove no later
   stage starts after expiry.
4. Change the producer-facing request to obtain operation bytes through
   `RemoteModuleOperationV1.to_wire_bytes()`. Add a test through the appliance's real operation
   reader; confirm it fails with canonical unframed bytes and passes with the wire form.
5. Add the row-without-volume verified retry case; run the focused files, `just lint`, and
   `just type`; expect clean; commit.

## Task 4 — verify the bounded child

1. Run the focused rootfs provider tests and the named real-host identity regressions.
2. Run `rg -n 'urn:kdive:remote-module-volume|_METADATA_NS|def _identity|def _metadata' src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_volumes.py`; expect no matches.
3. Run `just test-changed`, then `just lint`, `just type`, `just test`, and
   `just ci > <private-log> 2>&1 < /dev/null`; require every exit status to be zero.
4. Review the branch against `main`, run the security pass because the adapter parses remote
   output, simplify without changing behavior, and publish one reviewed PR.
