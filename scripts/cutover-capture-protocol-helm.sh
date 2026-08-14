#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for an existing Helm release.
set -euo pipefail
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/cutover-capture-protocol-lib.sh
source "${repo_root}/scripts/cutover-capture-protocol-lib.sh"

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
worker_name="${helm_release}-kdive-worker"
backup_parent="$(dirname -- "$backup_path")"
py="${repo_root}/.venv/bin/python"

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
for tool in docker gio helm kubectl mktemp mv pg_dump pg_restore psql timeout; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can scale workers" >&2
    exit 2
  }
done
[[ -x "$py" ]] || {
  echo "run just setup before the cutover; ${py} is required" >&2
  exit 2
}
cutover_init_contract

kube_context="$(cutover_bounded "Kubernetes context snapshot" kubectl config current-context)"
[[ -n "$kube_context" ]] || {
  echo "kubectl has no current context; select the intended cluster and rerun" >&2
  exit 2
}
helm_ctx=(helm --kube-context "$kube_context")
kubectl_ctx=(kubectl --context "$kube_context")
work_dir="$(mktemp -d "${backup_parent}/.kdive-cutover.XXXXXX")"
chmod 0700 "$work_dir"

cleanup_work() {
  [[ ! -e "$work_dir" ]] || gio trash "$work_dir" >/dev/null 2>&1 || {
    echo "cutover evidence remains for inspection: ${work_dir}" >&2
    return 1
  }
}
cleanup_preflight() {
  local rc=$?
  trap - EXIT
  set +e
  cleanup_work
  exit "$rc"
}
trap cleanup_preflight EXIT

# Resolve and prove the operator-selected cluster and release before touching the workload.
namespace_uid="$(cutover_bounded "Kubernetes namespace identity" \
  "${kubectl_ctx[@]}" get namespace "$namespace" -o jsonpath='{.metadata.uid}')"
[[ -n "$namespace_uid" ]] || {
  echo "namespace identity is empty for context=${kube_context} namespace=${namespace}" >&2
  exit 1
}
cutover_bounded "Helm release identity" \
  "${helm_ctx[@]}" status "$helm_release" --namespace "$namespace" >/dev/null
statefulset_uid="$(cutover_bounded "worker StatefulSet identity" \
  "${kubectl_ctx[@]}" get "statefulset/${worker_name}" --namespace "$namespace" \
  -o jsonpath='{.metadata.uid}')"
[[ -n "$statefulset_uid" ]] || {
  echo "worker StatefulSet identity is empty in the selected release namespace" >&2
  exit 1
}
for permission in "get pods" "get secrets" "get statefulsets.apps" \
  "patch statefulsets.apps/scale"; do
  read -r verb resource <<<"$permission"
  allowed="$(cutover_bounded "Kubernetes authorization preflight" \
    "${kubectl_ctx[@]}" auth can-i "$verb" "$resource" --namespace "$namespace")"
  [[ "$allowed" == yes ]] || {
    echo "Kubernetes authorization denied: ${verb} ${resource} in ${namespace}" >&2
    exit 1
  }
done

cutover_bounded "installed Helm values snapshot" \
  "${helm_ctx[@]}" get values "$helm_release" --namespace "$namespace" \
  --all --output json >"${work_dir}/installed-values.json"
