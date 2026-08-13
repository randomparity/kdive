#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for an existing Helm release.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: $0 RELEASE NAMESPACE VALUES_FILE BACKUP_PATH TARGET_IMAGE" >&2
  echo "A rolling protocol 2 to protocol 3 upgrade is refused." >&2
  exit 2
}

[[ "$#" -eq 5 ]] || usage
helm_release="$1"
namespace="$2"
values_file="$3"
backup_path="$4"
target_image="$5"
release="${helm_release}-kdive"
backup_parent="$(dirname -- "$backup_path")"

[[ -n "$helm_release" && -n "$namespace" && -f "$values_file" ]] || usage
[[ "$backup_path" == /* && "$backup_path" != */ ]] || {
  echo "BACKUP_PATH must be an absolute file path" >&2
  exit 2
}
[[ -d "$backup_parent" && -w "$backup_parent" && ! -e "$backup_path" ]] || {
  echo "backup target must be a new file in an existing writable directory: ${backup_path}" >&2
  exit 2
}
: "${KDIVE_MIGRATION_DATABASE_URL:?set the explicit migration-owner database DSN}"
[[ -n "$target_image" && "$target_image" != *[[:space:]]* ]] || usage
for tool in gio helm kubectl pg_dump pg_restore psql python; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can scale workers" >&2
    exit 2
  }
done

worker_replicas="$(helm get values "$helm_release" --namespace "$namespace" --all --output json |
  python -c 'import json,sys; print(json.load(sys.stdin)["worker"]["replicas"])')"
[[ "$worker_replicas" =~ ^[0-9]+$ ]] || {
  echo "installed worker.replicas is not a non-negative integer: ${worker_replicas}" >&2
  exit 2
}

image_repository=""
image_tag=""
image_digest=""
if [[ "$target_image" == *@sha256:* ]]; then
  image_repository="${target_image%@sha256:*}"
  image_digest="sha256:${target_image##*@sha256:}"
elif [[ "${target_image##*/}" == *:* ]]; then
  image_repository="${target_image%:*}"
  image_tag="${target_image##*:}"
else
  echo "TARGET_IMAGE must carry an explicit tag or sha256 digest" >&2
  exit 2
fi
[[ -n "$image_repository" && -n "${image_tag}${image_digest}" ]] || usage

# Capture the exact Pod UIDs before mutation; those are the witness rows the stop must terminate.
pod_evidence="$(mktemp)"
trap 'gio trash "$pod_evidence" >/dev/null 2>&1 || true' EXIT
kubectl get pods --namespace "$namespace" -l "app=${release}-worker" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.uid}{"\n"}{end}' >"$pod_evidence"

phase=precondition
recovery() {
  rc=$?
  ((rc == 0)) && return
  cutover_state=unknown
  if psql "$KDIVE_MIGRATION_DATABASE_URL" --set ON_ERROR_STOP=1 --quiet \
    --command "SELECT 1" >/dev/null 2>&1; then
    installed_protocol="$(
      psql "$KDIVE_MIGRATION_DATABASE_URL" --set ON_ERROR_STOP=1 --tuples-only --no-align \
        --command "SELECT protocol FROM public.capture_operation_cutoff WHERE singleton" \
        2>/dev/null || true
    )"
    cutover_state=old
    [[ "$installed_protocol" == "3" ]] && cutover_state=installed
  fi
  kubectl scale "statefulset/${release}-worker" --namespace "$namespace" --replicas=0 \
    >/dev/null 2>&1 || true
  echo "Helm capture protocol cutover failed during ${phase}; workers remain scaled to zero." >&2
  if [[ "$cutover_state" == installed ]]; then
    echo "Protocol 3 is installed. Do not restart a protocol-2 worker." >&2
    echo "Rollback database exactly with:" >&2
    echo "  pg_restore --clean --if-exists --dbname=\"${KDIVE_MIGRATION_DATABASE_URL}\" \"${backup_path}\"" >&2
    echo "Then deploy the prior image/chart before restoring its replica count." >&2
  elif [[ "$cutover_state" == old ]]; then
    echo "The old schema remains authoritative. Correct the named blocker and rerun the same command." >&2
  else
    echo "Database state could not be inspected; do not start any worker until it is known." >&2
    echo "If protocol 3 is installed, rollback database exactly with:" >&2
    echo "  pg_restore --clean --if-exists --dbname=\"${KDIVE_MIGRATION_DATABASE_URL}\" \"${backup_path}\"" >&2
    echo "Then deploy the prior image/chart before restoring its replica count." >&2
  fi
  gio trash "$pod_evidence" >/dev/null 2>&1 || true
  exit "$rc"
}
trap recovery EXIT

kubectl scale "statefulset/${release}-worker" --namespace "$namespace" --replicas=0
kubectl wait --for=delete pod --namespace "$namespace" -l "app=${release}-worker" --timeout=10m

while IFS=$'\t' read -r pod_name pod_uid; do
  [[ -n "$pod_name" && -n "$pod_uid" ]] || continue
  termination_count="$(
    psql "$KDIVE_MIGRATION_DATABASE_URL" \
      --set ON_ERROR_STOP=1 --tuples-only --no-align \
      --set "pod_name=${pod_name}" --set "pod_uid=${pod_uid}" --set "namespace=${namespace}" <<'SQL'
SELECT count(*) FROM public.worker_incarnations
WHERE authority_kind = 'kubernetes'
  AND authority_binding ->> 'namespace' = :'namespace'
  AND authority_binding ->> 'name' = :'pod_name'
  AND authority_binding ->> 'uid' = :'pod_uid'
  AND state = 'terminated' AND terminated_at IS NOT NULL AND outcome IS NOT NULL;
SQL
  )"
  [[ "$termination_count" == "1" ]] || {
    echo "missing exact lifecycle termination witness: ${namespace}/${pod_name} uid=${pod_uid}" >&2
    exit 1
  }
done <"$pod_evidence"

pg_dump --format=custom --file="$backup_path" "$KDIVE_MIGRATION_DATABASE_URL"
phase=upgrade
helm_args=(
  upgrade "$helm_release" "${repo_root}/deploy/helm/kdive"
  --namespace "$namespace"
  --values "$values_file"
  --set-string "worker.replicas=${worker_replicas}"
  --set-string "image.repository=${image_repository}"
  --set-string "image.tag=${image_tag}"
  --set-string "image.digest=${image_digest}"
  --wait --timeout 10m
)
helm "${helm_args[@]}"
phase=complete
gio trash "$pod_evidence" >/dev/null 2>&1 || true
trap - EXIT
echo "protocol 3 cutover complete; restored ${worker_replicas} worker replica(s)"
