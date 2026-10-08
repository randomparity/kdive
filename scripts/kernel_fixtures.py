#!/usr/bin/env python3
"""Build and verify pinned external Linux fixtures (ADR-0693)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "fixtures/kernel/debug.config"
COMMON = (
    ".config",
    "input.config",
    "vmlinux",
    "System.map",
    "modules.order",
    "modules.builtin",
    "modules.builtin.modinfo",
    "include/config/kernel.release",
    "include/config/auto.conf",
)
REQUIRED = {"x86_64": (*COMMON, "arch/x86/boot/bzImage"), "ppc64le": COMMON}
ARCH = {"x86_64": "x86", "ppc64le": "powerpc"}


def command(argv: list[str], **kwargs: Any) -> str:
    stdout = None if kwargs.pop("stream", False) else subprocess.PIPE
    result = subprocess.run(argv, check=True, text=True, stdout=stdout, **kwargs)
    return (result.stdout or "").strip()


def baseline_selection(baseline: str) -> dict[str, str]:
    data = tomllib.loads((ROOT / "fixtures/kernel/baselines.toml").read_text())
    if baseline not in ("longterm", "stable"):
        raise ValueError("select baseline longterm or stable")
    return {"repository": data["repository"], **data[baseline]}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def artifact(output: Path, name: str) -> Path:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError(f"unsafe fixture artifact path: {name!r}")
    result = output / path
    if any(p.is_symlink() for p in (result, *result.parents) if p != output.parent):
        raise ValueError(f"fixture artifact must not use symlinks: {name}")
    if not result.is_file():
        raise ValueError(f"missing fixture artifact: {name}; rebuild in a fresh output directory")
    return result


def members(output: Path, arch: str) -> list[str]:
    if arch not in REQUIRED:
        raise ValueError("select explicit target x86_64 or ppc64le")
    modules = artifact(output, "modules.order").read_text().splitlines()
    if not modules or any(not name.endswith((".o", ".ko")) for name in modules):
        raise ValueError("modules.order must name module objects (.o or .ko)")
    modules = [name.removesuffix(".o") + ".ko" if name.endswith(".o") else name for name in modules]
    if "CONFIG_BUILTIN_MODULE_RANGES=y" in artifact(output, ".config").read_text().splitlines():
        modules.append("modules.builtin.ranges")
    return sorted(set((*REQUIRED[arch], *modules)))


def elf_build_id(path: Path) -> str:
    match = re.search(r"Build ID: ([0-9a-f]+)", command(["readelf", "-n", str(path)]))
    if match is None:
        raise ValueError("vmlinux has no GNU build ID; rebuild with the fixture configuration")
    return match[1]


def config_values(text: str) -> dict[str, str]:
    normalized = re.sub(r"^# (CONFIG_\w+) is not set$", r"\1=n", text, flags=re.MULTILINE)
    return dict(
        line.split("=", 1) for line in normalized.splitlines() if line.startswith("CONFIG_")
    )


def check_config(effective: Path, fragment: Path) -> None:
    actual = config_values(effective.read_text())
    for name, value in config_values(fragment.read_text()).items():
        if actual.get(name) != value:
            raise ValueError(f"kernel configuration dropped {name}={value}; check dependencies")


def apply_config(effective: Path, fragment: str) -> None:
    names = config_values(fragment).keys()
    retained = [
        line
        for line in effective.read_text().splitlines()
        if not names & config_values(line).keys()
    ]
    effective.write_text("\n".join(retained) + "\n" + fragment)


def record(
    output: Path,
    *,
    baseline: str,
    arch: str,
    config_digest: str,
    toolchain: dict[str, str],
    builder: str,
    source_epoch: int,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": 1,
        "baseline": baseline,
        "arch": arch,
        "source": baseline_selection(baseline),
        "source_epoch": source_epoch,
        "fragment_sha256": config_digest,
        "toolchain": toolchain,
        "builder_commit": builder,
        "release": artifact(output, "include/config/kernel.release").read_text().strip(),
        "build_id": elf_build_id(artifact(output, "vmlinux")),
        "artifacts": {name: digest(artifact(output, name)) for name in members(output, arch)},
    }
    data["fixture_id"] = identity(data)
    temporary = output / "manifest.json.tmp"
    temporary.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    temporary.replace(output / "manifest.json")
    return data


def verify_packaging_source(output: Path, commit: str) -> None:
    source = (output / "source").resolve(strict=True)
    expected = (
        f"# Automatically generated by {source}/Makefile: don't edit\n"
        f"export KBUILD_OUTPUT = {output.resolve()}\ninclude {source}/Makefile\n"
    )
    if artifact(output, "Makefile").read_text() != expected:
        raise ValueError("fixture Makefile linkage changed; restore the original or rebuild")
    command(
        ["bash", str(ROOT / "scripts/fetch-kernel-tree.sh"), str(source)],
        env={"PATH": os.environ.get("PATH", os.defpath), "KDIVE_KERNEL_REF": commit},
    )


def verify(output: Path, *, baseline: str, arch: str) -> dict[str, Any]:
    try:
        data = json.loads(artifact(output, "manifest.json").read_text())
        if not isinstance(data, dict):
            raise ValueError("fixture manifest must be an object")
        fixture_id = data.pop("fixture_id")
        if fixture_id != identity(data):
            raise ValueError("fixture manifest identity mismatch")
        if (
            data["schema"] != 1
            or data["baseline"] != baseline
            or data["arch"] != arch
            or data["source"] != baseline_selection(baseline)
        ):
            raise ValueError("fixture selection mismatch; rebuild the selected baseline and target")
        if (
            not isinstance(data["source_epoch"], int)
            or data["source_epoch"] < 0
            or not isinstance(data["toolchain"], dict)
            or not data["toolchain"]
            or any(not isinstance(v, str) or not v for v in data["toolchain"].values())
        ):
            raise ValueError("invalid fixture toolchain or source timestamp")
        for key, pattern in (
            ("builder_commit", r"[0-9a-f]{40}"),
            ("fragment_sha256", r"[0-9a-f]{64}"),
            ("build_id", r"[0-9a-f]+"),
        ):
            if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
                raise ValueError(f"invalid fixture {key}")
        expected = members(output, arch)
        if not isinstance(data["artifacts"], dict) or sorted(data["artifacts"]) != expected:
            raise ValueError("fixture artifact inventory mismatch")
        for name, checksum in data["artifacts"].items():
            if digest(artifact(output, name)) != checksum:
                raise ValueError(f"fixture bytes changed: {name}")
        if (
            data["fragment_sha256"] != data["artifacts"]["input.config"]
            or data["build_id"] != elf_build_id(output / "vmlinux")
            or data["release"] != (output / "include/config/kernel.release").read_text().strip()
        ):
            raise ValueError("fixture ELF or release identity mismatch")
        verify_packaging_source(output, data["source"]["commit"])
        data["fixture_id"] = fixture_id
        return data
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError(
            f"invalid fixture manifest: {exc}; rebuild in a fresh output directory"
        ) from exc


def toolchain_identity() -> dict[str, str]:
    result = {
        tool: command([tool, "--version"]).splitlines()[0]
        for tool in ("gcc", "make", "readelf", "pahole", "bison", "flex")
    }
    for query in (
        ["dpkg-query", "-W", "-f=${Package}=${Version}\n"],
        ["rpm", "-qa", "--qf", "%{NAME}=%{VERSION}-%{RELEASE}\n"],
    ):
        if shutil.which(query[0]) and (packages := command(query)):
            result["packages"] = packages
            return result
    raise ValueError("fixture provenance requires a nonempty dpkg-query or rpm package inventory")


def build(
    source: Path, output: Path, *, baseline: str, arch: str, config: Path, jobs: int
) -> dict[str, Any]:
    selected = baseline_selection(baseline)
    if arch not in ARCH or jobs < 1:
        raise ValueError("select x86_64 or ppc64le and a positive job count")
    if platform.machine() != arch:
        raise ValueError(f"native {arch} host required for this fixture build")
    source, output, config = source.resolve(), output.resolve(), config.resolve()
    if output.exists() or source == output or source in output.parents or output in source.parents:
        raise ValueError("choose a fresh output directory separate from source")
    fragment = config.read_text()
    # x86 capture needs fw_cfg; RHEL dracut also needs erofs and overlay to arm kdump.
    if arch == "x86_64" and config == CONFIG.resolve():
        fragment += (
            "CONFIG_FW_CFG_SYSFS=y\nCONFIG_EROFS_FS=y\n"
            "CONFIG_EROFS_FS_ZIP_LZMA=y\nCONFIG_OVERLAY_FS=y\n"
        )
    toolchain = toolchain_identity()
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LANGUAGE") or k.startswith("LC_")
    }
    env.update(KDIVE_KERNEL_REPO=selected["repository"], KDIVE_KERNEL_REF=selected["commit"])
    fetch = ["bash", str(ROOT / "scripts/fetch-kernel-tree.sh"), str(source)]
    command(fetch, env=env)
    epoch = int(command(["git", "-C", str(source), "show", "-s", "--format=%ct", "HEAD"], env=env))
    builder = command(["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=env)
    env.update(
        KBUILD_BUILD_USER="builder",
        KBUILD_BUILD_HOST="kernel-fixture",
        KBUILD_BUILD_VERSION="1",
        KBUILD_BUILD_TIMESTAMP=datetime.fromtimestamp(epoch, UTC).strftime(
            "%a %b %d %H:%M:%S UTC %Y"
        ),
    )
    flags = (
        f"-fdebug-prefix-map={source}=/usr/src/linux "
        f"-fdebug-prefix-map={output}=/usr/src/linux-build"
    )
    env.update(KCFLAGS=flags, KAFLAGS=flags)
    output.mkdir(parents=True)
    (output / "input.config").write_text(fragment)
    make = ["make", "-C", str(source), f"O={output}", f"ARCH={ARCH[arch]}"]
    command([*make, "ppc64le_defconfig" if arch == "ppc64le" else "defconfig"], env=env)
    apply_config(output / ".config", fragment)
    command([*make, "olddefconfig"], env=env)
    check_config(output / ".config", output / "input.config")
    targets = ["vmlinux", "modules"] + (["bzImage"] if arch == "x86_64" else [])
    command([*make, f"-j{jobs}", *targets], env=env, stream=True)
    command(fetch, env=env)
    return record(
        output,
        baseline=baseline,
        arch=arch,
        config_digest=digest(output / "input.config"),
        toolchain=toolchain,
        builder=builder,
        source_epoch=epoch,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--baseline", choices=("longterm", "stable"), required=True)
    parser.add_argument("--arch", choices=tuple(ARCH), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    try:
        if args.action == "build":
            if args.source is None:
                parser.error("build requires --source")
            data = build(
                args.source,
                args.output,
                baseline=args.baseline,
                arch=args.arch,
                config=args.config,
                jobs=args.jobs,
            )
        else:
            data = verify(args.output, baseline=args.baseline, arch=args.arch)
        print(json.dumps(data, sort_keys=True, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
