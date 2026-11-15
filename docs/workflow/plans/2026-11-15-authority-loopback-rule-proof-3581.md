# Guest-loopback rule on a provider authority host (#3581) — plan

Goal: record whether the runbook's `inet kdive_guest_loopback` rule, keyed to the uid QEMU runs
as on a `provider_authority_host`, blocks an egress guest's loopback connection and leaves the
authority service working, then update the runbook and ADR-0810 to match.

Architecture: no code change. A private lab lane (control plane plus one Fedora 44 authority host
provisioned by `deploy/ansible/site.yml`) supplies the evidence; two documents record it.

Tech stack: Ansible (`ansible-core==2.21.1` through `uv run --with`), nftables, libvirt session
daemon, QEMU guest agent, Markdown.

Spec: [design](../specs/2026-11-15-authority-loopback-rule-proof-3581-design.md).

Expected implementation size: 40–90 changed lines (M) — one runbook paragraph rewrite (~10 lines)
plus one appended ADR amendment with a results table (~30–80 lines).

## Global Constraints

- No lab hostname, FQDN or domain suffix, IP address, username, lab name or VM id in any
  committed file, commit message, PR body or issue comment; quote journal lines as message text
  only (`journalctl -o cat`); hosts are written `sys-R3` (authority host) and `sys-R4`
  (control plane), continuing ADR-0810's `sys-R1`/`sys-R2` tokens.
- ADR-0810 is append-only: add one `### Amendment (2026-11-15): <title> (#3581)` block at the
  end of the amendments, before `## Considered & rejected`; change nothing else in the file.
- Do not change the rule text, the role, or `pki.yml` (operator exclusions).
- State only what ran. A check that did not run is named as not run.

## File map

- `docs/operating/runbooks/image-lifecycle.md` — owns operator guidance for the rule; the
  sentence beginning "An authority host, where one account owns the session daemon" changes.
- `docs/adr/0810-guest-egress-host-loopback-exposure-is-accepted-and-documented.md` — owns the
  decision record; gains one amendment.

## Task 1: Live proof

Verification:
- Contract: outcomes (a) and (b) observed on a `provider_authority_host`.
  Mode: task-test-not-applicable — the deliverable is a host observation recorded as prose; no
  repository test can exercise a lab host's nftables state.

Steps:
1. Snapshot both guests, then stage the lane: libvirt and authority PKI, a bundle of the
   candidate, control-plane backends, `site.yml` without then with `provider_authority_host`
   (network listener enabled), the control-plane stack, and a staged base volume.
   Expected: `systemctl is-active kdive-external-boot-authority.service` prints `active` and the
   control-plane `/readyz` endpoints report ready.
2. Provision one authority System (manifest entry has `guest_egress: true`) and leave it running.
   Expected: System `ready`; one `qemu-kvm` process.
3. On the authority host: `ps -eo uid=,user=,comm= | grep -E 'qemu-(system|kvm)'` and
   `systemctl show -p MainPID kdive-external-boot-authority.service` then `ps -o uid= -p <pid>`.
   Expected: one uid, the authority account's.
4. Start a host listener on `127.0.0.1:<port>` (e.g. `python3 -m http.server --bind 127.0.0.1`);
   from the guest, via `virsh qemu-agent-command ... guest-exec` as the authority account, run
   `timeout 5 bash -c '</dev/tcp/10.0.2.2/<port>'`. Expected baseline: exit 0.
5. Write the runbook rule with `<qemu-uid>` and the resolver from `/etc/resolv.conf`; `nft -f`.
6. Repeat step 4. Record exit status ((a)).
7. For at least 90 s, record `journalctl -o cat -u kdive-external-boot-authority.service` and
   `systemctl show -p ActiveState,SubState,Result,NRestarts kdive-external-boot-authority.service`
   ((b)). Then attempt a provision and teardown through the lane; record the outcome. The unit's
   `StartLimitBurst=5` in 600 s may leave it `failed` with `Result=start-limit-hit`.
8. `nft delete table inet kdive_guest_loopback`. If the unit is `failed`, record its state, run
   `systemctl reset-failed kdive-external-boot-authority.service` and `systemctl start` it, then
   record whether it reaches `active` and ready.
9. Only if step 8 left the authority serving, tear down the System through the lane. Then roll
   both guests back to the snapshot and delete the snapshot.

Rollback: the snapshot rollback in step 9 restores the lab whatever the result; the lane teardown
before it is best effort.

## Task 2: Runbook and ADR amendment

Verification:
- Contract: runbook sentence and ADR amendment state the Task 1 result.
  Mode: task-test-not-applicable — prose; no executable consumer validates wording, and the
  repository's doc guardrails below cover links, paths and record shape.

Steps:
1. Replace the runbook sentence with the observed authority-host result and its cause, keeping
   "Do not assume" wording only for arrangements that did not run. A unit left `failed` by the
   start limit is attributed to the start limit, with the recovery commands.
2. Append the ADR-0810 amendment: correction of the "unproven" limit, rule, method, results
   table (no rule / rule on the QEMU uid), limits found, what was not run.
3. Scan the diff, commit message and planned PR/issue text for the authority host's short name,
   FQDN, domain suffix and addresses (`rg -n`); expected: no match.
4. Run `just docs-links`, `just docs-paths`, `just adr-status-check`, `just records`,
   `just lint`. Expected: each exits 0.
5. Commit `docs: record the loopback rule on a provider authority host (#3581)`.
