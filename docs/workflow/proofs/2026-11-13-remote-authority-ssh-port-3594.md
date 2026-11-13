# Remote authority System SSH port

Date: 2026-11-13, 04:16-04:25 UTC. Tracks #3594 (ADR-0815 follow-up 3). A remote authority System
reported its SSH port, and `systems.authorize_ssh_key` then `systems.check_ssh_reachable` worked
against it through the public MCP tools.

## Candidate and topology

Every deployed role ran `aea536906a86c8efec8b931176c1a5606b8724d0`, which contains #3608 (the
authority reports the port) and #3609 (the server and worker use it). The server, reconciler and
worker `/readyz` reported that commit. The authority host's installed package held the #3586
`ssh_forward_port=intent.ssh_port` line, and both control-plane venvs held the #3585
`authority_ssh_forward_invalid` check.

- Control plane `sys-C`: Ubuntu 26.04 x86_64 lab guest, stack started with `stack-services.sh`,
  one lifecycle-managed worker, installed local authority from `installed-local-authority.yml`.
- Remote authority `sys-A`: Fedora 44 x86_64 lab guest, provisioned with `site.yml` (remote
  libvirt host plus `provider_authority_host`).
- Manifest entry: SSH range 48000-48099 on `sys-A`'s address. Inventory `[[remote_libvirt]]`
  entry: `ssh_addr` set to the same address, `ssh_range = "49000:49099"`, which is deliberately
  disjoint from the manifest range.
- Root: the staged Fedora 44 base, SHA-256
  `599db3df7c18f78f370da4a0d9375d7208560f3b06fbe23761d8490b2d90bc83`. External-boot kernel:
  Linux v6.9 (`a38297e3fb01`) built on `sys-C`.

The driver called the tools over HTTP with a project `admin` token minted by the stack's mock
issuer. It read `authority_system_ownership` and `jobs` with the harness database role.

## Arms that ran

| Arm | Observation |
|---|---|
| Provision (Systems A and B) | `systems.provision` job `succeeded`; System `ready`; ownership `ready`, `ssh_forward_port` 48000. |
| Port source | `systems.ssh_info` answered `sys-A`:48000 from the first provision. The port is in the manifest range, outside the instance `ssh_range`. |
| Fence before first activation | `systems.authorize_ssh_key`: `conflict`, `authority_system_preactivation_mutation_fenced`. `systems.check_ssh_reachable`: `configuration_error`, same reason. |
| Activation | External boot of the v6.9 kernel; ownership `activated`. |
| Authorize during activation | `systems.authorize_ssh_key`: `conflict`, `external_boot_restricted`. |
| Firewall closed (System A) | Host firewall had no rule for 48000-48099. `check_ssh_reachable` job `succeeded` with `reachable: false`, `detail: unreachable`, `layer: tcp_connect`. A source-restricted rich rule for the range from `sys-C` fixed it. |
| Authorize after release (System B) | After `runs.release_external_boot` succeeded, the `authorize_ssh_key` job `succeeded`. |
| Reachability (System B) | `check_ssh_reachable` job `succeeded`, `reachable: true`, TCP connect and SSH banner both ok. |
| Agent login (System B) | `ssh -p 48000 root@sys-A` with the authorized key ran `uname -r` (`6.19.10-300.fc44.x86_64`) and `id -un` (`root`). |
| Wrong instance `ssh_addr` (System B) | Inventory `ssh_addr` pointed at `sys-C`, stack processes restarted with `stack-services.sh`. `ssh_info` answered `sys-C`:48000. `authorize_ssh_key` job failed `transport_failure`, `guest SSH endpoint is unreachable; not attempting the authorized-keys append`. `check_ssh_reachable` job `succeeded` with `reachable: false`, `layer: tcp_connect`. |
| Restored `ssh_addr` (System B) | Inventory restored and processes restarted; `check_ssh_reachable` `reachable: true`. |
| Cleanup | Both Systems `torn_down`, ownership `torn-down`, allocations `released`. The authority daemon listed no domains, its pool held only the base volume, and nothing listened in 48000-48099. |

Not run: a port collision between ranges, and a System provisioned before #3586. Their documented
behaviour comes from ADR-0815, not from this run.

## Differences from ADR-0815's wording

ADR-0815 says a wrong address shows up as `systems.check_ssh_reachable` failing with
`transport_failure`. The probe job instead succeeds with an unreachable verdict;
`systems.authorize_ssh_key` is the tool that fails `transport_failure`. The runbooks state the
observed behaviour.

## Other observations

- System A's bootstrap key was missing from the guest after its activation and release:
  `authorize_ssh_key` failed `transport_failure` with `auth_rejected`, and the guest had no
  `/root/.ssh/authorized_keys`. System A was activated about one second after `ready`. System B
  waited about 60 seconds before activation; its key was present before and after, and every arm
  passed. The guest root is btrfs and the guest-agent injection script does not sync. This is a
  hypothesis from two Systems, not a proven cause.
- After System A's failed authentications, guest sshd applied its source penalty, and the next
  `check_ssh_reachable` reported `detail: no SSH banner` at `layer: ssh_banner`.

## Lab state afterwards

Both lab guests were rolled back to their pre-run snapshot. The run's authority PKI keys and the
lab SSH key were shredded.
