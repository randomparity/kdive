#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 <ssh-target> <full-commit-sha>" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage
target=$1
revision=$2
[[ $revision =~ ^[0-9a-f]{40}$ ]] || usage

root=$(git rev-parse --show-toplevel)
[[ $(cd "$root" && git rev-parse HEAD) == "$revision" ]] || {
  echo "refusing proof: supplied revision is not local HEAD" >&2
  exit 2
}
[[ -z $(git -C "$root" status --porcelain --untracked-files=all) ]] || {
  echo "refusing proof: the local revision has uncommitted or untracked files" >&2
  exit 2
}

tag="kdive-2150-$revision"
remote_root="/var/tmp/$tag"
remote_install="/opt/$tag"
database="kdive_2150_${revision:0:12}"
database_login=kdive_authority_host
authority_instance="authority-2150-${revision:0:12}"
proof_incarnation="kdive-authority-2150-${revision:0:12}"

scratch=$(mktemp -d)
cleanup() {
  rm -rf -- "$scratch"
}
trap cleanup EXIT
bundle="$scratch/kdive.bundle"
(cd "$root" && git bundle create "$bundle" HEAD)
git bundle verify "$bundle"

echo "pre-state: resolving exact proof targets"
ssh -o BatchMode=yes "$target" bash -s -- \
  "$remote_root" "$remote_install" "$database" "$database_login" <<'REMOTE_PRESTATE'
set -euo pipefail
remote_root=$1
remote_install=$2
database=$3
database_login=$4
for path in "$remote_root" "$remote_install"; do
  if sudo -n test -e "$path" || sudo -n test -L "$path"; then
    sudo -n stat -c 'preexisting-path\t%N\t%U:%G\t%a\t%F' "$path"
    echo "refusing proof: exact SHA proof target already exists: $path" >&2
    exit 3
  fi
  printf 'absent-path\t%s\n' "$path"
done
for path in \
  /opt/kdive-provider-authority \
  /etc/kdive/credentials/provider-authority \
  /var/lib/kdive/provider-authority \
  /run/kdive/provider-authority \
  /etc/systemd/system/kdive-external-boot-authority.service; do
  if sudo -n test -e "$path" || sudo -n test -L "$path"; then
    sudo -n stat -c 'preexisting-authority\t%N\t%U:%G\t%a\t%F' "$path"
    echo "refusing proof: authority artifact is not clean: $path" >&2
    exit 3
  fi
  printf 'absent-authority\t%s\n' "$path"
done
if command -v psql >/dev/null; then
  sudo -n -u postgres psql -Atqc \
    "SELECT datname FROM pg_database WHERE datname = '$database'" postgres | grep -q . && {
    echo "refusing proof: proof database already exists" >&2
    exit 3
  }
  sudo -n -u postgres psql -Atqc \
    "SELECT rolname FROM pg_roles WHERE rolname = '$database_login'" postgres | grep -q . && {
    echo "refusing proof: authority LOGIN already exists" >&2
    exit 3
  }
fi
true
REMOTE_PRESTATE

echo "source: transferring exact Git bundle"
ssh -o BatchMode=yes "$target" sudo -n install -d -o dave -g dave -m 0755 "$remote_root"
scp -q "$bundle" "$target:$remote_root/kdive.bundle"

ssh -o BatchMode=yes "$target" bash -s -- \
  "$remote_root" "$remote_install" "$revision" "$database" "$database_login" \
  "$authority_instance" "$proof_incarnation" <<'REMOTE_PROOF'
set -euo pipefail
remote_root=$1
remote_install=$2
revision=$3
database=$4
database_login=$5
authority_instance=$6
proof_incarnation=$7
source_root="$remote_root/source"
secrets="$remote_root/secrets"
ansible="uv run --with ansible-core==2.21.1 ansible-playbook"

git clone -q "$remote_root/kdive.bundle" "$source_root"
git -C "$source_root" checkout -q "$revision"
[[ $(git -C "$source_root" rev-parse HEAD) == "$revision" ]]
install -d -m 0700 "$secrets"

