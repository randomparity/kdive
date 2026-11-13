# Remote bootstrap key survives an immediate activation

Date: 2026-11-13, 12:53-13:16 UTC. Tracks #3619. The bootstrap SSH key that the remote libvirt
provider writes into a guest through the guest agent was lost when the System was activated
within seconds of `ready`. With `sync -f` added to the injection script, the key survived.

## Candidate and topology

- Before the fix: every deployed role ran `155d84f4fbd8111c593862b19dd2d8e5787beba3` (main). The
  authority host's installed `bootstrap_key.py` had no `sync`.
- With the fix: every deployed role ran `57a3259d7e70605c98cd130d1e722d057350c3df` (this branch).
  The server, reconciler and worker `/readyz` reported that commit, both control-plane venvs and
  the authority host's installed package held the `sync -f` line, and the authority service was
  active.
- Control plane `sys-C`: Ubuntu 26.04 x86_64 lab guest, stack started with `stack-services.sh`,
  installed local authority from `installed-local-authority.yml`.
- Remote authority `sys-A`: Fedora 44 x86_64 lab guest, provisioned with `site.yml` (remote
  libvirt host plus `provider_authority_host`). Same lane as
  [the #3594 record](2026-11-13-remote-authority-ssh-port-3594.md).
- Root: the staged Fedora 44 base, SHA-256
  `599db3df7c18f78f370da4a0d9375d7208560f3b06fbe23761d8490b2d90bc83`. The guest root filesystem
  is btrfs. External-boot kernel: Linux v6.9 (`a38297e3fb01`) built on `sys-C`.

Each try ran one System at a time. The steps were `allocations.request`, then `systems.provision`
and a wait for `ready`, then a controlled delay, then activation (external boot of the v6.9
kernel), then `runs.release_external_boot`. After the release, the guest's
`/root/.ssh/authorized_keys` was read through the guest agent and compared with the public key in
`system_bootstrap_keys`. The System was then torn down and the allocation released. The guest
agent wrote the key about 2-3 seconds before `ready` was observed, so a 0 s delay means activation
started about 3 seconds after the write.

## Arms that ran

| Arm | Head | Delay after `ready` | Tries | Guest `authorized_keys` after release |
|---|---|---|---|---|
| 1: immediate, before the fix | main | 0 s | 3 | Missing in all 3. `/root/.ssh` existed with the image's original timestamp. |
| 2: delayed, before the fix | main | 45 s | 1 | Present but empty (0 bytes). The key was lost. |
| 2: delayed, before the fix | main | 90 s | 1 | Present, one line, equal to the stored bootstrap public key. |
| 3: immediate, with the fix | branch | 0 s | 3 | Present in all 3, one line, equal to the stored bootstrap public key. |

One earlier immediate try before the fix is not counted. The guest agent was not yet answering
after the release, so the key could not be read. The reader now waits for the agent.

## Findings

- The loss reproduces. Activation stops the domain with libvirt `destroy`
  (`remote_module_authority_host.py`, `stop_system`), and an unflushed append is lost.
- The issue's hypothesis named btrfs's 30 s commit interval. The 45 s arm shows that the file's
  creation can be committed while its data is not: the file survived empty. Delaying activation
  past the commit interval is not enough. 90 s was enough on this guest.
- `sync -f /root/.ssh/authorized_keys` ran on the Fedora 44 guest and exited 0. Every injection
  in arm 3 succeeded.

Not run: guests with an ext4 or xfs root, and an `authorize_ssh_key` login after the fixed
activation. The sync flushes the filesystem that holds the file whatever its type, so this record
does not depend on btrfs.

## Lab state afterwards

Both lab guests were rolled back to their pre-run snapshot and restarted, with no kdive checkout
present. The run's authority PKI keys, libvirt PKI keys and lab SSH key were shredded.
