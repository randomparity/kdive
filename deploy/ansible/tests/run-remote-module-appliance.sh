#!/bin/sh
set -eu

test_root=$(mktemp -d)
trap 'chmod -R u+w "$test_root" 2>/dev/null || :; rm -rf "$test_root"' EXIT HUP INT TERM
source_dir="$test_root/source"
install_dir="$test_root/install"
mkdir -p "$source_dir/image"
printf 'kernel\n' >"$source_dir/image/vmlinuz"
printf 'initramfs\n' >"$source_dir/image/initramfs.cpio"
printf '{"format":"kdive-remote-module-appliance-v1"}\n' >"$source_dir/manifest.json"

for arch in x86_64 ppc64le; do
  tar -cf "$test_root/appliance-v1-$arch.tar" -C "$source_dir" image manifest.json
done

python - "$test_root" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
images = {}
for arch in ("x86_64", "ppc64le"):
    archive = root / f"appliance-v1-{arch}.tar"
    images[arch] = {
        "url": archive.resolve().as_uri(),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
variables = {
    "remote_libvirt_module_appliance_images": images,
    "remote_libvirt_module_appliance_install_dir": str(root / "install"),
    "remote_libvirt_module_appliance_owner": str(os.getuid()),
    "remote_libvirt_module_appliance_group": str(os.getgid()),
}
(root / "vars.json").write_text(json.dumps(variables), encoding="utf-8")
PY

export ANSIBLE_CONFIG=deploy/ansible/ansible.cfg
export ANSIBLE_ROLES_PATH=deploy/ansible/roles
playbook=deploy/ansible/tests/remote-module-appliance.yml
ansible-playbook "$playbook" -i localhost, -e "@$test_root/vars.json"
second_log="$test_root/second.log"
if ! ansible-playbook "$playbook" -i localhost, -e "@$test_root/vars.json" >"$second_log" 2>&1; then
  cat "$second_log"
  exit 1
fi
cat "$second_log"
grep -Eq 'changed=0([[:space:]].*)?failed=0([[:space:]]|$)' "$second_log"

for arch in x86_64 ppc64le; do
  test -f "$install_dir/$arch/image/vmlinuz"
  test -f "$install_dir/$arch/image/initramfs.cpio"
  test -f "$install_dir/$arch/manifest.json"
done

python - "$test_root/vars.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
variables = json.loads(path.read_text(encoding="utf-8"))
variables["remote_libvirt_module_appliance_images"]["x86_64"]["sha256"] = "0" * 64
path.write_text(json.dumps(variables), encoding="utf-8")
PY
rm "$install_dir/appliance-v1-x86_64.tar"
if ansible-playbook "$playbook" -i localhost, -e "@$test_root/vars.json"; then
  echo "bad module-appliance digest unexpectedly passed" >&2
  exit 1
fi
