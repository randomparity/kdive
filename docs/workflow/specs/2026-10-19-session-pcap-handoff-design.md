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

**D2 — check QMP replies (C2).** `LocalLibvirtTrafficCapture` parses every passthrough reply:

- An unparseable reply, a non-object reply, or one with neither `return` nor `error` raises
  `CONTROL_FAILURE` with `reason=qmp_reply_malformed`.
- An `object-del` error whose class is `DeviceNotFound`, or whose description contains
  `not found`, means the filter is already absent. It is tolerated in both attach and detach,
  as the libvirt-exception path tolerates absence today.
- An `object-add` error whose description begins `net dump: can't open` raises
  `CONFIGURATION_ERROR`. Its details are `reason=pcap_open_failed`, `operation=object_add`,
  `error=<QMP class>` and `remediation=PCAP_HYPERVISOR_WRITE_REMEDIATION`.
- Any other error reply raises `CONTROL_FAILURE` with `reason=capture_filter_rejected`, the
  operation, and the QMP class.

The QMP `desc` holds a host path, so the provider logs it at WARNING and never places it in
`details`. All the detail values fit #3269's forwarding shapes: token-shaped reason, operation and
error values, and printable ASCII remediation of at most 512 characters. A raised `libvirtError`
remains the transport failure path. Its text match for "not found" is removed, because absence
now arrives as a reply. That matches `LocalLibvirtCaptureQuiescence`, which already parses
replies. A refused attach now fails within one monitor round trip instead of after the capture
window, and the executor's `finally` detach still runs.

**D3 — remediation text.** Rewrite `PCAP_HYPERVISOR_WRITE_REMEDIATION` so it does not depend on
the daemon. It should say that the hypervisor could not create the pcap under
`/var/lib/kdive/pcap/`, and that on an SELinux-enforcing host that tree must carry
`svirt_image_t` (`just prepare-local-libvirt-host`, ADR-0640). It should also say that the
hypervisor account must be able to write the per-System directory, through the
`kdive-live-libvirt` group on the session stack or as the QEMU user under `qemu:///system`.
Update `prepare_pcap_dir`'s docstring to match. Its behavior does not change.

**D4 — preflight.** `scripts/operations/check-local-libvirt.sh` adds the pcap root to its
enforcing label loop and names it in the remediation hint. The test seam is the unprefixed
`CHECK_PCAP_DIR`. The provider hardcodes the path, so this is not a new operator knob.

**A1 — operator decision.** `LocalLibvirtCaptureReaper._detach_filter` discards the same reply,
so a refused `object-del` there reads as reclaimed. The recommendation is to apply D2's reply check
in this change, which is about 15 lines. The alternative is to report it as an occurrence. The
remote capturer has the same pattern and is excluded.

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
   - The job failure-category contract: `CONFIGURATION_ERROR` for operator-fixable host setup,
     `CONTROL_FAILURE` for a refused control operation.
   - No host path in public failure details.
3. **Accepted.**
   - Under the session daemon, any `svirt_t` domain may read or write another System's pcap at
     `s0`. ADR-0640 already accepts this for images, and a domain must first escape the guest to
     reach it.
   - A reworded QEMU `can't open` message falls back to `CONTROL_FAILURE` with the QMP class, so
     it is still loud.
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
  - The reply is parsed as bounded JSON, and only the class token and fixed strings reach
    `details`. A malformed reply fails closed.
- **Out of scope.** Per-System MCS isolation under the session daemon, as in ADR-0640.

## Validation

| Contract | Focused test |
|---|---|
| D2 add refused, can't open | `test_traffic_capture.py`: `CONFIGURATION_ERROR`, exact detail keys, no `desc` or path |
| D2 add refused, other | the same file: `CONTROL_FAILURE`, `capture_filter_rejected`, class token |
| D2 del absent, as a reply | the same file: tolerated in attach and detach; the replaced exception-text test is removed |
| D2 malformed or empty reply | the same file: `qmp_reply_malformed` |
| D2 forwarded through the child | `test_provider_child.py`: the reason, error and remediation survive `_forwarded_details` |
| D1 label set | `deploy/ansible/tests/run-local-libvirt-host.py`: the expected list includes the pcap root |
| D4 preflight | `test_check_local_libvirt.py`: a mislabeled pcap root fails, and a labeled one passes |
| D3 text | the existing remediation test plus a check that it is ASCII, printable and at most 512 characters |

A controlled fault proves the new tests catch the defect: restoring the discarded reply must turn
the D2 tests red. The guardrails are `just lint`, type checking, `just records` and the docs
checks.

**Live proof (needs separate approval).** Run on the Fedora 44 enforcing lab guest, at a
committed probe merge of this branch, the #3255 head and main, following the #3225 plan's
procedure:

1. Negative arm. Before the cells, relabel only the pcap root to `var_lib_t` and run one
   default/direct cell. It must fail `configuration_error` with `reason=pcap_open_failed` within
   seconds of `running`, and the 15-second window must not elapse. Then `restorecon` the root.
2. Run the four capture cells against the #3225 success criteria.
3. Restore the guest to `toolchain`.
