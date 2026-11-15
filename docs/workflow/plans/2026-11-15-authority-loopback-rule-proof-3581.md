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

- No lab hostname, IP address, username, lab name or Proxmox VM id in any committed file, commit
  message, PR body or issue comment; hosts are written `sys-R3` (authority host) and `sys-R4`
  (control plane), continuing ADR-0810's `sys-R1`/`sys-R2` tokens.
- ADR-0810 is append-only: add one `### Amendment (2026-11-15): <title> (#3581)` block at the
  end of the amendments, before `## Considered & rejected`; change nothing else in the file.
- Do not change the rule text, the role, or `pki.yml` (operator exclusions).
- State only what ran. A check that did not run is named as not run.
- Avoid the 09:00Z–10:30Z Proxmox backup window for rollback.

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
1. Stage the lane from the private recipe (snapshot both guests first; PKI, bundle, backends,
   `site.yml` without then with the authority role, control-plane stack, staged base volume).
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
7. Watch `journalctl -u kdive-external-boot-authority.service` and `systemctl status` for 90 s.
   Record readiness errors and unit state ((b)). Then attempt a provision and teardown through
   the lane; record the outcome.
8. `nft delete table inet kdive_guest_loopback`; confirm the unit returns to `active`.
9. Tear down the System; roll both guests back to the snapshot; delete the snapshot.

Rollback: step 8 and step 9 restore the lab whatever the result.

## Task 2: Runbook and ADR amendment

Verification:
- Contract: runbook sentence and ADR amendment state the Task 1 result.
  Mode: task-test-not-applicable — prose; no executable consumer validates wording, and the
  repository's doc guardrails below cover links, paths and record shape.

Steps:
1. Replace the runbook sentence with the observed authority-host result and its cause, keeping
   "Do not assume" wording only for arrangements that did not run.
2. Append the ADR-0810 amendment: correction of the "unproven" limit, rule, method, results
   table (no rule / rule on the QEMU uid), limits found, what was not run.
3. Run `just docs-links`, `just docs-paths`, `just adr-status-check`, `just records`,
   `just lint`. Expected: each exits 0.
4. Commit `docs: record the loopback rule on a provider authority host (#3581)`.
