# 0662 — The `--wipe` reap's privilege follows the endpoint it was published

## Status

Accepted (2026-09-15)

## Context

`stack-down.sh --wipe` runs every mutating reap call under `sudo` and enumerates bare, so the two
halves do not carry the same identity (#2515 recorded the reporting half of that split and left
the privilege half to #2516). `resolve_libvirt_uri` publishes two daemon scopes to one variable:
the lifecycle contract's operator-owned **session** endpoint, and the `qemu:///system` fallback
on a host without that contract, which is root's. One fixed privilege cannot be right for both.

## Decision

`stack-down.sh` derives **one** privilege for the whole `--wipe` reap from the resolved
`KDIVE_LIBVIRT_URI`, by reading its path component with the query stripped: `/session` is reached
as the invoking account, everything else keeps `sudo`. That decision governs the enumeration that
grades the reap, the `destroy` and `undefine` that perform it, and the overlay `rm`, so
observation and mutation always carry the same identity.

The Decision above governs those four calls. The up-front `--wipe` gate's **liveness probe** is not
one of them, because it grades nothing. It proves the endpoint answers before the teardown begins, and it runs as the
**invoking account** on both branches. That keeps it the operator's own authorization for the
daemon they aimed at — the one check that can still tell an operator they are pointed somewhere
they have no business reaping.

## Consequences

- On a provisioned host the reap needs no root. The operator reaches the mode-`0770` socket and
  the mode-`2770` overlay directory as their **owner**: `install-live-worker-lifecycle.sh`
  installs the socket `operator_uid:group_gid:770` and `/var/lib/kdive/rootfs`
  `operator:kdive-live-libvirt` mode `2770`. `kdive-live-libvirt` membership is how the *worker*
  accounts reach the same two paths; both are access paths the contract publishes, and neither
  is root's.
- The gate's probe runs as the invoking account on every endpoint, so an operator is refused up
  front whenever the daemon they aimed at will not answer *them*. On a bare host, where the
  operator is normally in the `libvirt` group, it answers and the run proceeds to a reap that
  escalates — the behaviour before this decision, unchanged. On a provisioned host an operator who
  overrides the endpoint to `qemu:///system` is refused there, because the contract keeps them out
  of root's daemon.
  That refusal is the reason for the carve-out, and it is worth stating as a property rather than
  a preference: **for this gate, escalation must not be what makes an unreachable endpoint
  reachable.** Had the
  probe followed the endpoint's privilege, `sudo virsh` would answer it on every non-session
  endpoint and the gate could never refuse one. A run aimed at the wrong daemon would then pass
  the gate, drop the compose data volumes irreversibly, find zero domains where it looked, and
  sweep every overlay whose domains are alive on the daemon it did not ask — reporting success and
  exiting 0. Grading and mutation still share one identity, which is what this decision is for;
  the probe needs no identity agreement with them precisely because it grades nothing.
  Because the first escalation is therefore at the reap, after the `Type 'wipe'` confirmation, a
  host configured to prompt for a password asks only on a run the operator has already confirmed,
  and no sudo timestamp is refreshed by a run that aborts at the prompt.
- Classification is textual: an endpoint whose scope is not in its path would be misclassified.
  Both published URIs and the bare-host default carry it there.
- The overlay `rm` becomes the invoking account's on the session branch, so the operator must be
  able to **write** the overlay directory, not merely list it. That requirement is one this
  decision introduces: before it the block's only `rm` was an unconditional `sudo rm -f`, so root
  unlinked regardless of the directory's mode and write was never needed. The guard beside the
  sweep tests `! -r || ! -x` — listability — and cannot see it, because it was written when the
  removal could not be refused.
- A precondition is therefore added for it, in the up-front `--wipe` gate: on the session branch,
  an overlay directory that is not writable and holds at least one `*-overlay.qcow2` refuses the
  run before anything is stopped or dropped. The gate is where it has to live — the sweep runs
  after `docker compose --profile obs down -v`, so a refusal there arrives with the data volumes
  already gone. It is keyed on that **non-empty glob** rather than on the mode alone, and that
  condition is load-bearing: a bare `! -w` refusal grades a clean reap of an *empty* overlay
  directory as a failure, which is the defect #2515 closed. The installed `2770` directory is
  writable by its owner and never reaches the refusal. This bounds the writability class only; an
  unreadable directory still refuses at the sweep, after the volume drop, as it does today.
- Which *daemon* answers is unchanged for the published `?socket=` endpoints, where the socket
  path selects it; for a plain `qemu:///session` it changes from root's per-uid daemon to the
  invoking account's, which is the point.

## Considered & rejected

- **Keep `sudo` on every call.** verified: `_reconcile_libvirt_tuple` in
  `deploy/systemd/install-live-worker-lifecycle.sh` adopts an endpoint only when the live pid's
  uid equals `$operator_uid`, and the same installer creates `/var/lib/kdive/rootfs` as
  `operator:kdive-live-libvirt` mode `2770`. Root owns neither, so escalation bypasses the ownership
  gate rather than satisfying it.
- **Drop `sudo` from every call.** verified: `resolve_libvirt_uri` in
  `scripts/live-stack/libvirt-uri.sh` resolves `qemu:///system` when the contract file is absent,
  and that daemon is root-owned.
- **Restate the ownership model so root is correct** (#2516's second acceptance). judgment: it
  grants the reap a privilege the rest of the contract spends real effort withholding — worker
  accounts are kept out of `sudo` entirely.
- **Match the two published URIs exactly instead of classifying.** verified:
  `resolve_libvirt_uri` honours a caller-supplied `KDIVE_LIBVIRT_URI` without consulting
  `LIBVIRT_SOCKET_URIS`, so a plain `qemu:///session` reaches the reap and an exact-match list
  would escalate against it.
- **Ask libvirt which scope the URI names.** judgment: a round trip to the daemon whose
  reachability is the question under test, for something the URI string already states.