install -m 0600 /dev/null "$remote_root/inventory"
printf '%s\n' \
  '[live_vm_runners]' \
  'localhost ansible_connection=local ansible_python_interpreter=/usr/bin/python3' \
  >"$remote_root/inventory"
install -m 0600 /dev/null "$remote_root/proof.yml"
cat >"$remote_root/proof.yml" <<EOF
---
- hosts: live_vm_runners
  become: true
  gather_facts: true
  vars_files:
    - $source_root/deploy/ansible/inventory/group_vars/live_vm_runners.yml
  vars:
    github_runner_user: github-runner
  roles:
    - role: $source_root/deploy/ansible/roles/live_vm_host
EOF

echo "prerequisites: installing through the live_vm_host role"
cd "$remote_root"
$ansible -i inventory proof.yml --tags authority_prerequisites

sudo -n -u postgres createdb "$database"
sudo -n install -d -o postgres -g postgres -m 0700 "$remote_root/migrate"
sudo -n -u postgres git clone -q "$remote_root/kdive.bundle" "$remote_root/migrate/source"
sudo -n -u postgres git -C "$remote_root/migrate/source" checkout -q "$revision"
sudo -n -u postgres env \
  KDIVE_DATABASE_URL="postgresql:///$database?host=/var/run/postgresql" \
  uv run --project "$remote_root/migrate/source" python -m kdive migrate

database_password=$(openssl rand -hex 24)
worker_credential=$(openssl rand -hex 32)
worker_hash=$(printf %s "$worker_credential" | sha256sum | cut -d' ' -f1)
sudo -n -u postgres psql --set=login="$database_login" \
  --set=password="$database_password" --dbname="$database" <<'SQL'
CREATE ROLE :"login" LOGIN PASSWORD :'password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT kdive_provider_authority TO :"login";
SQL
sudo -n -u postgres psql --set=incarnation="$proof_incarnation" \
  --set=credential_hash="$worker_hash" --dbname="$database" <<'SQL'
INSERT INTO worker_incarnations (
  incarnation, authority_kind, authority_binding, credential_hash, fence_protocol
) VALUES (
  :'incarnation', 'local', '{"proof":"issue-2150"}', decode(:'credential_hash', 'hex'), 4
);
SQL
database_dsn="postgresql://$database_login:$database_password@127.0.0.1:5432/$database" # pragma: allowlist secret
printf '%s\n' "$database_dsn" >"$secrets/database-dsn"
printf '%s\n' "$worker_credential" >"$secrets/worker-credential"
chmod 0600 "$secrets/database-dsn" "$secrets/worker-credential"
unset database_dsn database_password worker_credential

server_name=$(python3 - "$authority_instance" <<'PY'
import base64
import hashlib
import sys

digest = hashlib.sha256(sys.argv[1].encode()).digest()
print(base64.b32encode(digest).decode().rstrip("=").lower() + ".authority.kdive.invalid")
PY
)
openssl ecparam -name prime256v1 -genkey -noout -out "$secrets/ca.key"
openssl req -x509 -new -sha256 -key "$secrets/ca.key" -days 1 \
  -subj /CN=kdive-2150-proof-ca -out "$secrets/ca.pem" \
  -addext basicConstraints=critical,CA:TRUE,pathlen:0 \
  -addext keyUsage=critical,keyCertSign,cRLSign

