#!/bin/bash
# Issue the external-boot authority CA and its TLS leaves into one private directory
# (ADR-0803). The disposable proof carrier and retained lanes both use it.
set -euo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
usage: authority-pki.sh ca --dir DIR --subject CN --days N --key new|existing
       authority-pki.sh leaf --dir DIR --name NAME --usage server|client --days N
                             --key new|existing [--authority-instance INSTANCE]
       authority-pki.sh server-name --authority-instance INSTANCE
EOF
  exit 2
}

die() {
  echo "authority-pki: $*" >&2
  exit 2
}

temporaries=()
cleanup() {
  [[ ${#temporaries[@]} -eq 0 ]] || rm -rf -- "${temporaries[@]}"
}
trap cleanup EXIT

dir="" subject="" days="" key="" name="" usage_kind="" instance="" temporary="" key_file=""
parse_options() {
  while [[ $# -gt 0 ]]; do
    [[ $# -ge 2 ]] || usage
    case $1 in
    --dir) dir=$2 ;;
    --subject) subject=$2 ;;
    --days) days=$2 ;;
    --key) key=$2 ;;
    --name) name=$2 ;;
    --usage) usage_kind=$2 ;;
    --authority-instance) instance=$2 ;;
    *) usage ;;
    esac
    shift 2
  done
}

server_name() {
  # Same derivation as kdive.providers.external_boot_authority.protocol.authority_server_name.
  python3 - "$1" <<'PY'
import base64
import hashlib
import sys

digest = hashlib.sha256(sys.argv[1].encode()).digest()
print(base64.b32encode(digest).decode().rstrip("=").lower() + ".authority.kdive.invalid")
PY
}

require_days_and_key() {
  # Six digits bound the shell arithmetic below; the operator chooses any lifetime inside it.
  [[ $days =~ ^[1-9][0-9]{0,5}$ ]] || die "--days must be a whole number of days from 1 to 999999"
  [[ $key == new || $key == existing ]] || die "--key must be new or existing"
}

