# check-setup-deps.sh: opt-in auto-fix + native arch visibility

Date: 2026-07-20
Status: approved (brainstorm), pending implementation
ADR: [0393](../../adr/0393-check-setup-deps-opt-in-remediation.md)

## Problem

`scripts/check-setup-deps.sh` is report-only ("never installs, never escalates").
An operator on a fresh/failing host must copy-paste the printed `apt install …`
lines by hand, and — for the libguestfs binding — additionally perform the manual
venv symlink documented in `docs/operating/runbooks/four-method-live-run.md` §4b.
Separately, the cross-arch advisory only reports foreign (TCG) arches and skips the
host arch, so it is not obvious which arch runs natively or under what conditions.

## Goals

1. Let the script optionally remediate what it already detects, using data it
   already computes — without breaking its non-interactive/report-only behavior.
2. Show the full provisionable-arch matrix: host arch identified first, then each
   guest arch with its acceleration (native KVM vs TCG) and any missing package.

## Non-goals

- `check-local-libvirt.sh` / Ansible remain the authoritative live-host gate. This
  script stays a setup-time convenience; it does not replace them.
- No new dependency, no config surface beyond the one flag.

## Design

### Contract change (intentional)

The header's "reports only, never installs, never escalates" invariant is replaced
by: **reports by default; remediates only on an explicit per-tier opt-in (interactive
prompt) or `-y`.** Escalation (`sudo`) happens only for an accepted package install.

### Interaction model

| stdin | flag | behavior |
|-------|------|----------|
| TTY | none | after each tier's report, prompt `Fix now? [y/N]` (default No) |
| any | `-y`/`--yes` | auto-accept every offer, no prompts (scripts / `just setup`) |
| non-TTY | none | **no prompt, no fix — identical to today** (protects CI + existing tests) |

Rationale: the non-TTY-without-`-y` default is what keeps every existing behavioral
test and CI invocation unchanged.

### Fix 1 — distro packages (per tier)

Each tier already accumulates its install package list. On accept, run a
**non-interactive** install command — **not** the human-facing hint verbatim (which
omits the auto-confirm flag and would itself prompt on a piped stdin):

| distro | install command |
|--------|-----------------|
| debian | `apt-get install -y <pkgs>` (`apt-get`, not `apt`, for scripting) |
| fedora | `dnf install -y <pkgs>` |
| arch | `pacman -S --noconfirm <pkgs>` |
| opensuse | `zypper --non-interactive install <pkgs>` |
| unknown | no auto-install — report the manual command and skip |

