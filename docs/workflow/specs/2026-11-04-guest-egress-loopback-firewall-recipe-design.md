# Guest-egress loopback firewall recipe (#3514)

## Problem

ADR-0810 names a uid-scoped loopback firewall rule as an untested operator control for
`restrict=off` guests. It also says the worker slot accounts run the session daemon, which the
lifecycle installer contradicts: an operator-owned daemon runs QEMU, so a slot-uid rule blocks
nothing. Operators have no proven control, and the runbook still calls #3496 open.

## Scope

Record the lab result, not a new control. Append one ADR-0810 amendment (uid correction, results,
limits, whether the rule covers the `kdivebuild` NIC). Add the proven rule and its limits beside the
exposure paragraph in `docs/operating/runbooks/image-lifecycle.md`, and fix its stale #3496 line.
No code, shipped role, or provisioning change; no ownership transition. Host names stay out of
public text. Excluded, with owners: operator (firewall role, enforcement, `qemu:///system` arm);
#3513 (customization NIC decision); #3515 (ADR-0804 pointer); #3541, #3547 (authority SSH reach).

### Failure model

1. **Actors and deployments:** an operator on a local-libvirt or authority host; an untrusted guest
   kernel with `restrict=off`. `qemu:///system` is unnamed, so outside this model.
2. **Invariants and assets:** the runbook must not recommend a rule that silently fails to block, or
   that breaks kdive on a host whose services share the QEMU uid. Other guests' SSH and gdbstub
   listeners are the assets.
3. **Accepted failure classes:**
   - Non-local destinations stay reachable: the network-zone firewall owns them (ADR-0313).
   - The rule is non-persistent and unenforced: enforcement is operator-owned.
   - openSUSE and Rocky stay unproven: the dispatch limits the proof to two families.
   - The proof runs domains with kdive's NIC and gdbstub arguments, not provisioned Systems: the
     rule acts on QEMU's sockets, and keyed to the demo operator it stops kdive anyway.
4. **Covered elsewhere:** the `kdivebuild` NIC decision (#3513); authority SSH tool reach (#3541,
   #3547).

## Success

1. The amendment states, per proved family, what the rule blocked and which listed paths survived:
   guest DNS, outbound NAT, package install, client SSH `hostfwd`, client gdbstub attach.
2. The amendment states which account owns QEMU for the local and authority arrangements, and shows
   a client sharing that uid is blocked.
3. The runbook rule uses `table inet`, states the loopback-resolver exemption, and limits itself to
   hosts where no kdive client shares the QEMU uid. Unproven families are named with the reason.

## Validation

- Contract: recorded rule behaviour. Mode: `task-test-not-applicable`. Reason: nothing in the
  repository executes a host nftables rule; the proof is the live run on lab hosts, with baseline,
  rule, and removal rounds, whose redacted transcript goes on the pull request.
- Contract: runbook and ADR prose. Mode: `task-test-not-applicable`. Reason: wording has no
  executable consumer; `just records` and the doc checks in `just ci` cover shape and links.
