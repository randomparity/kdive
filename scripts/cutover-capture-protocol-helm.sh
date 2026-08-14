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
[[ -d "$backup_parent" && -w "$backup_parent" && ! -e "$backup_path" &&
  ! -L "$backup_path" ]] || {
  echo "backup target must be a new file in an existing writable directory" >&2
  exit 2
}
: "${KDIVE_MIGRATION_DATABASE_URL:?set the explicit migration-owner database DSN}"
[[ -n "$target_image" && "$target_image" != *[[:space:]]* ]] || usage
for tool in docker env gio helm kubectl ln mktemp pg_dump pg_restore psql timeout; do
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

work_dir="$(mktemp -d "${backup_parent}/.kdive-helm-cutover.XXXXXX")"
chmod 0700 "$work_dir"
frozen_kubeconfig="${work_dir}/kubeconfig"
frozen_chart="${work_dir}/chart"
frozen_values="${work_dir}/values.yaml"

cleanup_work() {
  [[ ! -e "$work_dir" ]] || gio trash "$work_dir" >/dev/null 2>&1 || {
    echo "restricted cutover snapshot retained at: ${work_dir}" >&2
    return 0
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
cutover_init_database_access "$KDIVE_MIGRATION_DATABASE_URL" "$work_dir" "$py"
cutover_scrub_database_environment

kube_context="$(cutover_bounded "Kubernetes context snapshot" \
  kubectl config current-context)"
[[ -n "$kube_context" ]] || {
  echo "kubectl has no current context; select the intended cluster and rerun" >&2
  exit 2
}
cutover_bounded "Kubernetes credential snapshot" \
  kubectl --context "$kube_context" config view --raw --minify --flatten \
  >"$frozen_kubeconfig"
chmod 0400 "$frozen_kubeconfig"
"$py" - "${repo_root}/deploy/helm/kdive" "$values_file" \
  "$frozen_chart" "$frozen_values" <<'PY'
import os
import shutil
import sys

chart, values, chart_copy, values_copy = sys.argv[1:]
for root, dirs, files in os.walk(chart):
    for name in dirs + files:
        if os.path.islink(os.path.join(root, name)):
            raise SystemExit("Helm chart snapshot refuses symbolic links")
shutil.copytree(chart, chart_copy)
shutil.copy2(values, values_copy)
for root, dirs, files in os.walk(chart_copy):
    os.chmod(root, 0o500)
    for name in files:
        os.chmod(os.path.join(root, name), 0o400)
os.chmod(values_copy, 0o400)
PY
helm_ctx=(helm --kubeconfig "$frozen_kubeconfig" --kube-context "$kube_context")
kubectl_ctx=(kubectl --kubeconfig "$frozen_kubeconfig" --context "$kube_context")

cluster_uid="$(cutover_bounded "Kubernetes cluster identity" \
  "${kubectl_ctx[@]}" get namespace kube-system -o jsonpath='{.metadata.uid}')"
namespace_uid="$(cutover_bounded "Kubernetes namespace identity" \
  "${kubectl_ctx[@]}" get namespace "$namespace" -o jsonpath='{.metadata.uid}')"
statefulset_uid="$(cutover_bounded "worker StatefulSet identity" \
  "${kubectl_ctx[@]}" get "statefulset/${worker_name}" --namespace "$namespace" \
  -o jsonpath='{.metadata.uid}')"
cutover_bounded "Helm release identity" "${helm_ctx[@]}" status "$helm_release" \
  --namespace "$namespace" --output json >"${work_dir}/release.json"
release_revision="$("$py" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "${work_dir}/release.json")"
[[ -n "$cluster_uid" && -n "$namespace_uid" && -n "$statefulset_uid" ]] || {
  echo "cluster, namespace, or worker StatefulSet identity is empty" >&2
  exit 1
}
[[ "$release_revision" =~ ^[1-9][0-9]*$ ]] || {
  echo "Helm release revision is invalid" >&2
  exit 1
}

current_identity() {
  local current_cluster current_namespace current_statefulset current_revision
  current_cluster="$(cutover_bounded "Kubernetes cluster identity revalidation" \
    "${kubectl_ctx[@]}" get namespace kube-system -o jsonpath='{.metadata.uid}')"
  current_namespace="$(cutover_bounded "namespace identity revalidation" \
    "${kubectl_ctx[@]}" get namespace "$namespace" -o jsonpath='{.metadata.uid}')"
  current_statefulset="$(cutover_bounded "StatefulSet identity revalidation" \
    "${kubectl_ctx[@]}" get "statefulset/${worker_name}" --namespace "$namespace" \
    -o jsonpath='{.metadata.uid}')"
  current_revision="$(cutover_bounded "release revision revalidation" \
    "${helm_ctx[@]}" status "$helm_release" --namespace "$namespace" \
    --output json | "$py" -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
  [[ "$current_cluster" == "$cluster_uid" &&
    "$current_namespace" == "$namespace_uid" &&
    "$current_statefulset" == "$statefulset_uid" &&
    "$current_revision" == "$release_revision" ]] || {
    echo "cluster, namespace, StatefulSet, or release revision identity changed" >&2
    return 1
  }
}

current_object_identity() {
  local current_cluster current_namespace current_statefulset
  current_cluster="$(cutover_bounded "Kubernetes cluster identity revalidation" \
    "${kubectl_ctx[@]}" get namespace kube-system -o jsonpath='{.metadata.uid}')"
  current_namespace="$(cutover_bounded "namespace identity revalidation" \
    "${kubectl_ctx[@]}" get namespace "$namespace" -o jsonpath='{.metadata.uid}')"
  current_statefulset="$(cutover_bounded "StatefulSet identity revalidation" \
    "${kubectl_ctx[@]}" get "statefulset/${worker_name}" --namespace "$namespace" \
    -o jsonpath='{.metadata.uid}')"
  [[ "$current_cluster" == "$cluster_uid" &&
    "$current_namespace" == "$namespace_uid" &&
    "$current_statefulset" == "$statefulset_uid" ]] || {
    echo "cluster, namespace, or StatefulSet identity changed" >&2
    return 1
  }
}

namespaced_permissions=(
  "get pods" "list pods" "watch pods"
  "get secrets" "list secrets" "create secrets" "update secrets" "patch secrets"
  "delete secrets" "get jobs.batch" "list jobs.batch" "watch jobs.batch"
  "create jobs.batch" "delete jobs.batch" "get deployments.apps" "list deployments.apps"
  "watch deployments.apps" "delete deployments.apps" "get replicasets.apps"
  "list replicasets.apps"
  "create deployments.apps" "update deployments.apps" "patch deployments.apps"
  "get statefulsets.apps" "list statefulsets.apps" "watch statefulsets.apps"
  "create statefulsets.apps" "update statefulsets.apps" "delete statefulsets.apps"
  "patch statefulsets.apps" "patch statefulsets.apps/scale" "get services"
  "list services" "create services" "update services" "patch services" "delete services"
  "get configmaps" "list configmaps" "create configmaps" "update configmaps"
  "patch configmaps" "delete configmaps" "get serviceaccounts" "list serviceaccounts"
  "create serviceaccounts" "update serviceaccounts" "patch serviceaccounts"
  "delete serviceaccounts"
  "get roles.rbac.authorization.k8s.io" "create roles.rbac.authorization.k8s.io"
  "update roles.rbac.authorization.k8s.io" "patch roles.rbac.authorization.k8s.io"
  "list roles.rbac.authorization.k8s.io" "delete roles.rbac.authorization.k8s.io"
  "get rolebindings.rbac.authorization.k8s.io"
  "create rolebindings.rbac.authorization.k8s.io"
  "update rolebindings.rbac.authorization.k8s.io"
  "patch rolebindings.rbac.authorization.k8s.io"
  "list rolebindings.rbac.authorization.k8s.io"
  "delete rolebindings.rbac.authorization.k8s.io" "get networkpolicies.networking.k8s.io"
  "create networkpolicies.networking.k8s.io" "update networkpolicies.networking.k8s.io"
  "patch networkpolicies.networking.k8s.io" "list networkpolicies.networking.k8s.io"
  "delete networkpolicies.networking.k8s.io"
)
for permission in "${namespaced_permissions[@]}"; do
  read -r verb resource <<<"$permission"
  allowed="$(cutover_bounded "Kubernetes authorization preflight" \
    "${kubectl_ctx[@]}" auth can-i "$verb" "$resource" --namespace "$namespace")"
  [[ "$allowed" == yes ]] || {
    echo "Kubernetes authorization denied: ${verb} ${resource}" >&2
    exit 1
  }
done
cluster_permissions=(
  "get clusterroles.rbac.authorization.k8s.io"
  "create clusterroles.rbac.authorization.k8s.io"
  "update clusterroles.rbac.authorization.k8s.io"
  "patch clusterroles.rbac.authorization.k8s.io"
  "list clusterroles.rbac.authorization.k8s.io"
  "delete clusterroles.rbac.authorization.k8s.io"
  "get clusterrolebindings.rbac.authorization.k8s.io"
  "create clusterrolebindings.rbac.authorization.k8s.io"
  "update clusterrolebindings.rbac.authorization.k8s.io"
  "patch clusterrolebindings.rbac.authorization.k8s.io"
  "list clusterrolebindings.rbac.authorization.k8s.io"
  "delete clusterrolebindings.rbac.authorization.k8s.io"
)
for permission in "${cluster_permissions[@]}"; do
  read -r verb resource <<<"$permission"
  allowed="$(cutover_bounded "Kubernetes cluster authorization preflight" \
    "${kubectl_ctx[@]}" auth can-i "$verb" "$resource")"
  [[ "$allowed" == yes ]] || {
    echo "Kubernetes authorization denied: ${verb} ${resource}" >&2
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
[[ "$worker_replicas" =~ ^[0-9]+$ && -n "$release_secret" ]] || {
  echo "installed worker replicas or migration Secret reference is invalid" >&2
  exit 1
}

if ! cutover_bounded "target image local resolution" \
  docker image inspect "$target_image" >/dev/null 2>&1; then
  cutover_bounded "target image pull" docker pull "$target_image" >/dev/null
fi
if [[ "$target_image" == *@sha256:* ]]; then
  resolved_image="$target_image"
else
  repo_digests="$(cutover_bounded "target image digest snapshot" \
    docker image inspect --format '{{json .RepoDigests}}' "$target_image")"
  resolved_image="$(
    CUTOVER_REPO_DIGESTS="$repo_digests" "$py" - "$target_image" <<'PY'
import json
import os
import sys


def normalize_repository(reference: str) -> str:
    repository = reference.split("@", 1)[0]
    tail = repository.rsplit("/", 1)[-1]
    if ":" in tail:
        repository = repository.rsplit(":", 1)[0]
    first = repository.split("/", 1)[0]
    if "/" not in repository:
        repository = f"docker.io/library/{repository}"
    elif "." not in first and ":" not in first and first != "localhost":
        repository = f"docker.io/{repository}"
    return repository.replace("index.docker.io/", "docker.io/", 1)


approved = normalize_repository(sys.argv[1])
candidates = {
    item for item in json.loads(os.environ["CUTOVER_REPO_DIGESTS"])
    if normalize_repository(item) == approved
}
if len(candidates) != 1:
    raise SystemExit("target image has no unique digest for its approved repository")
print(candidates.pop())
PY
  )"
fi
[[ "$resolved_image" == *@sha256:* ]] || {
  echo "target image did not resolve to an immutable registry digest" >&2
  exit 1
}
image_repository="${resolved_image%@sha256:*}"
image_digest="sha256:${resolved_image##*@sha256:}"

source_args=(
  "$helm_release" "$frozen_chart" --namespace "$namespace" --values "$frozen_values"
  --set-string "worker.replicas=${worker_replicas}"
  --set-string "image.repository=${image_repository}"
  --set-string "image.tag=" --set-string "image.digest=${image_digest}"
)
cutover_bounded "target Helm render" "${helm_ctx[@]}" template --is-upgrade \
  "${source_args[@]}" >"${work_dir}/source-target.yaml"
IFS=$'\t' read -r target_secret target_key < <(
  "$py" - "${work_dir}/source-target.yaml" "$resolved_image" <<'PY'
import sys
import yaml

docs = [doc for doc in yaml.safe_load_all(open(sys.argv[1])) if doc]
workloads = [
    doc for doc in docs
    if doc.get("kind") in {"StatefulSet", "Job"}
    and doc["metadata"]["name"].endswith(("-worker", "-migrate"))
]
images = [
    container["image"] for doc in workloads
    for container in doc["spec"]["template"]["spec"]["containers"]
]
if not images or any(image != sys.argv[2] for image in images):
    raise SystemExit("target Helm render does not use the resolved image digest")
migrate = next(doc for doc in workloads if doc["kind"] == "Job")
env = migrate["spec"]["template"]["spec"]["containers"][-1]["env"]
database = next(item for item in env if item["name"] == "KDIVE_DATABASE_URL")
ref = database["valueFrom"]["secretKeyRef"]
print(ref["name"], ref["key"], sep="\t")
PY
)
"$py" - "${work_dir}/authority.json" "$kube_context" "$cluster_uid" \
  "$namespace" "$namespace_uid" "$worker_name" "$statefulset_uid" \
  "$helm_release" "$release_revision" "$resolved_image" <<'PY'
import json
import os
import sys

path = sys.argv[1]
keys = (
    "context", "cluster_uid", "namespace", "namespace_uid", "statefulset",
    "statefulset_uid", "release", "release_revision", "resolved_image",
)
payload = dict(zip(keys, sys.argv[2:], strict=True))
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True)
os.chmod(path, 0o400)
PY

read_secret() {
  local name="$1" key="$2" destination="$3"
  cutover_bounded "migration Secret snapshot" "${kubectl_ctx[@]}" get secret "$name" \
    --namespace "$namespace" --output json >"${destination}.json"
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
read_secret "$release_secret" "$release_key" "${work_dir}/release-dsn"
read_secret "$target_secret" "$target_key" "${work_dir}/target-dsn"
"$py" - "$CUTOVER_DATABASE_SECRET" \
  "${work_dir}/release-dsn" "${work_dir}/target-dsn" <<'PY'
import sys

supplied = open(sys.argv[1], "rb").read()
if any(open(path, "rb").read() != supplied for path in sys.argv[2:]):
    raise SystemExit("migration database does not match release and target authorities")
PY

cutover_secret="kdive-cutover-$(
  "$py" -c 'import secrets; print(secrets.token_hex(8))' # pragma: allowlist secret
)"
cutover_key="database-url"
cutover_bounded "immutable migration Secret render" \
  "${kubectl_ctx[@]}" create secret generic "$cutover_secret" \
  --namespace "$namespace" --from-file="${cutover_key}=${work_dir}/target-dsn" \
  --dry-run=client --output yaml >"${work_dir}/cutover-secret.yaml"
"$py" - "${work_dir}/cutover-secret.yaml" <<'PY'
import os
import sys
import yaml

path = sys.argv[1]
payload = yaml.safe_load(open(path, encoding="utf-8"))
payload["immutable"] = True
with open(path, "w", encoding="utf-8") as stream:
    yaml.safe_dump(payload, stream, sort_keys=False)
os.chmod(path, 0o400)
PY
collision="$(cutover_bounded "cutover Secret collision preflight" \
  "${kubectl_ctx[@]}" get secret "$cutover_secret" --namespace "$namespace" \
  --ignore-not-found --output name)"
[[ -z "$collision" ]] || {
  echo "attempt-unique cutover Secret unexpectedly exists; refusing mutation" >&2
  exit 1
}
helm_args=(
  upgrade "$helm_release" "$frozen_chart" --namespace "$namespace"
  --values "$frozen_values" --set-string "worker.replicas=${worker_replicas}"
  --set-string "image.repository=${image_repository}" --set-string "image.tag="
  --set-string "image.digest=${image_digest}"
  --set-string "databaseCredentials.migration.secretName=${cutover_secret}"
  --set-string "databaseCredentials.migration.key=${cutover_key}"
)
cutover_bounded "frozen target Helm render" "${helm_ctx[@]}" template --is-upgrade \
  "${helm_args[@]:1}" >"${work_dir}/target.yaml"
"$py" - "${work_dir}/target.yaml" "$cutover_secret" "$cutover_key" <<'PY'
import sys
import yaml

jobs = [
    doc for doc in yaml.safe_load_all(open(sys.argv[1]))
    if doc and doc.get("kind") == "Job"
    and doc["metadata"]["name"].endswith("-migrate")
]
env = jobs[0]["spec"]["template"]["spec"]["containers"][-1]["env"]
database = next(item for item in env if item["name"] == "KDIVE_DATABASE_URL")
ref = database["valueFrom"]["secretKeyRef"]
if (ref["name"], ref["key"]) != (sys.argv[2], sys.argv[3]):
    raise SystemExit("frozen migration hook does not use the cutover-owned Secret")
PY
cutover_bounded "frozen Helm upgrade server-side dry run" \
  "${helm_ctx[@]}" "${helm_args[@]}" --dry-run=server --hide-secret \
  --wait --timeout "${CUTOVER_OPERATION_TIMEOUT_SECONDS}s" \
  >"${work_dir}/upgrade-dry-run.txt"
cutover_bounded "database lifecycle-authority preflight" \
  "${CUTOVER_DATABASE_ENV[@]}" psql --set ON_ERROR_STOP=1 --quiet \
  --command "SELECT 1 FROM public.worker_incarnations LIMIT 1" \
  >"${work_dir}/database-authority"

cutover_secret_uid=""
current_cutover_secret() {
  local observation
  observation="$(mktemp "${work_dir}/secret-observation.XXXXXX")"
  cutover_bounded "immutable migration Secret revalidation" \
    "${kubectl_ctx[@]}" get secret "$cutover_secret" --namespace "$namespace" \
    --output json >"$observation"
  "$py" - "$observation" "$cutover_secret_uid" "$cutover_key" \
    "${work_dir}/target-dsn" <<'PY'
import base64
import json
import sys

observation, expected_uid, key, frozen_path = sys.argv[1:]
payload = json.load(open(observation, encoding="utf-8"))
value = base64.b64decode(payload["data"][key], validate=True)
if payload["metadata"]["uid"] != expected_uid or payload.get("immutable") is not True:
    raise SystemExit("cutover Secret identity or immutability changed")
if value != open(frozen_path, "rb").read():
    raise SystemExit("cutover Secret credential changed")
PY
}

print_shell_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

print_original_rerun() {
  # shellcheck disable=SC2016 # recovery must reference the operator's environment literally
  printf '  KDIVE_MIGRATION_DATABASE_URL="$KDIVE_MIGRATION_DATABASE_URL" '
  printf '%q ' "${repo_root}/scripts/cutover-capture-protocol-helm.sh" \
    "$helm_release" "$namespace" "$values_file" "$backup_path" "$target_image"
  printf '\n'
}

print_successor_cleanup_commands() {
  echo "Expected release revision: $((release_revision + 1))"
  print_shell_command "${helm_ctx[@]}" status "$helm_release" --namespace "$namespace" \
    --output json
  echo "Expected StatefulSet UID: ${statefulset_uid}"
  print_shell_command "${kubectl_ctx[@]}" get "statefulset/${worker_name}" \
    --namespace "$namespace" -o jsonpath='{.metadata.uid}'
  echo "Expected cutover Secret UID: ${cutover_secret_uid}"
  print_shell_command "${kubectl_ctx[@]}" get secret "$cutover_secret" \
    --namespace "$namespace" -o jsonpath='{.metadata.uid}'
  print_shell_command "${kubectl_ctx[@]}" delete secret "$cutover_secret" \
    --namespace "$namespace" --wait=true
}

old_schema_phase() {
  case "$phase" in
  secret-created | stop | backup) return 0 ;;
  *) return 1 ;;
  esac
}

