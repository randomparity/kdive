# Session-stack pcap hand-off on SELinux-enforcing hosts

Design for issue #3270; source baseline `8ed4d8b39`. Frozen scope:
[WORK:SCOPE](https://github.com/randomparity/kdive/issues/3270#issuecomment-6278678922), token
`q3270-ff95afbb`. Its criteria are C1 (the pcap directory carries a label a confined domain may
write), C2 (a refused capture filter fails loudly with a public-safe reason and remediation) and
C3 (the four `control.capture_traffic` functional cells pass on the session stack). Excluded, with
owners: the spool setgid and stuck job (#3225), failure-record diagnosability (#3269, merged), and
the remote capturer and any narrowing of the ADR-0640 label (dropped, no owner).

## Problem

`control.capture_traffic` fails on the local fixed-worker session stack of an SELinux-enforcing
Fedora 44 host. One diagnostic capture cell, run with `dontaudit` rules disabled and #3269's
forwarded details in place, recorded:

- The job failed `configuration_error` in phase `provider_execution` with
  `reason=pcap_not_written` and `bytes=0`.
- The per-System directory `/var/lib/kdive/pcap/<system>` existed. It was owned by the fixed worker
  with mode `0770`, group `kdive-live-libvirt`, and the inherited label `var_lib_t`. A one-second
  observer saw no file in it at any point of the 15-second capture.
- The domain's QEMU log records the `custom-monitor` taint at attach time and no error.
- `matchpathcon /var/lib/kdive/pcap` gives `var_lib_t`, and the host's local fcontext rules carry
  no pcap entry. Neither worker venv can `import selinux`, so the runtime relabel was skipped.
- `ausearch` found no AVC. [ADR-0640](../../adr/0640-static-svirt-image-label-for-session-mode-domains.md)
  records that `ausearch` does not surface these denials on these hosts; the journal holds them.

A local reproduction used the same component versions (Fedora 44, QEMU 10.2.2, libvirt 12.0.0
session daemon) with QEMU running confined as `svirt_t:s0:cX,cY`:

- An `object-add` of `filter-dump` sent with `virsh qemu-monitor-command` into a `var_lib_t`
  directory exited 0. The reply was
  `{"error":{"class":"GenericError","desc":"net dump: can't open <path>: Permission denied"}}`,
  and the journal logged `avc: denied { write } … tcontext=…:var_lib_t:s0 tclass=dir`.
- The same command into an `svirt_image_t` directory returned `{"return":{}}` and left a 24-byte
  pcap labeled `svirt_image_t:s0`.
- A bare QEMU answers an `object-del` of an absent id with a `GenericError` *reply*
  (`object '<id>' not found`), not a transport failure.
- libvirt v12.0.0 `qemuMonitorJSONArbitraryCommand` serializes whatever reply QEMU returns and
  never inspects its `error` member.

**Root cause 1 — label.** `local_worker_host_svirt_image_directories` omits `/var/lib/kdive/pcap`,
so every per-System directory inherits `var_lib_t`. The confined domain cannot create its pcap.

**Root cause 2 — swallowed refusal.** `LocalLibvirtTrafficCapture._object_add` and `_object_del`
discard the passthrough's return value. They treat only a raised `libvirtError` as failure, which
is the premise ADR-0385 states and the evidence above refutes. QEMU's refusal therefore surfaces
15 seconds later at readback as `pcap_not_written`. Its remediation names `qemu:///system` and a
root worker, and neither applies to this topology.

Ruled out:

- DAC. With a session daemon libvirt keeps uid and gid at `-1`. `virSetUIDGID` calls
  `setgroups` only when the gid changes (`virutil.c`), so QEMU keeps the operator account's
  `kdive-live-libvirt` membership.
- A private mount namespace. libvirt sets one up only for a privileged daemon (`qemu_conf.c`).
- The missing `selinux` binding as the fix. ADR-0640 already rejects a relabel from the
  unprivileged worker.

## Design

**D1 — provision the label (C1).** Add `/var/lib/kdive/pcap` to
`local_worker_host_svirt_image_directories`. The existing enforcing-only `sefcontext` and
`restorecon -R` tasks then cover it. Each per-System directory the worker creates inherits
`svirt_image_t:s0`, and so does each pcap QEMU creates. Under the session daemon that is a label
QEMU can use. Under `qemu:///system` the result is the same, because neither daemon relabels an
out-of-band file. The root worker's existing chown to the QEMU user is unchanged.

**D2 — one checked QMP reply (C2).** A new module,
`src/kdive/providers/local_libvirt/lifecycle/qmp_reply.py`, owns QMP passthrough reply parsing
for the local provider. `parse_qmp_reply(raw)` returns the reply's `return` value, its `id`, and,
for an error reply, the error class and description. It raises `MalformedQmpReplyError` for any of:

- invalid JSON or a reply that is not an object;
- a reply with neither `return` nor `error`, or with both;
- an `error` that is not an object carrying a string `class`.

An error is *absent* when its class is `DeviceNotFound` or its description contains `not found`.
Three callers use it, and none of them keeps the `libvirtError` "not found" text match, because
absence arrives only as a reply. A raised `libvirtError` stays the transport-failure path.

- **Capturer** (`LocalLibvirtTrafficCapture`, attach and detach):
  - An absent `object-del` is tolerated.
  - An `object-add` refused with `net dump: can't open …: Permission denied` (EACCES) raises
    `CONFIGURATION_ERROR` with `reason=pcap_open_denied`, `operation=object_add`,
    `error=<QMP class>` and `remediation=PCAP_HYPERVISOR_WRITE_REMEDIATION`.
  - Any other error reply raises `CONTROL_FAILURE` with `reason=capture_filter_rejected`, the
    operation and the QMP class. That includes the same open message with another errno, such as
    ENOENT when teardown's `rmtree` races a capture, or ENOSPC.
  - A malformed reply raises `CONTROL_FAILURE` with `reason=qmp_reply_malformed`.
- **Reaper (A1)** (`LocalLibvirtCaptureReaper._detach_filter`, `object-del` only):
  - An absent filter is tolerated.
  - Any other error reply, or a malformed one, raises `CONTROL_FAILURE`, so the sweep defers the
    row. A refused delete no longer reads as reclaimed.
- **Quiescence** (`capture_operation.py`) keeps its `CONTROL_FAILURE` category, its
  id-correlation check and its `allow_not_found` rule, and the parsing moves into the shared
  module. Some reply shapes now get a different message:
  - a non-object reply now reads `malformed`, where it used to read "did not correlate";
  - a reply with neither member now reads `malformed`, where it used to read "inconclusive";
  - a reply carrying both `return` and `error`, which used to succeed, is now `malformed`;
  - an error without a class is now `malformed` and no longer counts as absent.

The QMP `desc` holds a host path, so the provider logs it at WARNING and never places it in
`details`. The log carries it repr-quoted and cut to 256 characters, because QEMU controls that
text. The detail values fit #3269's forwarding shapes: token-shaped reason, operation and
error, and printable ASCII remediation of at most 512 characters.

A refused attach fails after two monitor round trips: the leading `object-del`, then the
`object-add`. It no longer waits out the capture window. The executor's `finally` detach still
runs. That detach's `object-del` gets an absent reply, which is tolerated, so the attach error
survives. If that detach itself raises, for example because the domain vanished, its
`CONTROL_FAILURE` replaces the attach error. That is accepted below.

`CONFIGURATION_ERROR` is not retryable (`RETRYABLE_BY_CATEGORY`), so an EACCES refusal fails the
job on its first attempt. `CONTROL_FAILURE` is retryable, so a deterministic
`capture_filter_rejected` or `qmp_reply_malformed` is re-dispatched up to the queue's three
attempts before the job fails. That is slower, but loud on every attempt. The operator chose not
to mark these failures terminal.

**Readback and reclaim on the session stack.**

- The worker creates the per-System directory. `prepare_pcap_dir`'s `chmod(0o770)` clears the
  setgid bit it inherited from the `2770` root.
- QEMU runs as the operator account and creates the pcap with `0644` under libvirt's `002`
  umask. The file is therefore owned by the operator and the operator's primary group, mode
  `0644`, label `svirt_image_t:s0`.
- The worker that owns the directory reads the file through the other-read bit. A worker in
  another slot reaches it through the directory's `kdive-live-libvirt` group and also reads it
  through the other-read bit. The reading worker is `unconfined_service_t`, which may read
  `svirt_image_t`.
- Reclaim unlinks the file, which needs write on the directory: the owner, or the
  `kdive-live-libvirt` group.
- `reclaim()` swallows `OSError`. The cells alone therefore cannot prove the file was reclaimed,
  so the live proof checks that no `<job>.pcap` is left after each cell.

**D3 — remediation text.** Rewrite `PCAP_HYPERVISOR_WRITE_REMEDIATION` so it does not depend on
the daemon. It should say that the hypervisor could not create the pcap under
`/var/lib/kdive/pcap/`, and that on an SELinux-enforcing host that tree must carry
`svirt_image_t` (`just prepare-local-libvirt-host`, ADR-0640). It should also say that the
hypervisor account must be able to write the per-System directory, through the
`kdive-live-libvirt` group on the session stack or as the QEMU user under `qemu:///system`.
Update `prepare_pcap_dir`'s docstring to match. Its behavior does not change.

The attach-time error satisfies criterion C2. The best-effort runtime relabel is left in place,
but nothing relies on it to report a failure.

**D4 — preflight.** `scripts/operations/check-local-libvirt.sh` adds the pcap root to its
enforcing label loop and names it in the remediation hint. As with rootfs and install, a missing
root reads as `labeled unknown` and fails. The test seam is the unprefixed `CHECK_PCAP_DIR`. The
provider hardcodes the path, so this is not a new operator knob.

**Dependency on #3225.** This branch alone cannot satisfy C3. The four cells also need the
spool and finalization fixes in #3255, which closes #3225. The PR therefore does not claim C3
against main by itself. The bound live proof runs on a merge of both heads.

The remote capturer has the same ignored-reply pattern and stays excluded.

**ADR impact.**

- Amend ADR-0640: the pcap tree joins the label set, with the same shared-`s0` consequence.
- Amend ADR-0385: QMP failures arrive as reply payloads, and attach and detach check them.

No new ADR number, migration or setting.

## Failure model

1. **Actors and deployments.**
   - A local operator running the fixed-worker session stack on a RedHat-family enforcing host.
   - The same role on a permissive or non-SELinux host.
   - A root worker under `qemu:///system`.
2. **Invariants and assets.**
   - Captured guest traffic, which is `SENSITIVE`.
   - sVirt confinement of the domain, which stays `svirt_t` with MCS categories.
   - The job failure-category contract: `CONFIGURATION_ERROR` only for an EACCES refusal, which
     the operator can fix; `CONTROL_FAILURE`, which is retryable, for every other refusal.
   - No host path in public failure details.
   - A refused reaper delete is never reported as reclaimed.
3. **Accepted.**
   - Under the session daemon, any `svirt_t` domain may read or write another System's pcap at
     `s0`. ADR-0640 already accepts this for images, and a domain must first escape the guest to
     reach it.
   - A deterministic non-EACCES refusal retries up to three attempts before failing. It is
     slower, but loud on each attempt.
   - A reworded QEMU message falls back to `CONTROL_FAILURE` with the QMP class.
   - A detach that raises after a refused attach masks the attach error with its own
     `CONTROL_FAILURE`. This needs the domain to vanish between the two calls.
   - A host with a broader foreign fcontext rule written later still mislabels the tree. ADR-0640
     names this case, and D4 reports it.
4. **Covered elsewhere.**
   - Spool permissions and job finalization: #3225.
   - Detail forwarding: #3269.
   - The remote provider's reply handling: dropped by the operator.

### Threat model

- **Boundaries.** One is widened: the object label on `/var/lib/kdive/pcap`, from `var_lib_t` to
  `svirt_image_t`. One is added: the QMP reply text now enters failure classification.
- **Actors.** A compromised guest that reaches QEMU, and the local operator. The operator account
  is trusted.
- **Controls.**
  - The label stays enforcing-gated, and the directory and group modes are unchanged.
  - The reply is parsed as JSON with an exact shape, and only the class token and fixed strings
    reach `details`. A malformed reply fails closed.
- **Out of scope.** Per-System MCS isolation under the session daemon, as in ADR-0640.

## Validation

| Contract | Focused test |
|---|---|
| Shared parser | `test_qmp_reply.py`: ok, error, absent (class and text), and each malformed shape |
| D2 add refused, EACCES | `test_traffic_capture.py`: `CONFIGURATION_ERROR`, exact detail keys, no `desc` or path |
| D2 add refused, other errno or class | the same file: `CONTROL_FAILURE`, `capture_filter_rejected`, class token |
| D2 del absent, as a reply | the same file: tolerated in attach and detach; the replaced exception-text test is removed |
| D2 malformed reply | the same file: `qmp_reply_malformed` |
| D2 through the executor | `test_traffic_capture.py`: a refused add plus an absent detach gives `CONFIGURATION_ERROR` with zero polls |
| D2 forwarded through the child | `test_provider_child.py`: the reason, error and remediation survive `_forwarded_details` |
| A1 reaper | `test_reaping.py`: an absent reply is tolerated; another error or a malformed reply raises `CONTROL_FAILURE` |
| Quiescence unchanged | the existing `test_capture_operation.py`, with absence as a reply |
| D1 label set | `deploy/ansible/tests/run-local-libvirt-host.py`: the expected list includes the pcap root |
| D4 preflight | `test_check_local_libvirt.py`: a mislabeled or missing pcap root fails, and a labeled one passes |
| D3 text | a check that the remediation is ASCII, printable and at most 512 characters |

A controlled fault proves the new tests catch the defect: discarding the reply again must turn
the D2 and A1 tests red. The guardrails are `just lint`, type checking, `just records` and the
docs checks.

**Live proof (operator-granted).** Run on the Fedora 44 enforcing lab guest, at a committed probe
merge of this branch, the #3255 head and main, following the #3225 plan's procedure:

1. Negative arm, on a fresh System. Relabel only the pcap root to `var_lib_t` and run one
   default/direct cell. `failure_context.failure_detail_reason` must read `pcap_open_denied`.
   The job must fail `configuration_error` within seconds of `running`, without waiting out the
   15-second window. Then `restorecon` the root.
2. Run the four capture cells against the #3225 success criteria. After each cell, also check
   that no `<job>.pcap` remains.
3. Restore the guest to `toolchain`.
