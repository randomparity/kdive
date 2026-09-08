"""The module-staging host-tool contract shared by the install path and diagnostics (ADR-0635).

Module indexing runs the host's ``depmod`` against an extracted module tree (ADR-0346 §2). This
module owns *where* that binary is looked for, so the ``ops.diagnostics`` vantage reporting on the
requirement and the install path depending on it cannot name different directories.
"""

from __future__ import annotations

import os

DEPMOD = "depmod"
# depmod is an sbin tool — /usr/sbin under merged-usr, /sbin under split-usr. Resolution never
# consults PATH: the fixed live-worker gate execs the worker from an environment allowlist that
# omits it, so a bare name falls back to os.defpath (/bin:/usr/bin) and misses /usr/sbin, which
# reported a missing package on hosts that had one (#2300). These are the set
# ``src/kdive/jobs/capture_operations/bootstrap/bootstrap_elf.py`` resolves its own host tools
# against, and every one is root-owned. /usr/local/{sbin,bin} are left out on purpose even though
# an ungated worker reaches them through PATH today: /usr/local is group-writable by default on
# part of the Debian family, and a binary planted there would run as the worker slot account
# (User=kdive-worker-N, in kdive-live-libvirt), inheriting its authority over guest overlays.
# These four are the contract: no operator override for a depmod outside them (ADR-0631). A
# symlink into one of them is the supported answer, and its target must be root-owned too --
# which() takes the link and the exec follows it to the target's bytes.
DEPMOD_SEARCH_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
DEPMOD_SEARCH_PATH = os.pathsep.join(DEPMOD_SEARCH_DIRS)
