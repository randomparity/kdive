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

## Consequences

- On a provisioned host the reap needs no root. The operator reaches the mode-`0770` socket and
  the mode-`2770` overlay directory as their **owner**: `install-live-worker-lifecycle.sh`
  installs the socket `operator_uid:group_gid:770` and `/var/lib/kdive/rootfs`
  `operator:kdive-live-libvirt` mode `2770`. `kdive-live-libvirt` membership is how the *worker*
  accounts reach the same two paths; both are access paths the contract publishes, and neither
  is root's.
- On a bare host the up-front `--wipe` gate now enumerates under `sudo`, so an operator who
  cannot escalate is refused before anything is stopped rather than mid-wipe. That moves the
  run's first escalation ahead of the irreversibility warning and the `Type 'wipe'` confirmation,
  so a host configured to prompt asks for the password before the operator has confirmed, and the
  refreshed sudo timestamp outlives an abort at the confirmation. Kept, but not because the
  ordering is forced: the confirmation block itself precedes every destructive step, so moving
  the gate below it would still refuse before anything is stopped. The reason to keep the gate
  first is narrower — refusing before asking spares the operator a confirmation on a run that
  cannot proceed, and `--wipe --yes` skips the prompt entirely, so the ordering only ever shows
  on the interactive bare-host path. Two alternatives were weighed and declined as
  confirmation-UX changes independent of this decision: moving the gate below the confirmation,
  and printing the irreversibility warning ahead of the gate while leaving the prompt where it
  is (which splits the warning from the confirmation it qualifies, and warns about
  irreversibility on runs the gate then refuses).
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
