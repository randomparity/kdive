#!/usr/bin/env bash
# Audit dependencies for known advisories, retrying only a run that produced no verdict.
#
# `pip-audit` exits 1 both when it finds a genuine advisory and when it cannot reach PyPI, so
# a loop that retries on exit status re-runs real findings until the budget is exhausted and
# is one plausible "exhausted budget means flaky" edit away from passing them (#1913).
#
# What separates the two is whether the run produced a verdict at all. Under `-f json`:
#
#   no advisories               exit 0, valid JSON, every `vulns` empty
#   genuine advisory            exit 1, valid JSON, some `vulns` non-empty
#   PyPI / index unreachable    exit 1, ZERO BYTES of stdout (traceback on stderr)
#
# So: parseable JSON is a verdict and is never retried — the pass/fail comes from its content,
# not from pip-audit's exit status. No parseable JSON means the audit did not complete, and
# only that is retried. Exhausting the retries fails: an unreachable PyPI leaves the
# dependency set unaudited, and an unaudited set is not a passing audit (ADR-0553).
#
# Usage: audit-deps.sh runtime|dev
#   runtime  the shipped set (--strict). GATES the build: findings, or an audit that never
#            completed, exit non-zero.
#   dev      every dependency including dev. Informational by contract — dev advisories do
#            not ship to users — so it reports to the job summary and always exits 0.
set -euo pipefail

# `uv run --with`, not `uvx`: pip-audit resolves wheels for the interpreter it runs on, and an
# ephemeral uvx env is built on whatever Python uv happens to find. On an interpreter other
# than the project's, it picks (say) cp313 wheels and dies on
# "THESE PACKAGES DO NOT MATCH THE HASHES" — hashes a lock pinned to `==3.14.*` never
# contained. That is a red audit with no advisory behind it, and it is invisible on a runner
# whose default interpreter happens to match. `uv run` uses the project's interpreter by
# construction, so there is nothing to keep in step (ADR-0553).
readonly PIP_AUDIT=(uv run --with 'pip-audit==2.10.0' pip-audit)
#: Seconds to wait between attempts. One attempt per entry, plus the first — deriving ATTEMPTS
#: keeps the two in step, since raising ATTEMPTS alone would index past the array and `set -u`
#: would abort the run between attempts with a bare `unbound variable`.
readonly BACKOFF_S=(5 15)
readonly ATTEMPTS=$((${#BACKOFF_S[@]} + 1))

readonly MODE="${1:-}"
case "$MODE" in
runtime | dev) ;;
*)
  echo "usage: $(basename "$0") runtime|dev" >&2
  exit 2
  ;;
esac

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
readonly reqs="$workdir/requirements.txt"
readonly report="$workdir/audit.json"
readonly errlog="$workdir/audit.err"

if [[ "$MODE" == runtime ]]; then
  uv export --no-emit-project --no-dev --no-default-groups --group live \
    --format requirements-txt >"$reqs"
  audit_args=(--no-deps --strict -r "$reqs" -f json)
else
  uv export --no-emit-project --format requirements-txt >"$reqs"
  audit_args=(--no-deps -r "$reqs" -f json)
fi

# 0 = completed, no findings; 1 = completed, findings (named on stdout); 2 = no verdict.
# The logic lives in scripts/audit_report.py so it can be proven offline against real report
# shapes; see tests/scripts/test_audit_report.py.
#
# `uv run python`, not bare `python3`, for the same reason pip-audit runs under `uv run` above:
# the runner's ambient interpreter is not the project's. This file is linted at
# `target-version = "py314"`, so ruff rewrites it to 3.14-only syntax (it did, to the `except`
# clause) — under an older `python3` it then dies on a SyntaxError, which the caller would read
# as a verdict. The project's interpreter is the only one it is checked against.
classify() {
  uv run python "$(dirname "$0")/audit_report.py" "$report"
}

verdict=2
findings=""
for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
  audit_status=0
  "${PIP_AUDIT[@]}" "${audit_args[@]}" >"$report" 2>"$errlog" || audit_status=$?
  if findings="$(classify)"; then
    verdict=0
  else
    verdict=$?
  fi
  # The classifier's contract is exactly 0, 1, 2. Anything else is the classifier itself
  # failing — a SyntaxError (1), no interpreter on PATH (127), the OOM killer (137) — and
  # must not be read as its verdict. Untreated, 1 means FOUND, so a broken classifier reports
  # "your dependencies are vulnerable" and names none.
  if ((verdict > 2)) || { ((verdict == 1)) && [[ -z "$findings" ]]; }; then
    echo "audit-deps: classifier exited ${verdict} with no findings — treating as no verdict" >&2
    verdict=2
  fi
  # Passing requires the report and pip-audit's own exit status to agree. The exit status
  # cannot tell a finding from a timeout, which is why it never decides on its own — but a
  # clean-looking report from a run that nonetheless failed is a disagreement this script
  # cannot explain (a `--strict` collection abort is the case to worry about), and the safe
  # reading of "cannot explain" is "no verdict": retry, then fail. Never green.
  if ((verdict == 0 && audit_status != 0)); then
    verdict=2
  fi
  if ((verdict != 2)); then
    break # the audit produced a verdict; it is authoritative and is not retried.
  fi
  if ((attempt < ATTEMPTS)); then
    delay="${BACKOFF_S[attempt - 1]}"
    echo "audit-deps: $MODE audit produced no verdict (attempt ${attempt}/${ATTEMPTS}); retrying in ${delay}s" >&2
    sleep "$delay"
  fi
done

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    cat >>"$GITHUB_STEP_SUMMARY"
  else
    cat >&2
  fi
}

if ((verdict == 2)); then
  tail -n 20 "$errlog" >&2
  echo >&2 # pip's last line often lacks a newline, which would swallow the annotation below
  if [[ "$MODE" == dev ]]; then
    echo "::warning::pip-audit could not complete the dev audit after ${ATTEMPTS} attempts (see log)"
    exit 0
  fi
  echo "::error::pip-audit could not complete after ${ATTEMPTS} attempts — the dependency set is unaudited" >&2
  exit 1
fi

if ((verdict == 0)); then
  echo "No known advisories in $MODE dependencies."
  if [[ "$MODE" == dev ]]; then
    # The dev job reports only through the summary, so a clean run must say so there too —
    # otherwise its summary is empty and indistinguishable from a step that never ran.
    echo "No known advisories in dev dependencies." | summary
  fi
  exit 0
fi

if [[ "$MODE" == dev ]]; then
  {
    echo "### pip-audit: dev-dependency advisories (informational)"
    echo '```'
    echo "$findings"
    echo '```'
  } | summary
  echo "::warning::pip-audit found dev-dependency advisories (see job summary)"
  exit 0
fi

echo "$findings" >&2
echo "::error::pip-audit found advisories in the shipped dependency set" >&2
exit 1
