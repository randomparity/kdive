#!/usr/bin/env bash
# Regression harness for the github_runner registration preflight (#1291). Drives the REAL
# role in isolation via ansible-playbook against localhost, asserting the two security-sensitive
# fail paths and the idempotence skip — no GitHub token, no runner, no network.
#
# The discriminator is a hermetic TRIPWIRE: the "Resolve the download URL" task runs only after
# the token/arch gates pass and the already-registered skip does NOT fire, so its presence in the
# play output means the register flow started. This is offline-robust — unlike watching for a
# config.sh call, which the role invokes as `./config.sh` from its chdir (never a PATH fake) and
# which is unreachable offline anyway (the download precedes it and fails first).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/github_runner_preflight.yml"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0
# --tags github_runner_register isolates the registration branch (arch resolve + marker stat +
# fail-closed/fail-loud + resolve-URL + download + config.sh), exactly as the gdbstub harness
# isolates its prune task. The svc.sh-install / .env / systemd tasks are UNtagged, so they never
# run here — they need root/systemd and cannot execute in a localhost harness.
play() { ansible-playbook "$playbook" --tags github_runner_register -e "@$1" >"$2" 2>&1; }
# The register flow reached the resolve-URL stage (past both gates and the skip). Offline-hermetic.
# A SKIPPED task still prints its "TASK [...]" header, so match the RESULT line after it
# (ok:/changed:), not the task name — otherwise a correctly-skipped block reads as "ran".
reached_register() { grep -A1 'Resolve the download URL' "$1" | grep -qE '^(ok|changed):'; }

# Case 1: token fail-closed — an empty token must fail the play before the register flow starts.
cat >"$work/case1.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: ""
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner1#" "$work/case1.yml"
if play "$work/case1.yml" "$work/case1.out"; then
  echo "FAIL case1: empty token did not fail the play"
  fail=1
elif reached_register "$work/case1.out"; then
  echo "FAIL case1: register flow proceeded past the token gate"
  fail=1
else echo "ok case1: token fail-closed"; fi

# Case 2: arch fail-loud — an arch with no asset and no override URL must fail, naming the seam.
cat >"$work/case2.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: tok
github_runner_tarball_url: ""
github_runner_arch_map:
  x86_64: {asset: "", label: x64}
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner2#" "$work/case2.yml"
if play "$work/case2.yml" "$work/case2.out"; then
  echo "FAIL case2: missing asset+override did not fail"
  fail=1
elif ! grep -qi 'ppc64le\|no upstream asset\|github_runner_tarball_url' "$work/case2.out"; then
  echo "FAIL case2: failure message did not name the arch seam"
  fail=1
else echo "ok case2: arch fail-loud"; fi

# Case 3: already-registered skip — with the .runner marker present the register flow must be
# skipped entirely. A valid token is supplied so the skip (not a missing token) is what stops
# registration: if a broken skip guard let the flow run, it would reach the register stage
# (tripwire) or fail at the real download — either way case3 goes red.
mkdir -p "$work/runner3"
echo '{}' >"$work/runner3/.runner"
echo '{}' >"$work/runner3/.credentials"
cat >"$work/case3.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: tok
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner3#" "$work/case3.yml"
if ! play "$work/case3.yml" "$work/case3.out"; then
  echo "FAIL case3: already-registered run failed (should skip register)"
  cat "$work/case3.out"
  fail=1
elif reached_register "$work/case3.out"; then
  echo "FAIL case3: register flow ran for an already-registered runner"
  fail=1
else echo "ok case3: already-registered skip"; fi

exit "$fail"