prove_recovery_stopped_state() {
  local identity_rc=0 stop_rc=0 replicas="" survivors=""
  current_object_identity >/dev/null 2>&1
  identity_rc=$?
  if [[ "$identity_rc" -eq 0 ]]; then
    cutover_bounded "Helm recovery stop" "${kubectl_ctx[@]}" scale \
      "statefulset/${worker_name}" --namespace "$namespace" --replicas=0 \
      >/dev/null 2>&1
    stop_rc=$?
  else
    stop_rc="$identity_rc"
  fi
  replicas="$(cutover_bounded "Helm stopped-replica proof" \
    "${kubectl_ctx[@]}" get "statefulset/${worker_name}" --namespace "$namespace" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  survivors="$(cutover_bounded "Helm stopped-Pod proof" \
    "${kubectl_ctx[@]}" get pods --namespace "$namespace" -l "app=${worker_name}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" uid="}{.metadata.uid}{"\n"}{end}' \
    2>/dev/null)"
  if [[ "$stop_rc" -eq 0 && "$replicas" == 0 && -z "$survivors" ]]; then
    echo "Helm stopped-state proof found zero replicas and Pods." >&2
  else
    echo "workers may still be running; surviving replicas=${replicas:-unknown}." >&2
    echo "surviving Pods=${survivors:-unknown}" >&2
  fi
}

cleanup_unused_cutover_secret() {
  if ! current_object_identity >/dev/null 2>&1; then
    return 1
  fi
  if ! current_cutover_secret >/dev/null 2>&1; then
    return 1
  fi
  cutover_bounded "unused cutover Secret cleanup" \
    "${kubectl_ctx[@]}" delete secret "$cutover_secret" --namespace "$namespace" \
    --wait=true >/dev/null 2>&1
}

print_phase_recovery() {
  if old_schema_phase; then
    echo "The old schema remains authoritative; rerun the complete cutover exactly:" >&2
    print_original_rerun >&2
  elif [[ "$phase" == upgrade ]]; then
    echo "The named backup is complete; resume the frozen protocol-3 upgrade exactly:" >&2
    print_shell_command "${helm_ctx[@]}" "${helm_args[@]}" --wait \
      --timeout "${CUTOVER_OPERATION_TIMEOUT_SECONDS}s" >&2
    echo "After success, verify the printed identities and remove the cutover Secret:" >&2
    print_successor_cleanup_commands >&2
    echo "Or rollback the database exactly before deploying the prior chart and image:" >&2
    cutover_print_restore_command "$backup_path" >&2
  else
    echo "Protocol 3 is installed; verify identities and remove the cutover Secret:" >&2
    print_successor_cleanup_commands >&2
  fi
}

phase=pre-mutation
worker_mutation_started=0
recovery() {
  local rc=$? secret_retained=1
  if ((rc == 0)); then
    return
  fi
  trap - EXIT
  set +e
  if [[ "$worker_mutation_started" -eq 1 ]]; then
    prove_recovery_stopped_state
  else
    echo "worker deployment was not mutated by this cutover attempt." >&2
  fi

  if old_schema_phase; then
    if cleanup_unused_cutover_secret; then
      secret_retained=0
    fi
  fi
  echo "restricted frozen Helm snapshot retained at: ${work_dir}" >&2
  if [[ "$secret_retained" -eq 1 ]]; then
    echo "immutable migration Secret retained: ${namespace}/${cutover_secret}" >&2
  fi
  print_phase_recovery
  echo "Never start a protocol-2 worker until database and prior image are restored." >&2
  cutover_cleanup_temporary_backup
  exit "$rc"
}

current_identity
cutover_bounded "immutable migration Secret creation" \
  "${kubectl_ctx[@]}" create --filename "${work_dir}/cutover-secret.yaml"
phase=secret-created
trap recovery EXIT
cutover_secret_uid="$(cutover_bounded "immutable migration Secret identity" \
  "${kubectl_ctx[@]}" get secret "$cutover_secret" --namespace "$namespace" \
  -o jsonpath='{.metadata.uid}')"
[[ -n "$cutover_secret_uid" ]] || exit 1
current_cutover_secret
phase=stop
current_identity
cutover_bounded "worker Pod identity snapshot" "${kubectl_ctx[@]}" get pods \
  --namespace "$namespace" -l "app=${worker_name}" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.uid}{"\n"}{end}' \
  >"${work_dir}/pods"
