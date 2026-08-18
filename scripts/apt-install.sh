#!/usr/bin/env bash
# Install apt packages on a CI runner under a hard per-call timeout and a bounded retry.
#
# The failure this exists for is a **stall, not a non-zero exit**. The `Install libvirt build
# headers` step wedged for 13 and 33 minutes on two runs in one afternoon (#1978) against a ~15s
# normal, and a human had to notice and cancel the job. `apt-get` never exited, so a
# retry-on-exit-status wrapper would not have fired on either occurrence. The hard `timeout` is
# the half that turns a stall into a failure; the retry is the half that makes that failure
# non-fatal.
#
# That pairing is what lets the budget below be tight enough to be useful. A timeout that fires
# on a slow-but-working mirror costs one retry, not a red check — both reported wedges recovered
# on a plain re-run with no code change. Sizing the budget for the slowest legitimate run instead
# would put the bound back above the wedge it is meant to catch.
#
# `apt-get` carries no verdict-versus-transport ambiguity for the retry to resolve — a non-zero
# exit can only mean the packages are not installed, never a finding to be re-run away — so a
# plain bounded retry is correct here, on the same reading of ADR-0553 that
# `pull-test-images.sh` uses. Attempts and backoff are deliberately identical to that script
# (ADR-0566).
#
# Usage: apt-install.sh PACKAGE [PACKAGE...]
set -euo pipefail

