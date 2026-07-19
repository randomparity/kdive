#!/usr/bin/env bash
# Regression harness for the github_runner registration preflight (#1291). Drives the REAL
# role in isolation via ansible-playbook against localhost, with a fake config.sh, asserting
# the two security-sensitive fail paths and the idempotence skip — no GitHub token, no runner.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/github_runner_preflight.yml"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
install -m 0755 "$here/fake-config-sh" "$work/config.sh"

export PATH="$work:$PATH"
export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False
export FAKE_CONFIG_LOG="$work/config.log"

fail=0
# --tags github_runner_register isolates the registration branch (arch resolve + marker stat +
# fail-closed/fail-loud + download + config.sh), exactly as the gdbstub harness isolates its
# prune task. The svc.sh-install / .env / systemd tasks are UNtagged, so they never run here —
# they need root/systemd and cannot execute in a localhost harness.
play() { ansible-playbook "$playbook" --tags github_runner_register -e "@$1" >"$2" 2>&1; }

# Case 1: token fail-closed — empty token in the register branch must fail, no config.sh run.
: >"$FAKE_CONFIG_LOG"
cat >"$work/case1.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: ""
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner1#" "$work/case1.yml"
if play "$work/case1.yml" "$work/case1.out"; then
  echo "FAIL case1: empty token did not fail the play"
  fail=1
elif [[ -s "$FAKE_CONFIG_LOG" ]]; then
  echo "FAIL case1: config.sh ran despite empty token"
  fail=1
elif grep -q 'Resolve the download URL' "$work/case1.out"; then
  # Hermetic tripwire: the register flow proceeded PAST the token assert (this task runs
  # only after it). Catches a bypassed assert even offline, where the download fails before
  # config.sh and would otherwise mask the regression.
  echo "FAIL case1: register flow proceeded past the token gate"
  fail=1
else echo "ok case1: token fail-closed"; fi

# Case 2: arch fail-loud — an arch with no asset and no override URL must fail loud.
: >"$FAKE_CONFIG_LOG"
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

# Case 3: already-registered skip — .runner marker present => no token needed, config.sh not run.
: >"$FAKE_CONFIG_LOG"
mkdir -p "$work/runner3"
echo '{}' >"$work/runner3/.runner"
echo '{}' >"$work/runner3/.credentials"
cat >"$work/case3.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: ""
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner3#" "$work/case3.yml"
if ! play "$work/case3.yml" "$work/case3.out"; then
  echo "FAIL case3: already-registered run failed (should skip register)"
  cat "$work/case3.out"
  fail=1
elif [[ -s "$FAKE_CONFIG_LOG" ]]; then
  echo "FAIL case3: config.sh ran for an already-registered runner"
  fail=1
else echo "ok case3: already-registered skip"; fi

exit "$fail"