# Keys stay in an operator-owned 0700 directory outside every Git work tree: an ignored path
# inside a checkout is deleted with its worktree, and a tracked one can be committed.
require_private_dir() {
  local out status=0
  [[ -n $dir ]] || usage
  # A trailing slash or a . or .. component would make the symlink test resolve through a link.
  while [[ $dir == */ && $dir != / ]]; do dir=${dir%/}; done
  [[ /$dir/ != */./* && /$dir/ != */../* ]] || die "$dir must not contain . or .. components"
  [[ -d $dir && ! -L $dir ]] || die "$dir must be an existing directory, not a symlink"
  [[ $(stat -c %u -- "$dir") == "$(id -u)" ]] || die "$dir must be owned by the invoking user"
  [[ $(stat -c %a -- "$dir") == 700 ]] || die "$dir must have mode 0700"
  command -v git >/dev/null || die "git is needed to prove $dir is outside every Git work tree"
  out=$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_CEILING_DIRECTORIES LC_ALL=C LANGUAGE= \
    git -C "$dir" rev-parse --is-inside-work-tree --is-inside-git-dir 2>&1) || status=$?
  if [[ $status -eq 0 ]]; then
    [[ $out == $'false\nfalse' ]] ||
      die "$dir is inside a Git work tree or .git directory; keep keys outside every checkout"
  elif [[ $status -ne 128 || $out != *"not a git repository"* ]]; then
    die "cannot prove $dir is outside every Git work tree: $out"
  fi
}

# Sets temporary: a private file beside its final name, so the later mv is an atomic rename.
temporary_in_dir() {
  temporary=$(mktemp "$dir/.$1.XXXXXX")
  temporaries+=("$temporary")
}

spki_sha256() {
  openssl x509 -noout -pubkey -in "$1" | openssl pkey -pubin -outform DER | sha256sum | cut -d' ' -f1
}

key_spki_sha256() {
  openssl pkey -in "$1" -pubout -outform DER | sha256sum | cut -d' ' -f1
}

# Sets key_file: a fresh key for --key new, the existing key for --key existing.
prepare_key() {
  local target=$1 label=$2
  if [[ $key == new ]]; then
    [[ ! -e $target ]] || die "$label already exists in $dir; use --key existing to renew"
    temporary_in_dir "$label"
    key_file=$temporary
    openssl ecparam -name prime256v1 -genkey -noout -out "$key_file"
  else
    [[ -f $target ]] || die "$label does not exist in $dir; use --key new to create it"
    key_file=$target
  fi
}

install_issued() {
  local certificate=$1 key_file=$2 target_pem=$3 target_key=$4
  [[ $(spki_sha256 "$certificate") == "$(key_spki_sha256 "$key_file")" ]] ||
    die "issued $(basename "$target_pem") does not match its key"
  [[ $key_file == "$target_key" ]] || mv -- "$key_file" "$target_key"
  mv -- "$certificate" "$target_pem"
  printf 'issued %s notAfter=%s spki-sha256=%s\n' "$(basename "$target_pem")" \
    "$(openssl x509 -noout -enddate -in "$target_pem" | cut -d= -f2)" "$(spki_sha256 "$target_pem")"
}

command_ca() {
  local leaf current
  [[ -n $subject ]] || usage
  require_days_and_key
  require_private_dir
  if [[ $key == existing && -f $dir/ca.pem ]]; then
    current=$(openssl x509 -noout -subject -nameopt RFC2253 -in "$dir/ca.pem")
    [[ $current == "subject=CN=$subject" ]] ||
      die "--subject $subject does not match the current ca.pem ($current)"
  fi
  shopt -s nullglob
  for leaf in "$dir"/*.pem; do
    [[ $leaf == "$dir/ca.pem" ]] && continue
    ! openssl x509 -noout -checkend $((days * 86400)) -in "$leaf" >/dev/null ||
      die "$(basename "$leaf") would outlive a $days-day CA certificate; choose a longer --days"
  done
  prepare_key "$dir/ca.key" ca.key
  temporary_in_dir ca.pem
  openssl req -x509 -new -sha256 -key "$key_file" -days "$days" -subj "/CN=$subject" \
    -out "$temporary" \
    -addext basicConstraints=critical,CA:TRUE,pathlen:0 \
    -addext keyUsage=critical,keyCertSign,cRLSign
  openssl verify -x509_strict -CAfile "$temporary" "$temporary" >/dev/null
  install_issued "$temporary" "$key_file" "$dir/ca.pem" "$dir/ca.key"
}

command_leaf() {
  local work purpose eku
  [[ $name =~ ^[a-z0-9][a-z0-9-]*$ && $name != ca ]] ||
    die "--name must be lowercase letters, digits and hyphens, and not ca"
  case $usage_kind in
  server)
    [[ -n $instance ]] || die "--usage server requires --authority-instance"
    purpose=sslserver eku=serverAuth
    ;;
  client)
    [[ -z $instance ]] || die "--usage client does not take --authority-instance"
    purpose=sslclient eku=clientAuth
    ;;
  *) die "--usage must be server or client" ;;
  esac
  require_days_and_key
  require_private_dir
  [[ -f $dir/ca.key && -f $dir/ca.pem ]] ||
    die "ca.key does not exist in $dir; run the ca command first"
  openssl x509 -noout -checkend $((days * 86400)) -in "$dir/ca.pem" >/dev/null ||
    die "CA certificate expires before a $days-day $name certificate; renew it with ca --key existing"
  prepare_key "$dir/$name.key" "$name.key"
  work=$(mktemp -d)
  temporaries+=("$work")
  openssl req -new -sha256 -key "$key_file" -subj "/CN=$name" -out "$work/request.csr"
  {
    echo '[proof]'
    echo 'basicConstraints=critical,CA:FALSE'
    echo 'keyUsage=critical,digitalSignature'
    echo "extendedKeyUsage=$eku"
    [[ $usage_kind != server ]] || echo "subjectAltName=DNS:$(server_name "$instance")"
  } >"$work/request.ext"
  temporary_in_dir "$name.pem"
  openssl x509 -req -sha256 -in "$work/request.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" \
    -CAserial "$dir/ca.srl" -CAcreateserial -days "$days" -extfile "$work/request.ext" \
    -extensions proof -out "$temporary" 2>"$work/sign.err" ||
    die "signing $name failed: $(cat "$work/sign.err")"
  openssl verify -x509_strict -purpose "$purpose" -CAfile "$dir/ca.pem" "$temporary" >/dev/null
  install_issued "$temporary" "$key_file" "$dir/$name.pem" "$dir/$name.key"
}

[[ $# -ge 1 ]] || usage
command=$1
shift
parse_options "$@"
case $command in
ca) command_ca ;;
leaf) command_leaf ;;
server-name)
  [[ -n $instance ]] || usage
  server_name "$instance"
  ;;
*) usage ;;
esac