if (($# == 0)); then
  echo "usage: apt-install.sh PACKAGE [PACKAGE...]" >&2
  exit 2
fi

# Wall-clock ceiling for the install. `libvirt-dev` lands in ~15s and the slowest healthy step
# #1978 recorded was 83s for update and install together, so 60s is several times the observed
# cost and still an order of magnitude under the wedge. `live.yml` installs the ppc64le emulator
# and the libguestfs appliance — far more to fetch — and raises this rather than forcing one
# value to fit both.
readonly INSTALL_TIMEOUT_S="${KDIVE_APT_TIMEOUT_S:-60}"
# Validated, and not merely for tidiness: `timeout 0s` means *no limit*, so a single mistyped
# digit would silently restore the unbounded behaviour of #1978 while the log went on printing a
# budget. Rejecting anything but a positive integer also keeps the arithmetic below from
# evaluating an attacker- or typo-supplied string, which bash would expand rather than reject.
if [[ ! "$INSTALL_TIMEOUT_S" =~ ^[1-9][0-9]*$ ]]; then
  echo "apt-install: KDIVE_APT_TIMEOUT_S must be a positive whole number of seconds, got '${INSTALL_TIMEOUT_S}'" >&2
  exit 2
fi
# `apt-get update` fetches the repository indexes and costs the same whatever is installed
# afterwards, so it never needs the larger budget a large package set does — capping it keeps
# `live.yml`'s raised budget from inflating the worst case below. Taking the smaller of the two
# also keeps a *lowered* budget applying to both calls and to the repair, which is how the guard
# test drives this script.
#
# Worst case for a total outage, at the defaults: 3 attempts x (60s + 60s), plus up to 10s of
# SIGKILL grace per call, plus a bounded repair after each attempt, plus 20s of backoff — about
# 11 minutes, against the 360-minute Actions default. Every job-level timeout-minutes in the
# workflows that call this script is sized to contain that, so the step fails with the
# diagnostic below rather than being cut off mid-sentence.
readonly UPDATE_TIMEOUT_S=$((INSTALL_TIMEOUT_S < 60 ? INSTALL_TIMEOUT_S : 60))
# Grace between SIGTERM and SIGKILL. apt installs a SIGTERM handler and can be blocked in a
# socket read, which is precisely the state this script kills it in.
readonly KILL_AFTER_S=10

# One attempt per backoff entry, plus the first. Deriving ATTEMPTS keeps the two in step:
# raising it alone would index past the array, and `set -u` aborts on an unbound element.
# tests/guards/test_apt_install_is_bounded.py asserts both match `pull-test-images.sh`.
readonly BACKOFF_S=(5 15)
readonly ATTEMPTS=$((${#BACKOFF_S[@]} + 1))

readonly APT_OPTS=(
  # Fail a blackholed mirror inside apt, where the error names the host and IP, rather than
  # letting the outer timeout kill the process with nothing to read. The outer timeout stays as
  # the backstop: this bounds one connection, not the call.
  -o Acquire::http::Timeout=15
  -o Acquire::https::Timeout=15
  # This script owns the retry. apt's own retries would multiply into the budget above without
  # appearing in it, and would re-try inside a call the outer timeout is already counting down.
  # The cost is granularity: apt would have re-fetched a single flaky file in about a second,
  # where this script re-runs the whole attempt. Accepted for a budget that is legible from the
  # outside (ADR-0566).
  -o Acquire::Retries=0
  # Without this the timeout below cannot reach dpkg, and the whole change is decorative.
  # `timeout` signals its own process group; apt's default pty mode puts dpkg in a *new* group
  # and a new session (measured: apt-get pgid 1, dpkg pgid 988 sid 988), and dpkg ignores SIGHUP
  # (SigIgn 0x7), so the pty hangup does not reach it either. A budget that expired mid-unpack
  # therefore left an orphaned root dpkg holding /var/lib/dpkg/lock, and every retry then failed
  # instantly against that lock. `DPkg::Use-Pty=0` keeps dpkg in apt's group, where the kill
  # lands. The only thing lost is apt's pty progress rendering, which no CI log reads.
  -o DPkg::Use-Pty=0
  # apt's compiled default is 0 — fail immediately if the lock is held. A retry that races a
  # still-exiting dpkg from the attempt this script just killed would otherwise burn an attempt
  # on exit 100 rather than waiting the moment out.
  -o DPkg::Lock::Timeout=30
)

# The mirrors apt is *configured* to use — not, on its own, the mirror that failed. On a hosted
# runner this is a constant, so it is context for the failure rather than a diagnosis of it; the
# phase below is what discriminates a download stall from local unpack work overrunning the
# budget. Read out of apt's own resolved configuration so it covers both the deb822 (`.sources`)
# and legacy (`.list`) layouts, and bounded like every other apt call here: `indextargets` reads
# local configuration and should never block, but this runs on the failure path, right after the
# script SIGKILLed apt, and a diagnostic that hangs loses the diagnosis.
apt_mirrors() {
  local sites
  sites="$(timeout 5s apt-get indextargets --no-release-info 2>/dev/null |
    awk '$1 == "Site:" && $2 != "" { print $2 }' |
    sort -u |
    paste -sd' ' -)"
  printf '%s' "${sites:-<apt reported no configured sources>}"
}

# Clear a dpkg left mid-unpack by this script's own SIGKILL. Bounded, because it runs maintainer
# scripts (libvirt-daemon-system's postinst creates users and enables units) and this script's
# whole claim is that no call it makes is unbounded. Non-fatal: it is recovery from damage this
# script caused, and a database that is genuinely broken still fails the next attempt loudly.
repair_dpkg() {
  sudo timeout --kill-after="${KILL_AFTER_S}s" "${UPDATE_TIMEOUT_S}s" dpkg --configure -a ||
    echo "apt-install: dpkg --configure -a did not complete; the package database may still be broken" >&2
}

# `sudo timeout` and not `timeout sudo`: the timeout has to be the parent of `apt-get` and run as
# root. The other way round the signal lands on sudo, which does not reliably forward it to a
# child it exec()d, and the stall would outlive its own timeout.
run_apt() {
  local budget="$1"
  shift
  sudo timeout --kill-after="${KILL_AFTER_S}s" "${budget}s" apt-get "${APT_OPTS[@]}" "$@"
}

# `phase` names which apt call failed, for the diagnostic. Set before each call rather than
# inferred afterwards: by the time the status is read, the two are indistinguishable.
phase=update

attempt_once() {
  phase=update
  run_apt "$UPDATE_TIMEOUT_S" update || return
  phase=install
  run_apt "$INSTALL_TIMEOUT_S" install -y --no-install-recommends "$@"
}

for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
  status=0
  attempt_once "$@" || status=$?
  if ((status == 0)); then
    echo "apt-install: installed $# package(s): $*"
    exit 0
  fi

  # 124 is GNU timeout's "the command timed out"; 137 is 128+SIGKILL, what --kill-after leaves
  # behind when the TERM went unheeded. Both mean a stall, which reads very differently from a
  # mirror that answered with an error — and the stall is the one #1978 is about.
  reason="exited ${status}"
  if ((status == 124 || status == 137)); then
    reason="stalled and was killed"
  fi
  echo "apt-install: attempt ${attempt}/${ATTEMPTS}: apt-get ${phase} ${reason} while installing '$*' (budgets: ${UPDATE_TIMEOUT_S}s update, ${INSTALL_TIMEOUT_S}s install); configured mirrors: $(apt_mirrors)" >&2

  # After every failed attempt, including the last. A killed install leaves packages half
  # unpacked whether or not another attempt follows, and skipping the repair on exhaustion would
  # hand a developer running `just apt-install` a broken package database with no message.
  repair_dpkg

  if ((attempt < ATTEMPTS)); then
    delay="${BACKOFF_S[attempt - 1]}"
    echo "apt-install: retrying in ${delay}s" >&2
    sleep "$delay"
  fi
done

echo "::error::apt-install: could not install '$*' after ${ATTEMPTS} attempts (budgets: ${UPDATE_TIMEOUT_S}s update, ${INSTALL_TIMEOUT_S}s install); the last failure was apt-get ${phase}, so a stall in the install phase may be local unpack work rather than the network; configured mirrors: $(apt_mirrors). If this ran outside CI, check the package database with 'sudo dpkg --configure -a'." >&2
exit 1
