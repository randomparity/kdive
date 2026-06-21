"""Writeback adapter seam for ``ops.export_systems_toml(persist=...)`` (#641, ADR-0199).

Three layers, each at its own boundary, none touching a real cluster:

* the skeleton guard — pure text check, shared marker with the serializer (drift test);
* the fake adapter — records the last write for the tool tests;
* the two real adapters + the factory — file writes to a tmp dir; the ConfigMap adapter's HTTP
  boundary is mocked (it is the external service). Secret material never reaches an error/detail.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory import serialize, writeback


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


@pytest.fixture(autouse=True)
def _block_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every adapter test here is fully offline: file writes go to tmp dirs and the ConfigMap
    # HTTP boundary is exercised through httpx.MockTransport. Block real socket connections so a
    # mutant that bypasses the injected transport (building a real-network client) fails fast and
    # deterministically instead of hanging on a connect timeout.
    import socket

    def _refuse(*args: object, **kwargs: object) -> None:
        raise OSError("real network access is blocked in this test module")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


# ---- skeleton guard -------------------------------------------------------------------


def _defined_image() -> serialize.ImageRow:
    return serialize.ImageRow(
        provider="remote_libvirt",
        name="img-a",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key=None,
        digest=None,
        volume=None,
        state="defined",
    )


def _remote_row() -> serialize.ResourceRow:
    return serialize.ResourceRow(
        name="host-a",
        cost_class="remote",
        pool="remote",
        host_uri="qemu+tls://host/system",
        vcpus=8,
        memory_mb=16384,
        concurrent_allocation_cap=2,
        seed=None,
    )


def _empty_snapshot(**rows: object) -> serialize.InventorySnapshot:
    base: dict[str, object] = {
        "images": (),
        "remote_libvirt": (),
        "local_libvirt": (),
        "fault_inject": (),
        "build_hosts": (),
        "cost_classes": (),
    }
    base.update(rows)
    return serialize.InventorySnapshot(**base)  # type: ignore[arg-type]


def test_assert_persistable_passes_clean_document() -> None:
    writeback.assert_persistable('name = "host-a"\nuri = "qemu+tls://h/system"\n')


def test_assert_persistable_rejects_a_placeholder() -> None:
    text = f'gdb_addr = "{serialize.REMOTE_PLACEHOLDER_PREFIX}gdb_addr"\n'
    with pytest.raises(CategorizedError) as exc:
        writeback.assert_persistable(text)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # the marker is named so the operator knows what to complete; no secret echoed.
    assert serialize.REMOTE_PLACEHOLDER_PREFIX in str(exc.value)
    # the details carry the marker under the exact key the tool/log reads.
    assert exc.value.details == {"marker": writeback.WRITEBACK_PLACEHOLDER_MARKER}


def test_guard_marker_matches_a_freshly_serialized_skeleton() -> None:
    # Drift guard: serialize a real skeleton (a remote host AND a defined image, the two
    # placeholder sites) and assert the guard's marker is present in the output, so changing
    # either placeholder prefix without the guard would fail this test.
    rendered = serialize.serialize_inventory(
        _empty_snapshot(images=(_defined_image(),), remote_libvirt=(_remote_row(),))
    )
    assert writeback.WRITEBACK_PLACEHOLDER_MARKER in rendered
    with pytest.raises(CategorizedError):
        writeback.assert_persistable(rendered)


def test_guard_does_not_flag_a_clean_export() -> None:
    # The export header explains REPLACE_ME_* in prose; a clean inventory (no remote host, no
    # defined image) must NOT trip the guard even though the header mentions the marker.
    rendered = serialize.serialize_inventory(
        _empty_snapshot(
            build_hosts=(
                serialize.BuildHostRow(
                    name="bh-local",
                    kind="local",
                    base_image_volume=None,
                    workspace_root="/var/lib/kdive/build",
                    max_concurrent=4,
                ),
            )
        )
    )
    assert "REPLACE_ME_" in rendered  # the header prose mentions it
    writeback.assert_persistable(rendered)  # but the guard does not fire


# ---- serialize_inventory (exact output) -----------------------------------------------


def _body(snapshot: serialize.InventorySnapshot) -> str:
    """Serialized output with the explanatory ``_HEADER`` prose stripped.

    The header documents the REPLACE_ME_* placeholders and section names in comments, so
    substring assertions about the emitted blocks must look only at the rendered body.
    """
    rendered = serialize.serialize_inventory(snapshot)
    assert rendered.startswith(serialize._HEADER)
    return rendered[len(serialize._HEADER) :]


def _resource_row(name: str, **over: object) -> serialize.ResourceRow:
    base: dict[str, object] = {
        "name": name,
        "cost_class": "cc",
        "pool": "pool",
        "host_uri": "qemu:///system",
        "vcpus": 4,
        "memory_mb": 2048,
        "concurrent_allocation_cap": 1,
        "seed": None,
    }
    base.update(over)
    return serialize.ResourceRow(**base)  # type: ignore[arg-type]


def test_serialize_empty_snapshot_emits_header_and_schema_only() -> None:
    rendered = serialize.serialize_inventory(_empty_snapshot())
    assert rendered == serialize._HEADER + "\nschema_version = 2\n"
    assert "schema_version = 2" in rendered


def test_serialize_image_s3_source_with_digest_exact() -> None:
    image = serialize.ImageRow(
        provider="remote_libvirt",
        name="img-a",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=["a", "b"],
        object_key="objects/img-a.qcow2",
        digest="sha256:deadbeef",
        volume=None,
        state="built",
    )
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(image,)))
    assert rendered == serialize._HEADER + "\n".join(
        [
            "",
            "schema_version = 2",
            "",
            "[[image]]",
            'provider = "remote_libvirt"',
            'name = "img-a"',
            'arch = "x86_64"',
            'format = "qcow2"',
            'root_device = "/dev/vda"',
            'visibility = "public"',
            'capabilities = ["a", "b"]',
            "[image.source]",
            'kind = "s3"',
            'object_key = "objects/img-a.qcow2"',
            'digest = "sha256:deadbeef"',
            "",
        ]
    )


def test_serialize_image_staged_source_takes_precedence_over_object_key() -> None:
    image = serialize.ImageRow(
        provider="local_libvirt",
        name="img-staged",
        arch="aarch64",
        format="raw",
        root_device="/dev/vda",
        visibility="private",
        capabilities=[],
        object_key="should-be-ignored",
        digest="should-be-ignored",
        volume="pool/vol-1",
        state="staged",
    )
    body = _body(_empty_snapshot(images=(image,)))
    # Exact source block: the staged kind key must be emitted verbatim (an off-by-one in
    # the literal would still satisfy a substring check, so anchor on whole lines).
    source_block = "\n".join(
        [
            "[image.source]",
            'kind = "staged"',
            'volume = "pool/vol-1"',
        ]
    )
    assert source_block in body
    assert "[image.source]\n" in body
    # the staged branch wins: neither the object_key nor digest leak through.
    assert "should-be-ignored" not in body
    assert "object_key = " not in body
    assert "digest = " not in body
    assert "capabilities = []" in body


def test_serialize_image_s3_source_without_digest_omits_digest() -> None:
    image = serialize.ImageRow(
        provider="local_libvirt",
        name="img-nodigest",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key="objects/x",
        digest=None,
        volume=None,
        state="built",
    )
    body = _body(_empty_snapshot(images=(image,)))
    assert 'object_key = "objects/x"' in body
    assert "digest = " not in body


def test_serialize_defined_image_emits_object_key_placeholder() -> None:
    body = _body(_empty_snapshot(images=(_defined_image(),)))
    placeholder = f"{serialize.REMOTE_PLACEHOLDER_PREFIX}object_key"
    expected_source = "\n".join(
        [
            "[image.source]",
            'kind = "s3"',
            f'object_key = "{placeholder}"',
            "",
        ]
    )
    assert expected_source in body
    # the table header is the lowercase TOML key the reconciler parses, never uppercased.
    assert "[image.source]" in body
    assert "[IMAGE.SOURCE]" not in body


def test_serialize_defined_image_table_header_is_lowercase() -> None:
    # A defined image (no object_key, no volume) takes the placeholder branch; its
    # [image.source] header must be emitted verbatim so the document parses.
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(_defined_image(),)))
    source_lines = [line for line in rendered.splitlines() if line == "[image.source]"]
    assert source_lines == ["[image.source]"]


def test_serialize_images_sorted_by_provider_then_name_then_arch() -> None:
    def img(provider: str, name: str, arch: str) -> serialize.ImageRow:
        return serialize.ImageRow(
            provider=provider,
            name=name,
            arch=arch,
            format="qcow2",
            root_device="/dev/vda",
            visibility="public",
            capabilities=[],
            object_key="k",
            digest=None,
            volume=None,
            state="built",
        )

    # Supplied deliberately out of order so a dropped/constant sort key reorders the output.
    snapshot = _empty_snapshot(
        images=(
            img("zeta", "b", "x86_64"),
            img("alpha", "b", "x86_64"),
            img("alpha", "a", "x86_64"),
            img("alpha", "a", "aarch64"),
        )
    )
    rendered = serialize.serialize_inventory(snapshot)
    order = [
        rendered.index('provider = "alpha"\nname = "a"\narch = "aarch64"'),
        rendered.index('provider = "alpha"\nname = "a"\narch = "x86_64"'),
        rendered.index('provider = "alpha"\nname = "b"'),
        rendered.index('provider = "zeta"'),
    ]
    assert order == sorted(order)


def test_serialize_remote_block_exact_with_placeholders() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(remote_libvirt=(_resource_row("host-a", host_uri="qemu+tls://h/system"),))
    )
    expected_block = "\n".join(
        [
            "[[remote_libvirt]]",
            'name = "host-a"',
            'cost_class = "cc"',
            'pool = "pool"',
            "concurrent_allocation_cap = 1",
            'uri = "qemu+tls://h/system"',
            "vcpus = 4",
            "memory_mb = 2048",
            f'gdb_addr = "{serialize.REMOTE_PLACEHOLDER_PREFIX}gdb_addr"',
            f'gdbstub_range = "{serialize.REMOTE_PLACEHOLDER_PREFIX}gdbstub_range"',
            f'client_cert_ref = "{serialize.REMOTE_PLACEHOLDER_PREFIX}client_cert_ref"',
            f'client_key_ref = "{serialize.REMOTE_PLACEHOLDER_PREFIX}client_key_ref"',
            f'ca_cert_ref = "{serialize.REMOTE_PLACEHOLDER_PREFIX}ca_cert_ref"',
            f'base_image = "{serialize.REMOTE_PLACEHOLDER_PREFIX}base_image"',
            "shapes = []",
            "",
        ]
    )
    assert expected_block in rendered


def test_serialize_remote_missing_vcpus_raises() -> None:
    with pytest.raises(ValueError) as exc:
        serialize.serialize_inventory(
            _empty_snapshot(remote_libvirt=(_resource_row("host-a", vcpus=None),))
        )
    # the message names the exact missing capability so the operator can fix the row.
    assert "the required vcpus capability" in str(exc.value)
    assert "'host-a'" in str(exc.value)


def test_serialize_remote_missing_memory_raises() -> None:
    with pytest.raises(ValueError) as exc:
        serialize.serialize_inventory(
            _empty_snapshot(remote_libvirt=(_resource_row("host-a", memory_mb=None),))
        )
    assert "the required memory_mb capability" in str(exc.value)
    assert "'host-a'" in str(exc.value)


def test_serialize_local_block_exact() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(local_libvirt=(_resource_row("local-1", host_uri="qemu:///system"),))
    )
    expected_block = "\n".join(
        [
            "[[local_libvirt]]",
            'name = "local-1"',
            'cost_class = "cc"',
            'pool = "pool"',
            "concurrent_allocation_cap = 1",
            'host_uri = "qemu:///system"',
            "",
        ]
    )
    assert expected_block in rendered


def test_serialize_fault_block_exact_with_seed_default_zero() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(fault_inject=(_resource_row("f-1", seed=None),))
    )
    expected_block = "\n".join(
        [
            "[[fault_inject]]",
            'name = "f-1"',
            'cost_class = "cc"',
            'pool = "pool"',
            "concurrent_allocation_cap = 1",
            "vcpus = 4",
            "memory_mb = 2048",
            "seed = 0",
            "",
        ]
    )
    assert expected_block in rendered


def test_serialize_fault_block_emits_explicit_seed() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(fault_inject=(_resource_row("f-1", seed=7),))
    )
    assert "seed = 7" in rendered
    assert "seed = 0" not in rendered


def test_serialize_fault_missing_vcpus_raises_named_error() -> None:
    with pytest.raises(ValueError) as exc:
        serialize.serialize_inventory(
            _empty_snapshot(fault_inject=(_resource_row("f-1", vcpus=None),))
        )
    assert "the required vcpus capability" in str(exc.value)
    assert "'f-1'" in str(exc.value)


def test_serialize_fault_missing_memory_raises_named_error() -> None:
    with pytest.raises(ValueError) as exc:
        serialize.serialize_inventory(
            _empty_snapshot(fault_inject=(_resource_row("f-1", memory_mb=None),))
        )
    assert "the required memory_mb capability" in str(exc.value)
    assert "'f-1'" in str(exc.value)


def test_serialize_build_host_with_base_image_volume_exact() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(
            build_hosts=(
                serialize.BuildHostRow(
                    name="bh-1",
                    kind="remote",
                    base_image_volume="pool/base",
                    workspace_root="/var/lib/kdive/build",
                    max_concurrent=4,
                ),
            )
        )
    )
    expected_block = "\n".join(
        [
            "[[build_host]]",
            'name = "bh-1"',
            'kind = "remote"',
            'workspace_root = "/var/lib/kdive/build"',
            "max_concurrent = 4",
            'base_image_volume = "pool/base"',
            "",
        ]
    )
    assert expected_block in rendered


def test_serialize_build_host_without_volume_omits_field() -> None:
    rendered = serialize.serialize_inventory(
        _empty_snapshot(
            build_hosts=(
                serialize.BuildHostRow(
                    name="bh-1",
                    kind="local",
                    base_image_volume=None,
                    workspace_root="/ws",
                    max_concurrent=2,
                ),
            )
        )
    )
    assert "[[build_host]]" in rendered
    assert "base_image_volume" not in rendered
    assert "max_concurrent = 2" in rendered


def test_serialize_cost_class_emits_coeff_as_quoted_string() -> None:
    from decimal import Decimal

    rendered = serialize.serialize_inventory(
        _empty_snapshot(cost_classes=(("standard", Decimal("1.50")),))
    )
    expected_block = "\n".join(["[[cost_class]]", 'name = "standard"', 'coeff = "1.50"', ""])
    assert expected_block in rendered


def test_serialize_resources_and_build_hosts_sorted_by_name() -> None:
    # Each collection gets two rows in reverse-name order so a dropped/constant sort key
    # (insertion order) or a key=None sort (ResourceRow is unorderable → TypeError) is caught.
    snapshot = _empty_snapshot(
        remote_libvirt=(_resource_row("r-zzz"), _resource_row("r-aaa")),
        local_libvirt=(_resource_row("zzz"), _resource_row("aaa")),
        fault_inject=(_resource_row("fi-zzz"), _resource_row("fi-aaa")),
        build_hosts=(
            serialize.BuildHostRow(
                name="bh-z", kind="k", base_image_volume=None, workspace_root="/w", max_concurrent=1
            ),
            serialize.BuildHostRow(
                name="bh-a", kind="k", base_image_volume=None, workspace_root="/w", max_concurrent=1
            ),
        ),
    )
    rendered = serialize.serialize_inventory(snapshot)
    assert rendered.index('name = "r-aaa"') < rendered.index('name = "r-zzz"')
    assert rendered.index('name = "aaa"') < rendered.index('name = "zzz"')
    assert rendered.index('name = "fi-aaa"') < rendered.index('name = "fi-zzz"')
    assert rendered.index('name = "bh-a"') < rendered.index('name = "bh-z"')


def test_serialize_cost_classes_sorted_by_name_not_coeff() -> None:
    from decimal import Decimal

    # name order (alpha, zeta) is the OPPOSITE of coeff order (zeta=1 < alpha=9), so a sort
    # keyed on the coefficient instead of the name produces a different order.
    snapshot = _empty_snapshot(cost_classes=(("zeta", Decimal("1")), ("alpha", Decimal("9"))))
    rendered = serialize.serialize_inventory(snapshot)
    assert rendered.index('name = "alpha"') < rendered.index('name = "zeta"')


def test_serialize_section_order_is_fixed() -> None:
    from decimal import Decimal

    snapshot = _empty_snapshot(
        images=(_defined_image(),),
        remote_libvirt=(_resource_row("r1"),),
        local_libvirt=(_resource_row("l1"),),
        fault_inject=(_resource_row("fi1"),),
        build_hosts=(
            serialize.BuildHostRow(
                name="bh1", kind="k", base_image_volume=None, workspace_root="/w", max_concurrent=1
            ),
        ),
        cost_classes=(("cc1", Decimal("1")),),
    )
    body = _body(snapshot)
    positions = [
        body.index("[[image]]"),
        body.index("[[remote_libvirt]]"),
        body.index("[[local_libvirt]]"),
        body.index("[[fault_inject]]"),
        body.index("[[build_host]]"),
        body.index("[[cost_class]]"),
    ]
    assert positions == sorted(positions)


# ---- TOML emitter primitives ----------------------------------------------------------


def test_toml_str_escapes_special_characters() -> None:
    # A value with a quote, backslash, and newline must be escaped so it cannot break out.
    image = serialize.ImageRow(
        provider="p",
        name='a"b\\c\nd\te',
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key="k",
        digest=None,
        volume=None,
        state="built",
    )
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(image,)))
    assert r'name = "a\"b\\c\nd\te"' in rendered
    # the raw control characters never appear inside the emitted value line.
    name_line = next(line for line in rendered.splitlines() if line.startswith("name = "))
    assert "\t" not in name_line


def test_toml_str_escapes_low_control_char_as_unicode() -> None:
    image = serialize.ImageRow(
        provider="p",
        name="x\x01y",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key="k",
        digest=None,
        volume=None,
        state="built",
    )
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(image,)))
    assert 'name = "x\\u0001y"' in rendered
    assert "\x01" not in rendered


def test_toml_str_does_not_escape_space() -> None:
    # 0x20 (space) is the boundary: it must NOT be escaped (the `< 0x20` guard, not `<=`).
    image = serialize.ImageRow(
        provider="p",
        name="a b",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key="k",
        digest=None,
        volume=None,
        state="built",
    )
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(image,)))
    assert 'name = "a b"' in rendered
    assert "\\u0020" not in rendered


def test_toml_array_escapes_each_element() -> None:
    image = serialize.ImageRow(
        provider="p",
        name="img",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=['has"quote', "plain"],
        object_key="k",
        digest=None,
        volume=None,
        state="built",
    )
    rendered = serialize.serialize_inventory(_empty_snapshot(images=(image,)))
    assert r'capabilities = ["has\"quote", "plain"]' in rendered


# ---- fake adapter ---------------------------------------------------------------------


def test_fake_records_the_last_write() -> None:
    fake = writeback.FakeWriteback()
    assert fake.written is None
    _run(fake.write("hello = 1\n"))
    assert fake.written == "hello = 1\n"
    assert fake.target_kind == "fake"


def test_fake_can_be_made_to_fail_and_records_nothing() -> None:
    boom = CategorizedError("nope", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    fake = writeback.FakeWriteback(fail=boom)
    with pytest.raises(CategorizedError) as exc:
        _run(fake.write("x = 1\n"))
    assert exc.value is boom
    assert fake.written is None


# ---- mounted-file adapter -------------------------------------------------------------


def test_file_adapter_writes_atomically(tmp_path: Path) -> None:
    target = tmp_path / "systems.toml"
    adapter = writeback.MountedFileWriteback(target)
    assert adapter.target_kind == "file"
    _run(adapter.write('schema_version = 2\nname = "host-a"\n'))
    assert target.read_text() == 'schema_version = 2\nname = "host-a"\n'
    # no temp files left behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["systems.toml"]


def test_file_adapter_creates_temp_in_target_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Atomic replace requires the temp file to live on the same filesystem as the target,
    # which the adapter guarantees by passing dir=<target parent> to mkstemp. Pin that
    # contract directly so a `dir=None` mutant (temp in the system temp dir) is killed even
    # when tmp_path and the system temp share a filesystem.
    target = tmp_path / "systems.toml"
    captured: dict[str, object] = {}
    real_mkstemp = writeback.tempfile.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        captured["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(writeback.tempfile, "mkstemp", spy_mkstemp)
    _run(writeback.MountedFileWriteback(target).write("x = 1\n"))
    assert captured["dir"] == target.parent


def test_file_adapter_temp_name_is_hidden_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The temp file must be a dotfile (".systems-") with a ".tmp" suffix so an interrupted write
    # leaves an obviously-transient, hidden artifact rather than a plausible config file. Pin the
    # prefix/suffix passed to mkstemp so a dropped or re-cased affix is caught.
    target = tmp_path / "systems.toml"
    captured: dict[str, object] = {}
    real_mkstemp = writeback.tempfile.mkstemp

    def spy_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        captured["prefix"] = kwargs.get("prefix")
        captured["suffix"] = kwargs.get("suffix")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(writeback.tempfile, "mkstemp", spy_mkstemp)
    _run(writeback.MountedFileWriteback(target).write("x = 1\n"))
    assert captured["prefix"] == ".systems-"
    assert captured["suffix"] == ".tmp"


def test_file_adapter_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "systems.toml"
    target.write_text("old = 1\n")
    _run(writeback.MountedFileWriteback(target).write("new = 2\n"))
    assert target.read_text() == "new = 2\n"


def test_file_adapter_missing_parent_is_configuration_error(tmp_path: Path) -> None:
    target = tmp_path / "absent-dir" / "systems.toml"
    with pytest.raises(CategorizedError) as exc:
        _run(writeback.MountedFileWriteback(target).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(target) in str(exc.value)
    assert exc.value.details == {"path": str(target)}
    assert not target.exists()


# ---- ConfigMap adapter ----------------------------------------------------------------


def _configmap_adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> writeback.ConfigMapWriteback:
    transport = httpx.MockTransport(handler)
    return writeback.ConfigMapWriteback(
        namespace="kdive",
        name="kdive-systems",
        key="systems.toml",
        token="tok-abc",  # pragma: allowlist secret
        api_base="https://10.0.0.1:443",
        transport=transport,
    )


def test_configmap_patch_issues_the_expected_request() -> None:
    import json

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["called"] = True
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["accept"] = request.headers.get("accept")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"kind": "ConfigMap"})

    _run(_configmap_adapter(handler).write("schema_version = 2\n"))
    # the mock transport must actually be used (not bypassed for a real-network client).
    assert captured.get("called") is True
    assert captured["method"] == "PATCH"
    # httpx canonicalizes the default :443 out of the URL; assert the path, not the port form.
    assert str(captured["url"]).endswith("/api/v1/namespaces/kdive/configmaps/kdive-systems")
    assert str(captured["url"]).startswith("https://10.0.0.1")
    assert captured["auth"] == "Bearer tok-abc"
    assert captured["content_type"] == "application/strategic-merge-patch+json"
    assert captured["accept"] == "application/json"
    # the body is a strategic-merge patch on data.<key>, carrying the exact document.
    payload = json.loads(str(captured["body"]))
    assert payload == {"data": {"systems.toml": "schema_version = 2\n"}}


def test_configmap_sends_canonical_header_names_verbatim() -> None:
    # httpx normalizes header *lookups*, so request.headers.get(...) hides the case the code
    # actually emits. Assert the raw wire names so a re-cased header literal is caught.
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["names"] = [name.decode() for name, _ in request.headers.raw]
        return httpx.Response(200)

    _run(_configmap_adapter(handler).write("x = 1\n"))
    names = captured["names"]
    assert "Authorization" in names
    assert "Content-Type" in names
    assert "Accept" in names


def test_configmap_stores_the_injected_transport() -> None:
    # The injected transport must be retained on the instance; dropping it (storing None) would
    # make `write` build a real-network client instead of routing through the mock.
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    adapter = writeback.ConfigMapWriteback(
        namespace="kdive",
        name="kdive-systems",
        key="systems.toml",
        token="tok",  # noqa: S106
        api_base="https://10.0.0.1:443",
        transport=transport,
    )
    assert adapter._transport is transport


def test_configmap_client_routes_through_the_injected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_client` must build its client with the injected transport. If the transport branch is
    # inverted or the transport argument is dropped, httpx is asked to build a real-network
    # client (transport=None); fail loudly on that so the mutant cannot survive.
    real_init = httpx.AsyncClient.__init__

    def guarded_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        if kwargs.get("transport") is None:
            raise AssertionError("client built without the injected transport (real network path)")
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", guarded_init)

    captured: dict[str, bool] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["called"] = True
        return httpx.Response(200)

    _run(_configmap_adapter(handler).write("x = 1\n"))
    assert captured.get("called") is True


def test_configmap_defaults_to_tls_verification() -> None:
    # The constructor default for `verify` must keep TLS verification on; flipping it to False
    # would silently disable certificate checking for the API patch.
    adapter = writeback.ConfigMapWriteback(
        namespace="kdive",
        name="kdive-systems",
        key="systems.toml",
        token="tok",  # noqa: S106
        api_base="https://10.0.0.1:443",
    )
    assert adapter._verify is True


def test_configmap_strips_trailing_slash_from_api_base() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200)

    adapter = writeback.ConfigMapWriteback(
        namespace="kdive",
        name="kdive-systems",
        key="systems.toml",
        token="tok",  # noqa: S106
        api_base="https://10.0.0.1:443/",
        transport=httpx.MockTransport(handler),
    )
    _run(adapter.write("x = 1\n"))
    # a trailing slash on api_base must not produce a double slash before /api.
    assert "//api/v1" not in str(captured["url"]).replace("https://", "")
    assert str(captured["url"]).endswith("/api/v1/namespaces/kdive/configmaps/kdive-systems")


def test_configmap_401_maps_to_configuration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"configmap": "kdive-systems", "status": 401}


def test_configmap_403_maps_to_configuration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden: needs RBAC"})

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "kdive-systems" in str(exc.value)
    assert exc.value.details == {"configmap": "kdive-systems", "status": 403}


def test_configmap_300_is_not_treated_as_success() -> None:
    # the success window is 2xx only; a 3xx redirect must surface as a failure, not silently pass.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(300, json={"message": "multiple choices"})

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"target": "configmap", "status": 300}


@pytest.mark.parametrize("status", [201, 204, 299])
def test_configmap_2xx_other_than_200_succeeds(status: int) -> None:
    # Every code in the interior/upper part of the 2xx window is success, not just 200.
    # 201/204 exercise the interior (kills `status == 200`, `< 201`); 299 pins the upper
    # bound just below 300 (kills `< 300` -> `<= 200`/`< 201`).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"kind": "ConfigMap"})

    _run(_configmap_adapter(handler).write("x = 1\n"))


def test_configmap_500_is_infrastructure_failure_without_body() -> None:
    leaky_body = "internal-cluster-detail-should-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=leaky_body)

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "500" in str(exc.value)
    # the response body (which can echo cluster internals) is never surfaced.
    assert leaky_body not in str(exc.value)
    assert leaky_body not in repr(exc.value.details)
    assert exc.value.details == {"target": "configmap", "status": 500}


def test_configmap_transport_error_is_infrastructure_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CategorizedError) as exc:
        _run(_configmap_adapter(handler).write("x = 1\n"))
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    # the token is never echoed in an error.
    assert "tok-abc" not in str(exc.value)
    assert "tok-abc" not in repr(exc.value.details)
    # the details name the failing target and the exception type (not its message).
    assert exc.value.details == {"target": "configmap", "error": "ConnectError"}
    assert "ConnectError" in str(exc.value)
    assert "connection refused" not in str(exc.value)


def test_configmap_from_in_cluster_without_mount_is_configuration_error(tmp_path: Path) -> None:
    # Point the service-account dir at an empty tmp dir → no token/namespace → fail closed.
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="kdive-systems", key="systems.toml", service_account_dir=tmp_path
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# ---- factory --------------------------------------------------------------------------


def _load_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in ("KDIVE_INVENTORY_WRITEBACK", "KDIVE_INVENTORY_WRITEBACK_CONFIGMAP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.load(dict(os.environ))


def test_factory_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_env(monkeypatch)
    assert writeback.resolve_writeback_target() is None
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="off")
    assert writeback.resolve_writeback_target() is None


def test_factory_file_returns_file_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "systems.toml"))
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="file")
    target = writeback.resolve_writeback_target()
    assert isinstance(target, writeback.MountedFileWriteback)
    assert target.target_kind == "file"


def test_factory_unknown_value_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="bogus")
    with pytest.raises(CategorizedError) as exc:
        writeback.resolve_writeback_target()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "bogus" in str(exc.value)


def test_factory_configmap_outside_a_pod_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # configmap selected but no service-account mount → fail closed at resolve time.
    monkeypatch.setattr(writeback, "_SERVICE_ACCOUNT_DIR", tmp_path)
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="configmap")
    with pytest.raises(CategorizedError) as exc:
        writeback.resolve_writeback_target()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # the configmap branch was taken: the message is the in-cluster failure, NOT the
    # "unknown value" error a mis-typed selector would raise.
    assert "running in a Kubernetes pod" in str(exc.value)
    assert "unknown" not in str(exc.value).lower()


def test_factory_empty_value_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # an explicitly empty (whitespace) selector is treated as off, not an unknown value.
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="   ")
    assert writeback.resolve_writeback_target() is None


def test_factory_value_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "systems.toml"))
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="FILE")
    target = writeback.resolve_writeback_target()
    assert isinstance(target, writeback.MountedFileWriteback)


def test_factory_file_adapter_targets_the_configured_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # the selected file adapter must write to KDIVE_SYSTEMS_TOML, not some other path.
    target_path = tmp_path / "nested" / "systems.toml"
    target_path.parent.mkdir()
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(target_path))
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="file")
    adapter = writeback.resolve_writeback_target()
    assert isinstance(adapter, writeback.MountedFileWriteback)
    _run(adapter.write("x = 1\n"))
    assert target_path.read_text() == "x = 1\n"


def test_factory_unknown_value_carries_accepted_values_in_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="bogus")
    with pytest.raises(CategorizedError) as exc:
        writeback.resolve_writeback_target()
    assert exc.value.details == {
        "variable": "KDIVE_INVENTORY_WRITEBACK",
        "accepted_values": ["off", "configmap", "file"],
    }


def test_factory_configmap_in_a_pod_uses_default_and_overridden_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_sa_mount(tmp_path, namespace="ns-x")
    monkeypatch.setattr(writeback, "_SERVICE_ACCOUNT_DIR", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.9")

    _load_env(monkeypatch, KDIVE_INVENTORY_WRITEBACK="configmap")
    default_target = writeback.resolve_writeback_target()
    assert isinstance(default_target, writeback.ConfigMapWriteback)
    # default ConfigMap name, the file-name key, and the namespace from the mount.
    assert default_target._name == "kdive-systems"
    assert default_target._key == "systems.toml"
    assert default_target._namespace == "ns-x"

    _load_env(
        monkeypatch,
        KDIVE_INVENTORY_WRITEBACK="configmap",
        KDIVE_INVENTORY_WRITEBACK_CONFIGMAP="custom-cm",
    )
    custom_target = writeback.resolve_writeback_target()
    assert isinstance(custom_target, writeback.ConfigMapWriteback)
    assert custom_target._name == "custom-cm"


# ---- ConfigMapWriteback.from_in_cluster -----------------------------------------------


def _write_sa_mount(sa_dir: Path, *, token: str = "tok", namespace: str = "kdive") -> None:
    sa_dir.mkdir(parents=True, exist_ok=True)
    (sa_dir / "token").write_text(token)
    (sa_dir / "namespace").write_text(namespace)
    (sa_dir / "ca.crt").write_text("---ca---")


def test_from_in_cluster_builds_adapter_from_mount_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_sa_mount(tmp_path, token="tok-xyz", namespace="ns-1")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.1.2.3")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    adapter = writeback.ConfigMapWriteback.from_in_cluster(
        name="my-cm", key="systems.toml", service_account_dir=tmp_path
    )
    assert adapter.target_kind == "configmap"
    assert adapter._namespace == "ns-1"
    assert adapter._name == "my-cm"
    assert adapter._key == "systems.toml"
    assert adapter._token == "tok-xyz"  # noqa: S105
    assert adapter._api_base == "https://10.1.2.3:6443"
    assert adapter._verify == str(tmp_path / "ca.crt")


def test_from_in_cluster_defaults_port_to_443(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_sa_mount(tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    adapter = writeback.ConfigMapWriteback.from_in_cluster(
        name="cm", key="k", service_account_dir=tmp_path
    )
    assert adapter._api_base == "https://10.0.0.1:443"


def test_from_in_cluster_missing_token_names_the_token(tmp_path: Path) -> None:
    (tmp_path / "namespace").write_text("kdive")
    (tmp_path / "ca.crt").write_text("ca")
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="cm", key="k", service_account_dir=tmp_path
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "but the service-account token is not" in str(exc.value)
    assert exc.value.details == {"variable": "KDIVE_INVENTORY_WRITEBACK"}


def test_from_in_cluster_empty_token_is_treated_as_missing(tmp_path: Path) -> None:
    # a present-but-blank token file must fail closed, naming the token.
    _write_sa_mount(tmp_path, token="   ")
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="cm", key="k", service_account_dir=tmp_path
        )
    assert "but the service-account token is not" in str(exc.value)


def test_from_in_cluster_missing_namespace_names_the_namespace(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("tok")
    (tmp_path / "ca.crt").write_text("ca")
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="cm", key="k", service_account_dir=tmp_path
        )
    assert "but the pod namespace is not" in str(exc.value)


def test_from_in_cluster_missing_ca_names_the_ca(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("tok")
    (tmp_path / "namespace").write_text("kdive")
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="cm", key="k", service_account_dir=tmp_path
        )
    assert "but the service-account CA (ca.crt) is not" in str(exc.value)


def test_from_in_cluster_missing_service_host_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_sa_mount(tmp_path)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(CategorizedError) as exc:
        writeback.ConfigMapWriteback.from_in_cluster(
            name="cm", key="k", service_account_dir=tmp_path
        )
    assert "but the KUBERNETES_SERVICE_HOST is not" in str(exc.value)