issue_certificate() {
  local name=$1 eku=$2 san=${3:-}
  openssl ecparam -name prime256v1 -genkey -noout -out "$secrets/$name.key"
  openssl req -new -sha256 -key "$secrets/$name.key" -subj "/CN=$name" \
    -out "$secrets/$name.csr"
  {
    echo '[proof]'
    echo 'basicConstraints=critical,CA:FALSE'
    echo 'keyUsage=critical,digitalSignature'
    echo "extendedKeyUsage=$eku"
    [[ -z $san ]] || echo "subjectAltName=DNS:$san"
  } >"$secrets/$name.ext"
  openssl x509 -req -sha256 -in "$secrets/$name.csr" -CA "$secrets/ca.pem" \
    -CAkey "$secrets/ca.key" -CAcreateserial -days 1 -extfile "$secrets/$name.ext" \
    -extensions proof -out "$secrets/$name.pem" >/dev/null 2>&1
}
issue_certificate server serverAuth "$server_name"
issue_certificate health-client clientAuth
issue_certificate proof-client clientAuth
chmod 0600 "$secrets"/*

python3 - "$authority_instance" >"$secrets/request.json" <<'PY'
import json
import sys
from uuid import uuid4

print(json.dumps({
    "schema": "external-boot-authority-v1",
    "authority_id": str(uuid4()),
    "generation": 1,
    "system_id": str(uuid4()),
    "activation_id": str(uuid4()),
    "run_id": str(uuid4()),
    "plan_identity": "sha256:" + "1" * 64,
    "purpose": "activate",
    "operation": "activate",
    "provider_kind": "local-libvirt",
    "authority_instance": sys.argv[1],
    "operation_identity": "issue-2150-proof",
    "operation_digest": "sha256:" + "2" * 64,
}, sort_keys=True, separators=(",", ":")))
PY
chmod 0600 "$secrets/request.json"

install -m 0600 /dev/null "$remote_root/vars.yml"
cat >"$remote_root/vars.yml" <<EOF
live_vm_venv: $remote_install
live_vm_repo_url: $remote_root/kdive.bundle
live_vm_repo_version: $revision
live_vm_host_authority_instance: $authority_instance
live_vm_host_authority_database_name: $database
live_vm_host_authority_database_login: $database_login
live_vm_host_authority_database_admin_dsn: postgresql:///$database?host=/var/run/postgresql
live_vm_host_authority_database_dsn_source: $secrets/database-dsn
live_vm_host_authority_server_key_source: $secrets/server.key
live_vm_host_authority_server_certificate_source: $secrets/server.pem
live_vm_host_authority_server_ca_source: $secrets/ca.pem
live_vm_host_authority_worker_client_ca_source: $secrets/ca.pem
live_vm_host_authority_health_client_certificate_source: $secrets/health-client.pem
live_vm_host_authority_health_client_key_source: $secrets/health-client.key
live_vm_host_authority_proof_enabled: true
live_vm_host_authority_proof_incarnation: $proof_incarnation
live_vm_host_authority_proof_client_certificate: $secrets/proof-client.pem
live_vm_host_authority_proof_client_key: $secrets/proof-client.key
live_vm_host_authority_proof_worker_credential: $secrets/worker-credential
live_vm_host_authority_proof_request: $secrets/request.json
EOF

echo "provision: installing exact revision $revision"
$ansible -i inventory proof.yml --extra-vars @vars.yml
installed=$(sudo -n cat /opt/kdive-provider-authority/revision)
[[ $installed == "$revision" ]]
[[ $(sudo -n -u github-runner git -C "$remote_install" rev-parse HEAD) == "$revision" ]]

run_teardown() {
  $ansible -i inventory "$source_root/deploy/ansible/playbooks/authority_host_teardown.yml" \
    --extra-vars "authority_database_admin_dsn=postgresql:///$database?host=/var/run/postgresql" \
    --extra-vars "authority_database_login=$database_login"
  sudo -n test ! -e /etc/systemd/system/kdive-external-boot-authority.service
  sudo -n test ! -e /run/kdive/provider-authority
  sudo -n test -d /etc/kdive/credentials/provider-authority
  sudo -n test -d /var/lib/kdive/provider-authority/journal
  ! getent passwd kdive-authority-2150-proof >/dev/null
  [[ $(sudo -n -u postgres psql -Atqc \
    "SELECT rolcanlogin FROM pg_roles WHERE rolname='$database_login'" postgres) == f ]]
  [[ $(sudo -n -u postgres psql -Atqc \
    "SELECT state FROM worker_incarnations WHERE incarnation='$proof_incarnation'" "$database") == terminated ]]
}

echo "teardown pass 1"
run_teardown
echo "teardown pass 2"
run_teardown

sudo -n rm -rf -- "/opt/kdive-provider-authority" "$remote_install"
sudo -n rm -rf -- "$remote_root"
echo "proof complete: exact revision $revision; retained credentials journal and inert database"
REMOTE_PROOF
