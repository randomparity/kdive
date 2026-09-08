# Historical Ansible recovery-root symlink experiment

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../../README.md).

Record date: 2026-09-03. Issue: [#2210](https://github.com/randomparity/kdive/issues/2210).
Decision: [ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md).

The recorded experiment ran `ansible.builtin.file` with `state: directory` over a
symlink to a directory whose mode was `0755`. With the default `follow: true`,
the operation succeeded and changed the target mode to `0700`. With
`follow: false`, the module failed with "already exists as a link" and wrote nothing.

A separate `stat`/assert before creation detected an already-wrong layout but did
not make the later creation atomic. The file task's own refusal closed that
separate window. Reaching it required write access to the root-owned parent;
the experiment did not establish an exploit available to the modelled actor.

Configuration-time validation likewise did not protect a later open, so the stores
still checked at use with `O_NOFOLLOW`. A root-equivalent local actor was outside
the threat model.

The current provisioning owner is the
[live VM host role](../../../deploy/ansible/roles/live_vm_host/tasks/main.yml).
