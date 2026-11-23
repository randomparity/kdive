# Supervisor-owned Compose lifecycle lock

## Problem

The Compose wrapper opens its predictable `/tmp` lock with `Path.open("a+")`.
It follows symlinks and does not verify the inode before relying on `flock`.
A private-directory reproduction accepted a symlink to an unrelated regular file.
The same module already validates credential descriptors by owner, file type,
link count and `O_NOFOLLOW`; use that existing boundary for the lock too.

## Scope and design

Generalize the existing private credential opener to a supervisor-owned file opener,
with an explicit diagnostic subject. Migrate its three credential callers while
preserving their exact error text and flags. Keep mode 0600 creation and CLOEXEC,
NOFOLLOW, owner, regular-file and single-link checks. Close the descriptor on failed
metadata validation, including an exception from `fstat`.
The lock uses this opener with read/write, create and nonblocking flags, then wraps
its descriptor with `os.fdopen` and takes the existing nonblocking exclusive flock.
The nonblocking open prevents an untrusted FIFO from hanging before type validation.
Do not truncate, chmod, unlink or replace an existing lock; preserve its pathname,
project validation, contention diagnostic, critical section and release ordering.

## Success

1. Unsafe lock inode categories named above cannot enter the critical section.
2. A safe existing lock and newly created 0600 lock serialize the same project.
3. Exceptions inside the critical section release the lock; failed validation closes
   an opened descriptor; credential consumers retain their existing checks/messages.

## Failure model

- Actors and deployments: a local operator runs the reference Linux Compose wrapper;
  other local accounts can create names in the shared sticky `/tmp` directory.
- Invariants and assets: project operations share one supervisor-owned regular inode;
  lifecycle work does not start through an unsafe inode; credentials retain protection.
- Accepted failure classes: a foreign pre-created file can deny service, with explicit
  refusal rather than automatic deletion; root or a compromised supervisor UID is trusted.
- Covered elsewhere: worker-incarnation fencing and teardown proof remain the existing
  lifecycle gate/database contracts; Docker availability remains operator-managed.

## Threat model

- Boundary inventory: existing shared-filesystem pathname to supervisor lock descriptor;
  no new/widened network or credential boundary. The credential opener is reused.
- Actor model: another local UID may pre-create a lock name; kernel `/tmp` sticky-directory
  ownership prevents that UID unlinking the supervisor-owned inode after validation.
- Controls: NOFOLLOW rejects links; nonblocking open avoids FIFO hangs; fstat validates
  regular type, current UID and one link. Mode0600 creation/CLOEXEC constrain new descriptors.
  Validation failure exposes a bounded operation diagnostic, never file contents.
- Out of scope: malicious root/same-UID mutation and unavailable Docker, for the reasons
  in the failure model. Do not change host sysctls or repair foreign-owned files.

## Validation

Use the existing Compose lifecycle test module and private temporary directories.
Observe red for symlink, hardlink and safe0600creation before implementation; add FIFO
and foreign-owner metadata cases with deterministic bounded IO, descriptor cleanup,
normal/reentrant contention and release after an exception. The foreign-owner check may
inject fstat metadata at its OS boundary; no host ownership changes are required.
Prove new checks bite with controlled faults, restore source, then run the focused
module, whole-tree lint/types and configured hooks. Existing credential tests remain
selected. Full local gate follows final source changes; no live Compose deployment or
external state mutation is required to prove filesystem-lock behavior.
