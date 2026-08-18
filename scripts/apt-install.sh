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
# `apt-get update` fetches the repository indexes and costs the same whatever is installed
# afterwards, so it never needs the larger budget a large package set does — capping it keeps
# `live.yml`'s raised budget from tripling the worst case for a total outage (3 x 2 x 300s would
# land outside that job's own timeout-minutes, which would swallow the diagnostic below). Taking
# the smaller of the two also keeps a *lowered* budget applying to both calls, which is how the
# guard test drives this script.
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
  -o Acquire::Retries=0
)

# The mirrors apt is configured to use, for the failure line. Read out of apt's own resolved
# configuration so it covers both the deb822 (`.sources`) and legacy (`.list`) layouts, and
# local-only — it parses configuration and never touches the network, which is what makes it
# usable at the exact moment the network is wedged.
apt_mirrors() {
  local sites
  sites="$(apt-get indextargets --no-release-info 2>/dev/null |
    awk '$1 == "Site:" && $2 != "" { print $2 }' |
    sort -u |
    paste -sd' ' -)"
  printf '%s' "${sites:-<apt reported no configured sources>}"
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
  echo "apt-install: attempt ${attempt}/${ATTEMPTS}: apt-get ${phase} ${reason} while installing '$*' (budgets: ${UPDATE_TIMEOUT_S}s update, ${INSTALL_TIMEOUT_S}s install); apt mirrors: $(apt_mirrors)" >&2

  if ((attempt < ATTEMPTS)); then
    delay="${BACKOFF_S[attempt - 1]}"
    echo "apt-install: retrying in ${delay}s" >&2
    sleep "$delay"
    # A SIGKILLed `apt-get install` can leave dpkg mid-unpack. That state is a direct consequence
    # of the timeout above, so clear it before the retry rather than letting the next attempt
    # fail on something this script did. Recovery, not suppression: a dpkg that is genuinely
    # broken still fails the next attempt loudly, and the exhausted-budget path below still
    # fails the step.
    sudo dpkg --configure -a ||
      echo "apt-install: dpkg --configure -a failed; retrying the install anyway" >&2
  fi
done

echo "::error::apt-install: could not install '$*' after ${ATTEMPTS} attempts (budgets: ${UPDATE_TIMEOUT_S}s update, ${INSTALL_TIMEOUT_S}s install); last failure was apt-get ${phase}; apt mirrors: $(apt_mirrors)" >&2
exit 1
