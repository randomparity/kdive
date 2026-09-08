---
title: qemu-guest-agent guest-exec fails ENOENT on a present executable after virt-customize --copy-in
date: 2026-06-13
tags: [environment-quirk, selinux, libvirt, virt-customize, qemu-guest-agent, remote-libvirt]
components: [deploy/remote-libvirt-guest-helpers/kdive-install-kernel, src/kdive/providers/remote_libvirt/lifecycle/install.py]
---

## Problem

Driving the remote-libvirt install plane, `runs.install` failed and the worker log showed:

```
libvirt: QEMU Driver error : internal error: unable to execute QEMU agent command
'guest-exec': Failed to execute child process "/usr/local/sbin/kdive-install-kernel"
(No such file or directory)
```

The helper had just been injected into the base image with:

```
virt-customize -a fedora-kdive-remote-base-43.qcow2 \
  --copy-in kdive-install-kernel:/usr/local/sbin/ \
  --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel'
```

`guestfish --ro` confirmed the file WAS present and looked correct:

```
-rwxr-xr-x 1 1000 1000 3810 ... kdive-install-kernel    # executable
#!/bin/bash                                              # clean shebang, no CRLF
/bin/bash  ->  is-file: true                             # interpreter present
```

So every obvious hypothesis was wrong: the file exists, is executable, has a valid shebang,
and the interpreter exists. `ENOENT` ("No such file or directory") for a present, executable
script is the misdirection.

## Diagnosis recorded at the time

The copied executable retained uid/gid `1000:1000` and a generic SELinux type. The follow-up
changed ownership and restored its label together, after which the helper launched. This
combined intervention supports investigating ownership and labels, but the record includes no
isolated-variable test or denial audit establishing SELinux alone as the cause of `ENOENT`.

## Solution

After `--copy-in`, set ownership to root and **relabel** the file:

```
virt-customize -a <base>.qcow2 \
  --copy-in kdive-install-kernel:/usr/local/sbin/ \
  --run-command 'chown root:root /usr/local/sbin/kdive-install-kernel' \
  --run-command 'chmod 0755 /usr/local/sbin/kdive-install-kernel' \
  --run-command 'restorecon -v /usr/local/sbin/kdive-install-kernel'
```

Verified by booting a throwaway domain from a fresh overlay of the relabeled base image and
running guest-exec exactly as kdive does:

```
virsh qemu-agent-command helper-test \
  '{"execute":"guest-exec","arguments":{"path":"/usr/local/sbin/kdive-install-kernel",
   "arg":["boot-id"],"capture-output":true}}'
=> {"return":{"pid":897}}      # success: the agent launched the helper
```

(Before the chown+restorecon, the same guest-exec returned the ENOENT failure above.)

## Current setup owner

This is the dated before/after experiment from 2026-06-13. Its commands describe that
experiment; they are not the current base-image installation procedure. Follow the
[guest-helper guide](../../../deploy/remote-libvirt-guest-helpers/README.md) and its Ansible
image-building owner for packages, ownership, labels, and guest confinement.
