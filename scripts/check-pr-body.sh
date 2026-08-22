#!/usr/bin/env bash
# Fail when pull-request or issue body text carries a credential or a pasted process
# environment.
#
# This guards the channel that published five live keys in PR #2037. That body was assembled
# through a shell, so its markdown backtick spans ran as command substitutions. One span was
# `env`, and the whole process environment landed in a public pull-request body. GitHub secret
# scanning flagged two of the five keys, after the fact, as alerts.
#
# Two independent checks run here, because neither one alone is enough. Both numbers below come
# from a direct run against that body:
#
#   1. gitleaks pattern rules. These caught the Hugging Face, Exa and OpenRouter keys — three of
#      four. Pattern rules cannot catch a base64-wrapped credential: the Atlassian token in that
#      body was base64, so no provider prefix survived for any rule to match.
#   2. An environment-dump shape check. This counts bare KEY=VALUE lines anchored at the start of
#      a line. It targets the mechanism instead of the provider, so it catches the base64 token,
#      the local LiteLLM key and the host/network disclosure that every pattern rule missed. It
#      also keeps working for a provider gitleaks has no rule for.
#
# The Yelp detect-secrets hook in .pre-commit-config.yaml is not a substitute. It scans files in
# the repository, this text never becomes a file, and a direct run of detect-secrets 1.5.0 over
# that same body reported zero findings.
#
# Neither check ever prints a matched value. CI logs on a public repository are public, so a
# checker that echoed the secret would republish exactly what it just caught. gitleaks runs with
# --redact, and the environment-dump report lists variable names only.
#
# Set KDIVE_GITLEAKS to run an alternate gitleaks binary (the tests use this).
#
# Usage: check-pr-body.sh FILE [FILE...]
# Exit:  0 every file is clean; 1 a body carries a secret; 2 the checker could not run.
set -euo pipefail

readonly GITLEAKS="${KDIVE_GITLEAKS:-gitleaks}"

# Measured over 60 consecutive real pull-request bodies from this repository: every one of them
# had zero bare KEY=VALUE lines, while the leaked body had 80. Ten keeps a wide margin on both
# sides. Prose that cites a variable (KDIVE_LOCAL_ROLE_BOOTSTRAP=1) sits mid-sentence, so the
# start-of-line anchor already excludes it.
readonly ENV_DUMP_LIMIT=10

# A shell variable name in an `env` dump: upper-case, at least three characters, then '='.
readonly ENV_DUMP_PATTERN='^[A-Z][A-Z0-9_]{2,}='

# gitleaks exits 1 both for "found a leak" and for its own startup errors, so a crash would be
# reported as a credential. --exit-code moves the leak verdict to a code nothing else uses, which
# separates "this body carries a secret" from "the checker could not run".
readonly GITLEAKS_LEAK_EXIT=7

if (($# == 0)); then
  printf "usage: check-pr-body.sh FILE [FILE...]\n" >&2
  exit 2
fi

if ! command -v "${GITLEAKS}" >/dev/null 2>&1; then
  printf "check-pr-body: gitleaks not found (looked for '%s')\n" "${GITLEAKS}" >&2
  printf "  install it with: brew install gitleaks   (or set KDIVE_GITLEAKS)\n" >&2
  exit 2
fi

status=0

for file in "$@"; do
  if [[ ! -f "${file}" ]]; then
    printf "check-pr-body: no such file: %s\n" "${file}" >&2
    exit 2
  fi

  # grep -c exits 1 when it matches nothing; that is a clean body, not an error.
  env_lines=$(grep -cE "${ENV_DUMP_PATTERN}" "${file}" || true)
  if ((env_lines >= ENV_DUMP_LIMIT)); then
    printf "%s: %d bare KEY=VALUE lines — this looks like a pasted process environment\n" \
      "${file}" "${env_lines}" >&2
    printf "  variable names only (values withheld): %s\n" \
      "$(grep -oE "${ENV_DUMP_PATTERN}" "${file}" | tr -d '=' | sort -u | tr '\n' ' ')" >&2
    status=1
  fi

  # Report gitleaks output only on a hit, so a clean body produces no output at all.
  gitleaks_status=0
  gitleaks_report=$("${GITLEAKS}" dir "${file}" --redact --no-banner --no-color --verbose \
    --exit-code "${GITLEAKS_LEAK_EXIT}" 2>&1) || gitleaks_status=$?
  case "${gitleaks_status}" in
  0) ;;
  "${GITLEAKS_LEAK_EXIT}")
    printf "%s: gitleaks matched a credential pattern\n%s\n" "${file}" "${gitleaks_report}" >&2
    status=1
    ;;
  *)
    printf "check-pr-body: gitleaks failed to run (exit %d)\n%s\n" \
      "${gitleaks_status}" "${gitleaks_report}" >&2
    exit 2
    ;;
  esac
done

if ((status != 0)); then
  printf "\nRemove the secret from the body text, then rotate every key it exposed.\n" >&2
  printf "Editing a published body does NOT unpublish it: GitHub keeps every prior revision\n" >&2
  printf "in userContentEdits, and a public repository mirrors the original to third parties.\n" >&2
  printf "Build bodies with 'gh pr create --body-file FILE'. Never pass a body as a shell\n" >&2
  printf "string: backtick and \$() spans inside it run as commands. See AGENTS.md.\n" >&2
fi

exit "${status}"