IFS=$'\t' read -r worker_replicas release_secret release_key < <(
  "$py" - "${work_dir}/installed-values.json" <<'PY'
import json
import sys

values = json.load(open(sys.argv[1], encoding="utf-8"))
ref = values["databaseCredentials"]["migration"]
print(values["worker"]["replicas"], ref["secretName"], ref["key"], sep="\t")
PY
)
[[ "$worker_replicas" =~ ^[0-9]+$ && -n "$release_secret" && -n "$release_key" ]] || {
  echo "installed worker replicas or migration Secret reference is invalid" >&2
  exit 1
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
if ! cutover_bounded "target image local resolution" docker image inspect "$target_image" \
  >/dev/null 2>&1; then
  cutover_bounded "target image pull" docker pull "$target_image" >/dev/null
fi

helm_args=(
  upgrade "$helm_release" "${repo_root}/deploy/helm/kdive"
  --namespace "$namespace"
  --values "$values_file"
  --set-string "worker.replicas=${worker_replicas}"
  --set-string "image.repository=${image_repository}"
  --set-string "image.tag=${image_tag}"
  --set-string "image.digest=${image_digest}"
)
cutover_bounded "target Helm render" "${helm_ctx[@]}" template \
  "${helm_args[@]:1}" >"${work_dir}/target.yaml"
IFS=$'\t' read -r target_secret target_key < <(
  "$py" - "${work_dir}/target.yaml" "$target_image" "${work_dir}/target-dsn" <<'PY'
import base64
import os
import sys
import yaml

docs = [doc for doc in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if doc]
target = sys.argv[2]
worker = next(doc for doc in docs if doc.get("kind") == "StatefulSet" and doc["metadata"]["name"].endswith("-worker"))
migrate = next(doc for doc in docs if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("-migrate"))
images = [container["image"] for container in worker["spec"]["template"]["spec"]["containers"]]
images += [container["image"] for container in migrate["spec"]["template"]["spec"]["containers"]]
if not images or any(image != target for image in images):
    raise SystemExit("target Helm render does not use TARGET_IMAGE for worker and migration")
env = migrate["spec"]["template"]["spec"]["containers"][-1]["env"]
database = next(item for item in env if item["name"] == "KDIVE_DATABASE_URL")
ref = database["valueFrom"]["secretKeyRef"]
secrets = [
    doc for doc in docs
    if doc.get("kind") == "Secret" and doc["metadata"]["name"] == ref["name"]
]
if secrets:
    secret = secrets[0]
    if ref["key"] in secret.get("stringData", {}):
        value = secret["stringData"][ref["key"]].encode()
    else:
        value = base64.b64decode(secret["data"][ref["key"]], validate=True)
    fd = os.open(sys.argv[3], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
print(ref["name"], ref["key"], sep="\t")
PY
)
[[ -n "$target_secret" && -n "$target_key" ]] || {
  echo "target migration Secret reference is empty" >&2
  exit 1
}

read_secret() {
  local name="$1" key="$2" destination="$3"
  cutover_bounded "migration Secret snapshot" \
    "${kubectl_ctx[@]}" get secret "$name" --namespace "$namespace" -o json \
    >"${destination}.json"
  "$py" - "${destination}.json" "$key" "$destination" <<'PY'
import base64
import json
import os
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
value = base64.b64decode(payload["data"][sys.argv[2]], validate=True)
fd = os.open(sys.argv[3], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(value)
PY
}

compare_dsn_files() {
  CUTOVER_SUPPLIED_DSN="$KDIVE_MIGRATION_DATABASE_URL" "$py" - "$@" <<'PY'
import os
import sys

supplied = os.environ["CUTOVER_SUPPLIED_DSN"].encode()
if any(open(path, "rb").read() != supplied for path in sys.argv[1:]):
    raise SystemExit(1)
PY
}

read_secret "$release_secret" "$release_key" "${work_dir}/release-dsn"
target_secret_rendered=1
if [[ ! -e "${work_dir}/target-dsn" ]]; then
  target_secret_rendered=0
  read_secret "$target_secret" "$target_key" "${work_dir}/target-dsn"
fi
if ! compare_dsn_files "${work_dir}/release-dsn" "${work_dir}/target-dsn"; then
  echo "migration database does not match the release and target migration Secret authority" >&2
  exit 1
fi
cutover_bounded "database lifecycle-authority preflight" \
  psql "$KDIVE_MIGRATION_DATABASE_URL" --set ON_ERROR_STOP=1 --quiet \
  --command "DO \$check\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = 'public.worker_incarnations'::regclass AND relowner = (SELECT usesysid FROM pg_user WHERE usename = current_user)) THEN RAISE EXCEPTION 'migration principal does not own the lifecycle table'; END IF; END \$check\$" \
  >"${work_dir}/database-authority"
cutover_bounded "worker Pod identity snapshot" \
  "${kubectl_ctx[@]}" get pods --namespace "$namespace" -l "app=${worker_name}" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.uid}{"\n"}{end}' \
  >"${work_dir}/pods"

phase=stop
recovery() {
  local rc=$? stop_rc=0 replicas="" survivors=""
  ((rc == 0)) && return
  trap - EXIT
  set +e
  cutover_bounded "Helm recovery stop" "${kubectl_ctx[@]}" scale \
    "statefulset/${worker_name}" --namespace "$namespace" --replicas=0 >/dev/null 2>&1
  stop_rc=$?
  replicas="$(cutover_bounded "Helm stopped-replica proof" "${kubectl_ctx[@]}" get \
    "statefulset/${worker_name}" --namespace "$namespace" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  survivors="$(cutover_bounded "Helm stopped-Pod proof" "${kubectl_ctx[@]}" get pods \
    --namespace "$namespace" -l "app=${worker_name}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" uid="}{.metadata.uid}{"\n"}{end}' 2>/dev/null)"
  if [[ "$stop_rc" -eq 0 && "$replicas" == 0 && -z "$survivors" ]]; then
    echo "Helm cutover failed during ${phase}; stopped-state proof found zero replicas and Pods." >&2
  else
    echo "Helm cutover failed during ${phase}; workers may still be running." >&2
    echo "surviving replicas=${replicas:-unknown}; surviving Pods=${survivors:-unknown}" >&2
  fi
  if [[ "$CUTOVER_BACKUP_COMPLETE" -eq 1 ]]; then
    echo "The named backup is complete: ${backup_path}" >&2
  fi
  echo 'If protocol 3 is installed, rollback exactly with:' >&2
  printf "  pg_restore --clean --if-exists --dbname=\"\$KDIVE_MIGRATION_DATABASE_URL\" %q\n" \
    "$backup_path" >&2
  echo "Never start a protocol-2 worker until the database and prior image are restored." >&2
  cutover_cleanup_temporary_backup
  cleanup_work
  exit "$rc"
}
trap recovery EXIT

cutover_bounded "Helm worker scale-to-zero" "${kubectl_ctx[@]}" scale \
  "statefulset/${worker_name}" --namespace "$namespace" --replicas=0
cutover_bounded "Helm worker Pod deletion" "${kubectl_ctx[@]}" wait --for=delete pod \
  --namespace "$namespace" -l "app=${worker_name}" \
  --timeout="${CUTOVER_OPERATION_TIMEOUT_SECONDS}s"
while IFS=$'\t' read -r pod_name pod_uid; do
  [[ -n "$pod_name" && -n "$pod_uid" ]] || continue
  termination_count="$(cutover_bounded "exact lifecycle termination witness" \
    psql "$KDIVE_MIGRATION_DATABASE_URL" --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --set "pod_name=${pod_name}" --set "pod_uid=${pod_uid}" \
    --set "namespace=${namespace}" --command \
    "SELECT count(*) FROM public.worker_incarnations WHERE authority_kind = 'kubernetes' AND authority_binding ->> 'namespace' = :'namespace' AND authority_binding ->> 'name' = :'pod_name' AND authority_binding ->> 'uid' = :'pod_uid' AND state = 'terminated' AND terminated_at IS NOT NULL AND outcome IS NOT NULL")"
  [[ "$termination_count" == 1 ]] || {
    echo "missing exact lifecycle termination witness: ${namespace}/${pod_name} uid=${pod_uid}" >&2
    exit 1
  }
done <"${work_dir}/pods"

# Close the preflight-to-mutation window for externally managed Secrets before backup/upgrade.
read_secret "$release_secret" "$release_key" "${work_dir}/release-dsn-post-stop"
if [[ "$target_secret_rendered" -eq 1 ]]; then
  target_post_stop="${work_dir}/target-dsn"
else
  target_post_stop="${work_dir}/target-dsn-post-stop"
  read_secret "$target_secret" "$target_key" "$target_post_stop"
fi
if ! compare_dsn_files "${work_dir}/release-dsn-post-stop" "$target_post_stop"; then
  echo "migration database does not match the post-stop migration Secret authority" >&2
  exit 1
fi

phase=backup
cutover_prepare_backup "$backup_path"
cutover_publish_backup "$backup_path" "$KDIVE_MIGRATION_DATABASE_URL"
phase=upgrade
cutover_bounded "Helm protocol-3 upgrade" "${helm_ctx[@]}" "${helm_args[@]}" \
  --wait --timeout "${CUTOVER_OPERATION_TIMEOUT_SECONDS}s"
phase=complete
trap - EXIT
cleanup_work
echo "protocol 3 cutover complete; restored ${worker_replicas} worker replica(s)"