current_cutover_secret
worker_mutation_started=1
cutover_bounded "Helm worker scale-to-zero" "${kubectl_ctx[@]}" scale \
  "statefulset/${worker_name}" --namespace "$namespace" --replicas=0
cutover_bounded "Helm worker Pod deletion" "${kubectl_ctx[@]}" wait --for=delete pod \
  --namespace "$namespace" -l "app=${worker_name}" \
  --timeout="${CUTOVER_OPERATION_TIMEOUT_SECONDS}s"
while IFS=$'\t' read -r pod_name pod_uid; do
  [[ -n "$pod_name" && -n "$pod_uid" ]] || continue
  witness_sql="SELECT count(*) FROM public.worker_incarnations
WHERE fence_protocol < 3 AND authority_kind = 'kubernetes'
  AND authority_binding ->> 'namespace' = :'namespace'
  AND authority_binding ->> 'name' = :'pod_name'
  AND authority_binding ->> 'uid' = :'pod_uid'
  AND state = 'terminated' AND terminated_at IS NOT NULL AND outcome IS NOT NULL"
  termination_count="$(cutover_bounded "exact lifecycle termination witness" \
    "${CUTOVER_DATABASE_ENV[@]}" psql --set ON_ERROR_STOP=1 \
    --tuples-only --no-align --set "pod_name=${pod_name}" --set "pod_uid=${pod_uid}" \
    --set "namespace=${namespace}" --command "$witness_sql")"
  [[ "$termination_count" == 1 ]] || {
    echo "missing exact lifecycle termination witness: ${namespace}/${pod_name}" >&2
    exit 1
  }
