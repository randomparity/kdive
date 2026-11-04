# Guest-egress loopback firewall recipe (#3514)

## Problem

ADR-0810 names a uid-scoped loopback firewall rule as an untested operator control for
`restrict=off` guests. It also says the worker slot accounts run the session daemon; the lifecycle
installer shows an operator-owned daemon runs QEMU, so a slot-uid rule blocks nothing. Operators
have no proven control, and the runbook still calls #3496 open.

## Scope

Record the lab result, not a new control. Append one ADR-0810 amendment (uid correction, per-arm
results, limits, `kdivebuild` coverage). Beside the runbook exposure paragraph add the tested rule
and whether each installed arrangement can use it, and fix the stale #3496 line. No code, shipped
role, provisioning, or ownership change. Public text uses stable tokens for lab hosts, non-slirp
addresses, accounts, uids, and paths; slirp addresses, loopback, and versions stay. Excluded, with
owners: operator (firewall role, enforcement, `qemu:///system`); #3513 (customization NIC decision);
#3515 (ADR-0804 pointer); #3541, #3547 (authority SSH reach).

### Failure model

1. **Actors and deployments:** an operator on a local-libvirt or authority host; an untrusted guest
   kernel with `restrict=off`. `qemu:///system` is outside this model.
2. **Invariants and assets:** the runbook must not present a rule as usable where it silently fails
   to block or stops kdive. Assets: other guests' SSH and gdbstub listeners, on loopback or the
   host's own routable address, and lab identifiers.
3. **Accepted failure classes:**
   - Off-host destinations stay reachable: the network-zone firewall owns them (ADR-0313).
   - The rule is unenforced: enforcement is operator-owned; the runbook states it is lost on reboot
     or flush and gives a check.
   - openSUSE and Rocky stay unproven: the dispatch limits the proof to two families.
   - Domains carry kdive's arguments instead of being provisioned Systems: the rule acts on QEMU's
     sockets. Owner claims need the installed daemon and the running QEMU process, else that arm is
     recorded unproven.
4. **Covered elsewhere:** the `kdivebuild` decision (#3513); authority SSH reach (#3541, #3547).

## Success

1. The amendment records, per arm, no connection from the guest to loopback over IPv4 and IPv6, the
   host's own address, and another guest's listeners; and surviving guest DNS through a loopback
   resolver, NAT, package install, and client SSH and gdbstub. A failed or unrun arm says so.
2. It names QEMU's account per arrangement, states `kdivebuild` coverage, and shows uid-sharing
   service and client loopback connections fail.
3. The runbook rule uses `table inet`, `oif lo`, and `<qemu-uid>` and `<loopback-resolver>`
   placeholders with their sources, and applies only where no kdive service or client uses that uid.

## Validation

- Contract: recorded rule behaviour. Mode: `task-test-not-applicable`. Reason: nothing in the
  repository executes a host nftables rule. Evidence is baseline, rule, and removal rounds on lab
  hosts; raw output stays off the repository, and a whole-file identifier read precedes posting.
- Contract: runbook and ADR prose. Mode: `task-test-not-applicable`. Reason: wording has no
  executable consumer; `just records` and the doc checks in `just ci` cover shape and links.
