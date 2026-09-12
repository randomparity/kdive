#!/usr/bin/env bash
# Build one or more kdive-ready guest images from the rootfs catalog and register each in the
# local inventory so an MCP agent can provision it by catalog name. Idempotent per image.
#
#   examples/local-libvirt/build-image.sh fedora-kdive-ready-44 [debian-kdive-ready-13 ...]
#
# Per image: `python -m kdive build-fs --image NAME` publishes
# /var/lib/kdive/rootfs/local/NAME.qcow2 (+ its provenance sidecar); on an SELinux-enforcing
# host the rootfs directory is labeled svirt_image_t so the confined domain can use it; a
# `staged-path` [[image]] block is appended to systems.toml (skipped when one already declares
# NAME) and `reconcile-systems` loads it into the catalog. Needs the backends up (up.sh) for
# the reconcile step. The catalog names come from fixtures/local-libvirt/rootfs_catalog.toml.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"
# shellcheck source=examples/local-libvirt/selinux-label.sh disable=SC1091
source "${example_dir}/selinux-label.sh"

if (($# == 0)); then
  echo "usage: build-image.sh CATALOG_IMAGE [CATALOG_IMAGE...]" >&2
  echo "  catalog names: fixtures/local-libvirt/rootfs_catalog.toml (e.g. fedora-kdive-ready-44)" >&2
  exit 2
fi

# A user-writable workspace: the build-fs default (/var/lib/kdive/build/images) is root-owned.
workspace="${KDIVE_BUILD_IMAGE_WORKSPACE:-${XDG_DATA_HOME:-${HOME}/.local/share}/kdive/build/images}"
rootfs_dir=/var/lib/kdive/rootfs/local
systems_toml="${KDIVE_SYSTEMS_TOML:-${HOME}/.config/kdive/systems.toml}"

step() { printf '\n=== %s ===\n' "$1"; }

# Run reconcile-systems under the reconciler's own database authority (#1929): the inventory
# reconcile is the reconciler process's job, and no other authority's DSN is exposed to it.
reconcile() {
  KDIVE_DATABASE_URL="${KDIVE_RECONCILER_DATABASE_URL}" \
    env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
    -u KDIVE_WORKER_DATABASE_URL \
    "${KDIVE_PYTHON}" -m kdive reconcile-systems "$@"
}

mkdir -p "${workspace}" "$(dirname "${systems_toml}")"
if [[ ! -e "${systems_toml}" ]]; then
  printf 'schema_version = 2\n' >"${systems_toml}"
  echo "created ${systems_toml}"
fi

for name in "$@"; do
  qcow2="${rootfs_dir}/${name}.qcow2"

  step "build-fs --image ${name}"
  # stdout is the one-line KDIVE_GUEST_IMAGE export (eval-safe); the human summary is on stderr.
  "${KDIVE_PYTHON}" -m kdive build-fs --image "${name}" --workspace "${workspace}"
  [[ -f "${qcow2}" ]] || {
    echo "build-fs did not publish ${qcow2}" >&2
    exit 1
  }
  # Label the rootfs directory on SELinux-enforcing hosts only (Fedora/EL). A qcow2 published from
  # a $HOME workspace can carry data_home_t, which the confined domain cannot read (ADR-0640).
  kdive_label_svirt_image "${rootfs_dir}"

  # Declare the image from its own provenance sidecar (arch + baked capabilities), so the
  # [[image]] block never disagrees with what build-fs actually produced.
  step "register ${name} in ${systems_toml}"
  "${KDIVE_PYTHON}" - "${systems_toml}" "${name}" "${qcow2}" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

systems_toml, name, qcow2 = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])

declared = tomllib.loads(systems_toml.read_text()).get("image", [])
if any(row.get("provider") == "local-libvirt" and row.get("name") == name for row in declared):
    print(f"{name} already declared in {systems_toml}; leaving it as is")
    raise SystemExit(0)

sidecar = json.loads(Path(f"{qcow2}.provenance.json").read_text())["provenance"]
capabilities = ", ".join(f'"{cap}"' for cap in sidecar["capabilities"])
block = f"""
[[image]]
provider = "local-libvirt"
name = "{name}"
arch = "{sidecar["arch"]}"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = [{capabilities}]
[image.source]
kind = "staged-path"
path = "{qcow2}"
"""
with systems_toml.open("a") as handle:
    handle.write(block)
print(f"appended [[image]] {name} ({sidecar['arch']}, {capabilities}) to {systems_toml}")
PY

  step "reconcile-systems"
  reconcile --check
  reconcile
done

cat <<EOF

image(s) registered: $*
An agent discovers them with images.list / systems.profile_examples and provisions with
  rootfs = {kind = "catalog", provider = "local-libvirt", name = "<name>"}
EOF