done <"${work_dir}/pods"

complete_witness_sql="SELECT coalesce(string_agg(incarnation, ', ' ORDER BY incarnation), '')
FROM public.worker_incarnations
WHERE fence_protocol < 3 AND authority_kind = 'kubernetes'
  AND authority_binding ->> 'namespace' = :'namespace'
  AND left(authority_binding ->> 'name', length(:'worker_name') + 1)
    = :'worker_name' || '-'
  AND substring(authority_binding ->> 'name' from length(:'worker_name') + 2)
    ~ '^[0-9]+$'
  AND (state <> 'terminated' OR terminated_at IS NULL OR outcome IS NULL
    OR jsonb_typeof(authority_binding -> 'uid') IS DISTINCT FROM 'string')"
legacy_blockers="$(cutover_bounded "complete legacy Kubernetes incarnation witness" \
  "${CUTOVER_DATABASE_ENV[@]}" psql --set ON_ERROR_STOP=1 --tuples-only --no-align \
  --set "namespace=${namespace}" --set "worker_name=${worker_name}" \
  --command "$complete_witness_sql")"
[[ -z "$legacy_blockers" ]] || {
  echo "legacy Kubernetes incarnations are not exactly terminated: ${legacy_blockers}" >&2
  exit 1
}

phase=backup
current_identity
current_cutover_secret
cutover_prepare_backup "$backup_path"
cutover_publish_backup "$backup_path"
phase=upgrade
current_identity
current_cutover_secret
cutover_bounded "Helm protocol-3 upgrade" "${helm_ctx[@]}" "${helm_args[@]}" \
  --wait --timeout "${CUTOVER_OPERATION_TIMEOUT_SECONDS}s"
# shellcheck disable=SC2034 # recovery trap reports this boundary if cleanup fails
phase=secret-cleanup
current_object_identity
new_revision="$(cutover_bounded "upgraded release revision validation" \
  "${helm_ctx[@]}" status "$helm_release" --namespace "$namespace" --output json |
  "$py" -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
[[ "$new_revision" -eq $((release_revision + 1)) ]] || {
  echo "upgraded Helm release revision is not the approved successor" >&2
  exit 1
}
current_cutover_secret
cutover_bounded "cutover Secret cleanup" "${kubectl_ctx[@]}" delete secret \
  "$cutover_secret" --namespace "$namespace" --wait=true
trap - EXIT
cleanup_work
echo "protocol 3 cutover complete; restored ${worker_replicas} worker replica(s)"
