# 0393 — check-setup-deps.sh opt-in remediation

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->

## Context

`scripts/check-setup-deps.sh` reports missing host dependencies grouped by tier
with per-distro install hints, under a stated invariant: "reports only — never
installs and never escalates." On a fresh or failing host this forces the operator
to copy-paste each `apt install …` line by hand, and for the libguestfs binding to
additionally perform the manual venv symlink documented in
`docs/operating/runbooks/four-method-live-run.md` §4b (a uv venv has no
system-site-packages, so a system-installed `python3-guestfs` is not importable by
the worker's venv interpreter — see #1328). The script already computes everything a
fix needs: the exact install command per tier and the guestfs import probe.

The cross-arch advisory reports only foreign (TCG) guest arches and skips the host
arch, so which arch runs natively and under what conditions is not stated.

## Decision

Replace the "never installs, never escalates" invariant with **opt-in remediation**:

- Fixes run only on an explicit signal — an interactive per-tier `[y/N]` prompt
  (default No), or `-y`/`--yes` for scripted use. **When stdin is not a TTY and `-y`
  is absent, the script does not prompt and does not fix — behavior identical to
  today.** This non-interactive default preserves every existing caller (CI, tests).
- An accepted package install runs a **non-interactive** install command with an index
  refresh on fresh hosts (`apt-get update && apt-get install -y` / `dnf install -y` /
  `pacman -S --noconfirm` / `zypper --non-interactive install`) — not the human-facing
  hint, which would itself prompt. Arch deliberately omits the `-Sy` refresh (see
  Considered & rejected). Escalation is scoped to the mode: an **interactive**
  accept uses plain `sudo` (a password prompt is desired when a human just consented),
  while `-y`/non-TTY uses `sudo -n` and never blocks. A credential pre-flight (`sudo -v`
  interactive / `sudo -n true` non-interactive) runs first, so an escalation failure
  (skip with "run as root or passwordless sudo") is reported distinctly from a package
  failure. A `-y` provisioning caller is expected to run as root or with passwordless
  sudo. Each step is guarded so a failure does not abort the run under `set -euo
  pipefail`; the Required tier's exit code reflects the post-fix state. Escalation is
  confined to this accepted-install path.
- Only distro-package tiers and the guestfs symlink are auto-fixable. Manual-hint
  tooling (`uv`/rustup/`just`/`prek`, installed via `curl … | sh` or `uv tool install`)
  is never auto-run, even under `-y` — an unprompted piped-shell installer is a
  supply-chain surprise this opt-in design rejects; those remain report-only.
- The guestfs venv symlink is offered as a **separate** prompt; it needs no sudo, is
  ABI-checked (system and venv Python minor versions must match — fail loud, never a
  broken link), and is skipped when the venv does not yet exist.

The advisory identifies the host arch first, then each supported guest arch (host/
native first, then foreign) with its acceleration — native KVM (probing `/dev/kvm`
via `KDIVE_KVM_NODE`) vs TCG — and any missing package. Foreign-arch line wording is
unchanged; the host/native line is additive.

`check-local-libvirt.sh` and the Ansible roles remain the authoritative live-host
gate; this stays a setup-time convenience.

## Consequences

- A single command can bootstrap a host; `-y` makes it usable from provisioning
  scripts. The report-only contract is no longer absolute, so the header and any
  docs asserting it are updated.
- `sudo` may now be invoked, but only after an explicit human accept or `-y`.
- The guestfs fix keeps the venv isolated (symlink, not `--system-site-packages`),
  consistent with the runbook's recommended option and the Ansible role.

## Considered & rejected

- **A separate `--fix`/`setup-deps.sh` script.** More surface for the same logic the
  reporter already holds; the issue explicitly asked to fix from within the checker.
- **Auto-fix by default (no flag).** Silent escalation on a report command is
  surprising and unsafe for CI; opt-in is the safe default.
- **`uv venv --system-site-packages` as the guestfs remedy.** Simpler but exposes the
  venv to every system package, weakening the pinned/reproducible dependency set;
  kept only as the documented alternative in the runbook.
- **Arch `pacman -Sy <pkg>` (refresh db, install named only).** Rejected: on a
  non-fresh host it leaves the "partial upgrade" state Arch declares unsupported (the
  package may need newer libs than are on disk). `pacman -Syu` was also rejected — an
  unattended dependency check must not upgrade the whole system. Arch therefore uses
  plain `pacman -S` and, on a stale-db failure, tells the operator to run `pacman -Syu`.
