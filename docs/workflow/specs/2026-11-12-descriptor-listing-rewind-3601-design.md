# Descriptor directory listings rewind before reading (#3601)

## Problem

Provider and capture code lists directories through directory descriptors with
`os.listdir(<fd>)` and decides on the result: refuse a second target projection, clean
temporaries, collect or finalize remote store files, bound a scan. CPython's `os.listdir(fd)`
duplicates the descriptor (the duplicate shares the file offset), reads, and rewinds only
*after* reading. On btrfs a directory descriptor's visible index range is fixed when it is
opened and refreshed on each seek, so an entry created after the descriptor was opened or last
rewound is missing from the next listing. tmpfs and ext4 return it. With `TMPDIR` on btrfs, 22 tests in three files fail on `main`.

## Scope

- New module `kdive.providers.shared.directory_listing` with one function:

  ```python
  def list_directory_descriptor(descriptor: int) -> list[str]:
      os.lseek(descriptor, 0, os.SEEK_SET)
      return os.listdir(descriptor)
  ```

  The seek runs immediately before the read, so the listing includes every entry that exists
  when the listing starts, on btrfs as on ext4 and tmpfs. Its errors are the `OSError`s of
  `lseek` and `listdir`, unchanged; callers keep their existing exception handling.
- Replace the call at each descriptor listing site below, with no other change to the site:
  `local_libvirt/lifecycle/boot/session.py` (projection refusal), `session_mechanisms.py`
  (projection residue), `external_boot.py` (five sites), `remote_libvirt/external_boot_authority.py`
  (three `self._root_fd` sites), `remote_libvirt/system_authority.py` (`_scan`),
  `external_boot_authority/host.py` (journal lanes), `system_authority/journal.py` (the
  `system-operations` listing and `_check_evidence_bounds`),
  `jobs/capture_operations/storage/spool.py` (`_remove_spool_entries`).
- Excluded (operator approval 2026-11-12, amended 2026-11-13):
  `jobs/handlers/artifacts/rootfs_reclaim.py` `_live_writer_holds_a_partial` scans a path, stays
  unchanged, and is the guard's one allowlist entry. The 2026-11-12 exclusion of `journal.py`
  `_check_evidence_bounds` was withdrawn on 2026-11-13 (it lists a descriptor), so it routes
  through the helper and the guard exempts no descriptor.
- Guard test `tests/guards/test_directory_listing_rewinds.py`: an AST scan of `src/kdive`
  fails on any `os.listdir`/`os.scandir` call (attribute form or a name imported from `os`)
  outside the helper module, except the allowlisted `(file, enclosing function)` pair. An
  allowlist entry that no longer matches a call also fails, so the list cannot go stale.
- Test helper `_store_names` in `tests/providers/remote_libvirt/test_external_boot_authority.py`
  uses the helper, so remote store assertions do not under-report on btrfs.
- No ownership transition: a new shared helper consumed by existing owners. No ADR: the one
  choice (seek versus reopening `"."`) is local to a private helper; the rejected option is
  recorded here.
  - **Reopen `"."` through `dir_fd` and list the new descriptor.** judgment: one more open and
    close per listing, and a new failure mode (`EACCES` if the directory mode changed after the
    first open) at confinement and cleanup sites, to avoid moving an offset that CPython
    already moves on every `os.listdir(fd)`.

## Failure model

1. Actors and deployments: the worker and authority-host processes on a Linux host whose
   state directories are on ext4, tmpfs, XFS or btrfs (Fedora's default root filesystem).
2. Invariants and assets at stake: projection confinement (one target projection per
   activation); destructive cleanup that must refuse unknown entries; remote store finalize
   and collection that must see every index, materialization and preparation file; the
   remote store scan bound.
3. Accepted failure classes:
   - An entry created by another process between the seek and the end of the read may or may
     not be listed. POSIX allows it, and the sites already treat a concurrent writer through
     their own locks and fences; the guarantee is entries present when the listing starts.
   - The seek moves the shared offset of the listed descriptor. `os.listdir(fd)` already
     reads from and rewinds that same shared offset, so a concurrent reader of one descriptor
     was already unsafe; no site iterates a descriptor while listing it.
   - On btrfs, listings now include entries they missed, so cleanup removes owned temporaries
     it left behind before, and refusal and scan-bound checks fire where they did not. That is
     the intended outcome; ext4 and tmpfs outcomes are unchanged.
4. Covered elsewhere: directory `nlink` semantics on btrfs in slot diagnostics (#2909); a CI
   runner with a btrfs filesystem (out of scope, CI runners use ext4).

## Success

1. With `TMPDIR` on btrfs, `test_session.py`, `test_materializer.py` and
   `test_external_boot_authority.py` (the 22 failures on `main`) pass, and they still pass with
   the default `TMPDIR`.
2. `rg -n "os\.(listdir|scandir)\(" src/` lists only the helper module and the excluded
   `rootfs_reclaim.py` path scan.
3. The guard fails when a call written as `os.listdir(...)`, `os.scandir(...)`, or a name imported
   from `os` for either is added to a `src/kdive` module outside the allowlisted functions. An
   aliased module (`import os as x`), `posix.listdir`, or a stored function reference is not
   detected and is left to code review.

## Validation

- Helper — `Mode: focused-test`. `tests/providers/shared/test_directory_listing.py`: advance a
  descriptor's shared offset to the end by starting an `os.scandir(fd)` iteration, then assert
  the helper returns every entry (a bare `os.listdir(fd)` returns `[]` there on every
  filesystem, so the test bites on ext4 and tmpfs CI). Red: the helper without the seek returns
  `[]`. Green: `uv run python -m pytest tests/providers/shared/test_directory_listing.py -q`.
- Site routing — `Mode: focused-test`. Proof tier: the ordinary suite with `TMPDIR` on btrfs
  (local; CI cannot run it). Red on `main`: 22 failed in the three files above. Green after.
- Guard — `Mode: focused-test`. Red: inject `os.listdir(fd)` into a scanned module, or remove
  an allowlisted call, and observe failure; revert. Green:
  `uv run python -m pytest tests/guards/test_directory_listing_rewinds.py -q`.
- Test helper `_store_names` — `Mode: task-test-not-applicable`: test-support code; its
  correctness is observed only through the btrfs run of `test_external_boot_authority.py`
  covered by the site-routing entry.