**Escalation:** when `EUID≠0`, prefix `sudo -n` (non-interactive — never block on a
password prompt). If `sudo` is absent, or `sudo -n` fails (no cached creds / NOPASSWD /
`requiretty`), do **not** hang: emit an actionable message ("re-run as root or with
passwordless sudo to install: `<cmd>`") and skip that tier's fix. A `-y` provisioning
caller is therefore expected to run as root or with NOPASSWD sudo — stated in the
script header and the ADR.

**Failure handling under `set -euo pipefail`:** guard every install (`if ! <cmd>;
then report+continue; fi`) so a failed install (package absent on this distro/version,
network, disk) does not abort the whole script. Report which package set failed,
continue to remaining tiers and the advisory, and preserve a non-zero exit when the
Required tier is still unsatisfied at the end.

Separate accept per tier (Required / Recommended / Future) so an operator can install
required deps and decline live-only ones.

### Fix 2 — guestfs venv link (separate prompt, after Fix 1)

The guestfs future-tier probe tests `"${PY}" -c "import guestfs"`. The symlink source
files exist only once `python3-guestfs` is installed, so this fix is evaluated **after**
the Future-tier package install (re-probe), and offered only when the binding is present
system-wide but still not importable in the venv. On accept it does the ABI-checked
symlink of `guestfs.py` + `libguestfsmod*.so` into the venv site-packages (same operation
as the runbook §4b / Ansible role). No sudo.

- **Source discovery:** locate the binding at `/usr/lib/python3/dist-packages` (Debian
  dpkg path), falling back to the owning interpreter's `purelib` (Fedora) — the exact
  logic in runbook §4b. If zero `guestfs.py` matches → report "binding not found, is
  python3-guestfs installed?" and skip. If multiple `libguestfsmod*.so` match, link all.
- **Owning interpreter / ABI check:** the binding is built for the **system**
  interpreter (`/usr/bin/python3`, as the Ansible role pins). Compare its `X.Y` minor
  version against the venv `${PY}`'s; on mismatch, fail loud (report the two versions)
  and do **not** create a broken link.
- **Idempotency:** link with `ln -sf` (or skip when the correct link already exists) so a
  re-run after a partial prior attempt does not abort on "File exists".
- **Skipped** when the venv does not exist yet (e.g. first `just setup` before `uv sync`).

### After fixing

Re-verification re-runs the affected tier's probes after `hash -r` (bash caches
command lookups, so a just-installed binary is otherwise not found — a false "still
missing"). Re-run all three probe kinds a tier uses: PATH command (`command_exists`),
`pkg-config --exists`, and the venv import. Exit `0` if the Required tier is now
satisfied (today it exits `1` on missing Required deps); otherwise exit `1`.

### Native arch visibility

The advisory leads with a positive host-arch line, then lists each supported guest
arch — host/native arch first, then foreign — with its condition. Probes `/dev/kvm`
via `KDIVE_KVM_NODE` (as `check-local-libvirt.sh` does). Existing foreign-arch line
wording is unchanged (additive), so current assertions hold.

```
Host architecture: x86_64 (supported kdive provisioning arch)
  guest arch x86_64:  available natively via qemu-system-x86_64 (/dev/kvm accessible — KVM)
  guest arch ppc64le: available via TCG only (qemu-system-ppc64)
```

The native line reports only what the `/dev/kvm` probe proves (device accessible), not
that KVM will accelerate — CPU virt flags / nested-virt can still fail, which
`check-local-libvirt.sh` (the authoritative gate) validates. Native-line variants:
native emulator present but `/dev/kvm` inaccessible → "native emulator present, `/dev/kvm`
not accessible — runs under TCG until KVM is enabled"; native emulator absent → "not
available; install <pkg> for native guests". Unsupported host arch keeps today's single
explanatory line.

## Testing

Stub tests verify **command invocation and output wording**, not a real host bootstrap
(that is a manual/live step the ADR notes). Concretely:

- **non-TTY default:** with no `-y` and no TTY, assert **no** install/symlink/sudo ran —
  the report-only contract every existing test and CI run depends on.
- **`-y` package install:** `apt-get`/`dnf`/`sudo`/python stubs → assert the executed
  command carries the non-interactive flag (`-y`/`--noconfirm`/`--non-interactive`) and
  `sudo -n` when non-root; a sudo-that-fails stub → assert the actionable skip message,
  no hang.
- **install failure:** an install stub exiting non-zero → assert the script does not
  abort, reports the failed set, and exits non-zero when Required stays unsatisfied.
- **re-verify:** a binary stub that appears only after the install stub runs → assert
  exit `0` (proves `hash -r` + re-probe).
- **guestfs link:** ordered after install in one `-y` run (install then link);
  ABI-mismatch → fail loud, no symlink; pre-existing link → no abort (idempotent);
  glob-miss → skip with message; venv absent → skip.
- **native advisory:** exact host-first line ordering and per-arch wording for
  KVM-present, native-qemu-absent, and `/dev/kvm`-inaccessible (via `KDIVE_KVM_NODE`);
  existing foreign-line assertions unchanged.

## Commits

1. Native arch visibility in the advisory (host-first matrix).
2. Opt-in `-y`/interactive package install (per tier, sudo when non-root).
3. Opt-in guestfs venv-symlink fix (separate prompt, ABI-checked).
