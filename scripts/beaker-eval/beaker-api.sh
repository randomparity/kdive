#!/usr/bin/env bash
# Shared helpers for Beaker's system-level HTTP API, used by the issue #1792
# evaluation probes. Source this file; it is not meant to be executed.
#
# Every request is appended to $BEAKER_EVIDENCE (when set) as a numbered
# request/response record with secret-bearing fields redacted, so a probe run
# leaves behind evidence that can be pasted into the issue.

# Redact fields that carry credentials. Beaker echoes power_password in the
# system representation and accepts password on user endpoints; neither belongs
# in an evidence log destined for a public issue.
beaker_redact() {
  if jq -e . >/dev/null 2>&1 <<<"$1"; then
    jq '(.. | objects | select(has("password")).password) |= "[redacted]"
        | (.. | objects | select(has("root_password")).root_password) |= "[redacted]"
        | (.. | objects | select(has("power_password")).power_password) |= "[redacted]"
        | (.. | objects | select(has("power_passwd")).power_passwd) |= "[redacted]"' <<<"$1"
  else
    printf '%s\n' "$1"
  fi
}

beaker_require_env() {
  local missing=0
  if [[ -z "${BEAKER_URL:-}" ]]; then
    echo "BEAKER_URL is not set (expected e.g. https://beaker.example.com/bkr)" >&2
    missing=1
  fi
  if [[ "${BEAKER_URL:-}" == */ ]]; then
    echo "BEAKER_URL must not end with a slash: ${BEAKER_URL}" >&2
    missing=1
  fi
  for tool in curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      echo "required tool not found on PATH: $tool" >&2
      missing=1
    fi
  done
  return "$missing"
}

# Authenticate with a password read from a file, never from argv or the
# environment. Skipped when BEAKER_COOKIE_JAR already holds a live session.
beaker_login() {
  : "${BEAKER_COOKIE_JAR:?BEAKER_COOKIE_JAR must be set}"

  if [[ -z "${BEAKER_PASSWORD_FILE:-}" ]]; then
    echo "BEAKER_PASSWORD_FILE is not set; assuming BEAKER_COOKIE_JAR is already authenticated" >&2
    return 0
  fi
  if [[ ! -r "$BEAKER_PASSWORD_FILE" ]]; then
    echo "cannot read BEAKER_PASSWORD_FILE: $BEAKER_PASSWORD_FILE" >&2
    return 1
  fi
  : "${BEAKER_USERNAME:?BEAKER_USERNAME must be set when using BEAKER_PASSWORD_FILE}"

  local code
  code=$(jq -n --arg u "$BEAKER_USERNAME" --arg p "$(<"$BEAKER_PASSWORD_FILE")" \
    '{username: $u, password: $p}' |
    curl --silent --output /dev/null --write-out '%{http_code}' \
      --header 'Accept: application/json' \
      --header 'Content-Type: application/json' \
      --cookie-jar "$BEAKER_COOKIE_JAR" \
      --data-binary @- "$BEAKER_URL/auth/login_password")
  chmod 600 "$BEAKER_COOKIE_JAR" 2>/dev/null || true

  if [[ "$code" != 2* ]]; then
    echo "login failed for user '$BEAKER_USERNAME' (HTTP $code)" >&2
    return 1
  fi
}

# beaker_request METHOD PATH [BODY] [CONTENT_TYPE]
# Writes the response body to stdout and the HTTP status to $BEAKER_LAST_STATUS.
# Returns non-zero on any non-2xx status.
beaker_request() {
  local method="$1" path="$2" body="${3:-}" ctype="${4:-application/json}"
  local body_file code
  body_file=$(mktemp)

  local -a args=(
    --silent --show-error
    --cookie "$BEAKER_COOKIE_JAR"
    --request "$method"
    --header 'Accept: application/json'
    --output "$body_file"
    --write-out '%{http_code}'
  )
  if [[ -n "$body" ]]; then
    args+=(--header "Content-Type: $ctype" --data-binary "$body")
  fi

  code=$(curl "${args[@]}" "$BEAKER_URL$path")
  # Exported so callers can inspect the status of a request that returned
  # non-zero without re-issuing it.
  export BEAKER_LAST_STATUS="$code"

  if [[ -n "${BEAKER_EVIDENCE:-}" ]]; then
    {
      printf '\n===== %s %s =====\n' "$method" "$path"
      if [[ -n "$body" ]]; then
        printf -- '--- request (%s) ---\n' "$ctype"
        beaker_redact "$body"
      fi
      printf -- '--- response %s ---\n' "$code"
      beaker_redact "$(cat "$body_file")"
    } >>"$BEAKER_EVIDENCE"
  fi

  cat "$body_file"
  command rm -- "$body_file"
  [[ "$code" == 2* ]]
}

beaker_get() { beaker_request GET "$1"; }
beaker_post() { beaker_request POST "$1" "$2" "${3:-application/json}"; }
beaker_patch() { beaker_request PATCH "$1" "$2"; }

# One-line ownership and pool state for a system.
beaker_state() {
  beaker_request GET "/systems/$1/" 2>/dev/null | jq -c '{
    fqdn, status,
    arches,
    power_type,
    reservation: (.current_reservation
      | if . == null then null
        else {type, recipe_id, user: .user.user_name, start_time, finish_time} end),
    loan: (.current_loan
      | if . == null then null
        else {recipient: (.recipient // .recipient_user.user_name)} end),
    pools: [.pools[]? | if type == "string" then . else .name end]
  }'
}

# Poll a power/provision command to a terminal state. Prints each observed
# transition. Returns non-zero unless the command reached Completed.
#
# beaker_await_command FQDN COMMAND_ID [TIMEOUT_SECONDS]
beaker_await_command() {
  local fqdn="$1" id="$2" timeout="${3:-300}"
  local waited=0 interval=5 last='' entry state

  while ((waited < timeout)); do
    entry=$(beaker_request GET "/systems/$fqdn/commands/?page_size=20" 2>/dev/null |
      jq -c --argjson id "$id" '.entries[]? | select(.id == $id)')
    if [[ -n "$entry" ]]; then
      state=$(jq -r '.status' <<<"$entry")
      if [[ "$state" != "$last" ]]; then
        printf '    [%4ds] %s\n' "$waited" "$entry"
        last="$state"
      fi
      case "$state" in
      Completed) return 0 ;;
      Failed | Aborted) return 1 ;;
      esac
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done

  echo "    timed out after ${timeout}s with command $id still non-terminal" >&2
  echo "    NOTE: Beaker has no server-side command timeout; a command stays" >&2
  echo "          Running indefinitely while its lab controller is down." >&2
  return 1
}
