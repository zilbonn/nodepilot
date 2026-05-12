from __future__ import annotations

import pytest
from httpx import Response

from nodepilot.client import ProxmoxClient
from nodepilot.config import Settings
from nodepilot.operations import ProxmoxOperations, normalize_lxc_config_for_token


def settings() -> Settings:
    return Settings(
        api_url="https://pve.example.test:8006/api2/json",
        user="nodepilot@pve",
        token_name="mcp",
        token_value="secret",
        default_node="pve-node-1",
        verify_ssl=False,
        timeout=5,
    )


def mock_task(respx_mock, task: str) -> None:
    encoded = task.replace(":", "%3A").replace("@", "%40")
    node = task.split(":")[1]
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/{node}/tasks/{encoded}/status"
    ).mock(return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}}))


@pytest.mark.asyncio
async def test_qemu_power_action_uses_status_endpoint(respx_mock) -> None:
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/status/reboot"
    ).mock(return_value=Response(200, json={"data": None}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        assert await ops.power_action("qemu", 100, "reboot") is None


@pytest.mark.asyncio
async def test_qemu_create_fetches_next_id_when_missing(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/cluster/nextid").mock(
        return_value=Response(200, json={"data": "128"})
    )
    route = respx_mock.post("https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu").mock(
        return_value=Response(200, json={"data": None})
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        await ops.qemu_create({"name": "mcp-test", "memory": 2048}, wait_for_task=False)

    assert "vmid=128" in route.calls.last.request.content.decode()


@pytest.mark.asyncio
async def test_snapshot_create_delete_rollback_paths(respx_mock) -> None:
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/snapshot"
    ).mock(
        return_value=Response(200, json={"data": None})
    )
    respx_mock.delete(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/snapshot/before-change"
    ).mock(return_value=Response(200, json={"data": None}))
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/snapshot/before-change/rollback"
    ).mock(return_value=Response(200, json={"data": None}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        assert await ops.create_snapshot("qemu", 100, "before-change") is None
        assert await ops.delete_snapshot("qemu", 100, "before-change") is None
        assert await ops.rollback_snapshot("qemu", 100, "before-change") is None


@pytest.mark.asyncio
async def test_lxc_delete_passes_purge_flags(respx_mock) -> None:
    route = respx_mock.delete("https://pve.example.test:8006/api2/json/nodes/pve-node-1/lxc/128").mock(
        return_value=Response(200, json={"data": None})
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        assert await ops.lxc_delete(128) is None

    assert route.calls.last.request.url.params["purge"] == "1"
    assert route.calls.last.request.url.params["destroy-unreferenced-disks"] == "1"


@pytest.mark.asyncio
async def test_raw_api_request(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/cluster/resources").mock(
        return_value=Response(200, json={"data": [{"type": "node", "node": "pve-node-1"}]})
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.api_request("GET", "/cluster/resources")

    assert result == [{"type": "node", "node": "pve-node-1"}]


@pytest.mark.asyncio
async def test_storage_content_filter_for_iso(respx_mock) -> None:
    route = respx_mock.get(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/storage/local/content"
    ).mock(return_value=Response(200, json={"data": [{"volid": "local:iso/lubuntu.iso"}]}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.storage_content("local", content="iso")

    assert result == [{"volid": "local:iso/lubuntu.iso"}]
    assert route.calls.last.request.url.params["content"] == "iso"


@pytest.mark.asyncio
async def test_ensure_storage_content_adds_import_when_missing(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "storage": "local",
                    "content": "backup,iso,vztmpl",
                    "digest": "abc123",
                }
            },
        )
    )
    route = respx_mock.put("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(200, json={"data": None})
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.ensure_storage_content("local", "import")

    body = route.calls.last.request.content.decode()
    assert result["changed"] is True
    assert result["new_content"] == ["backup", "iso", "vztmpl", "import"]
    assert "content=backup%2Ciso%2Cvztmpl%2Cimport" in body
    assert "digest=abc123" in body


@pytest.mark.asyncio
async def test_ensure_storage_content_skips_when_present(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(200, json={"data": {"content": "backup,iso,vztmpl,import"}})
    )
    route = respx_mock.put("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(200, json={"data": None})
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.ensure_storage_content("local", "import")

    assert result["changed"] is False
    assert not route.called


@pytest.mark.asyncio
async def test_download_url_to_storage_for_ct_template(respx_mock) -> None:
    route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/storage/local/download-url"
    ).mock(
        return_value=Response(
            200,
            json={"data": "UPID:pve-node-1:1:2:3:download:nodepilot@pve:"},
        )
    )
    encoded = "UPID%3Apve-node-1%3A1%3A2%3A3%3Adownload%3Anodepilot%40pve%3A"
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/pve-node-1/tasks/{encoded}/status"
    ).mock(return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.download_url_to_storage(
            "https://example.test/kali-rootfs.tar.xz",
            "kali-rootfs.tar.xz",
            "vztmpl",
            checksum="abc123",
            checksum_algorithm="sha256",
            compression="xz",
        )

    body = route.calls.last.request.content.decode()
    assert result["status"]["exitstatus"] == "OK"
    assert "url=https%3A%2F%2Fexample.test%2Fkali-rootfs.tar.xz" in body
    assert "filename=kali-rootfs.tar.xz" in body
    assert "content=vztmpl" in body
    assert "checksum=abc123" in body
    assert "checksum-algorithm=sha256" in body
    assert "compression=xz" in body


@pytest.mark.asyncio
async def test_download_url_to_storage_for_cloud_image_import(respx_mock) -> None:
    route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/storage/local/download-url"
    ).mock(
        return_value=Response(
            200,
            json={"data": "UPID:pve-node-1:1:2:3:download:nodepilot@pve:"},
        )
    )
    encoded = "UPID%3Apve-node-1%3A1%3A2%3A3%3Adownload%3Anodepilot%40pve%3A"
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/pve-node-1/tasks/{encoded}/status"
    ).mock(return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.download_url_to_storage(
            "https://example.test/noble-server-cloudimg-amd64.img",
            "noble-server-cloudimg-amd64.img",
            "import",
        )

    body = route.calls.last.request.content.decode()
    assert result["status"]["exitstatus"] == "OK"
    assert "content=import" in body
    assert "filename=noble-server-cloudimg-amd64.img" in body


def test_lxc_feature_normalization_keeps_nesting_and_warns_for_keyctl() -> None:
    result = normalize_lxc_config_for_token({"features": "nesting=1,keyctl=1", "hostname": "kali"})

    assert result["config"] == {"features": "nesting=1", "hostname": "kali"}
    assert result["changed"] is True
    assert result["skipped_features"] == {"keyctl": "1"}
    assert result["warnings"][0]["code"] == "lxc_feature_skipped_for_api_token"


@pytest.mark.asyncio
async def test_create_cloudinit_vm_clones_configures_starts_and_returns_ip(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/cluster/nextid").mock(
        return_value=Response(200, json={"data": "140"})
    )
    clone_task = "UPID:pve-node-1:1:2:3:qmclone:140:nodepilot@pve:"
    clone_route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/9000/clone"
    ).mock(return_value=Response(200, json={"data": clone_task}))
    mock_task(respx_mock, clone_task)
    config_route = respx_mock.put(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/140/config"
    ).mock(return_value=Response(200, json={"data": None}))
    start_task = "UPID:pve-node-1:1:2:4:qmstart:140:nodepilot@pve:"
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/140/status/start"
    ).mock(return_value=Response(200, json={"data": start_task}))
    mock_task(respx_mock, start_task)
    respx_mock.get(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/140/agent/network-get-interfaces"
    ).mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "name": "eth0",
                        "ip-addresses": [
                            {"ip-address-type": "ipv4", "ip-address": "10.0.0.55"}
                        ],
                    }
                ]
            },
        )
    )

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.create_cloudinit_vm(
            template_vmid=9000,
            name="ubuntu-dev",
            user="demo",
            ssh_public_key="ssh-ed25519 AAA test",
            memory=2048,
            cores=2,
            poll_interval=0,
        )

    clone_body = clone_route.calls.last.request.content.decode()
    config_body = config_route.calls.last.request.content.decode()
    assert "newid=140" in clone_body
    assert "name=ubuntu-dev" in clone_body
    assert "full=1" in clone_body
    assert "ciuser=demo" in config_body
    assert "sshkeys=ssh-ed25519+AAA+test" in config_body
    assert "ipconfig0=ip%3Ddhcp" in config_body
    assert "agent=enabled%3D1" in config_body
    assert result["ip"] == "10.0.0.55"
    assert result["ssh"]["command"] == "ssh demo@10.0.0.55"


@pytest.mark.asyncio
async def test_create_cloudinit_vm_installs_packages_through_guest_agent(respx_mock) -> None:
    clone_task = "UPID:pve-node-1:1:2:3:qmclone:141:nodepilot@pve:"
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/9000/clone"
    ).mock(return_value=Response(200, json={"data": clone_task}))
    mock_task(respx_mock, clone_task)
    respx_mock.put("https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/141/config").mock(
        return_value=Response(200, json={"data": None})
    )
    start_task = "UPID:pve-node-1:1:2:4:qmstart:141:nodepilot@pve:"
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/141/status/start"
    ).mock(return_value=Response(200, json={"data": start_task}))
    mock_task(respx_mock, start_task)
    respx_mock.get(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/141/agent/network-get-interfaces"
    ).mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "name": "eth0",
                        "ip-addresses": [
                            {"ip-address-type": "ipv4", "ip-address": "10.0.0.56"}
                        ],
                    }
                ]
            },
        )
    )
    exec_route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/141/agent/exec"
    ).mock(return_value=Response(200, json={"data": {"pid": 7}}))
    respx_mock.get(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/141/agent/exec-status"
    ).mock(return_value=Response(200, json={"data": {"exited": True, "exitcode": 0}}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.create_cloudinit_vm(
            template_vmid=9000,
            name="ubuntu-dev",
            vmid=141,
            packages=["openssh-server", "qemu-guest-agent"],
            poll_interval=0,
        )

    body = exec_route.calls.last.request.content.decode()
    assert body.startswith("command=%2Fbin%2Fsh&command=-lc&command=")
    assert "openssh-server" in body
    assert "qemu-guest-agent" in body
    assert result["guest_initialization"][0]["name"] == "install_packages"


@pytest.mark.asyncio
async def test_create_cloudinit_vm_resizes_boot_disk(respx_mock) -> None:
    clone_task = "UPID:pve-node-1:1:2:3:qmclone:142:nodepilot@pve:"
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/9000/clone"
    ).mock(return_value=Response(200, json={"data": clone_task}))
    mock_task(respx_mock, clone_task)
    respx_mock.put("https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/142/config").mock(
        return_value=Response(200, json={"data": None})
    )
    respx_mock.get("https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/142/config").mock(
        return_value=Response(
            200,
            json={"data": {"bootdisk": "scsi0", "scsi0": "local-lvm:vm-142-disk-0"}},
        )
    )
    resize_task = "UPID:pve-node-1:1:2:4:qmresize:142:nodepilot@pve:"
    resize_route = respx_mock.put(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/142/resize"
    ).mock(return_value=Response(200, json={"data": resize_task}))
    mock_task(respx_mock, resize_task)

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        await ops.create_cloudinit_vm(
            template_vmid=9000,
            name="ubuntu-dev",
            vmid=142,
            start=False,
            wait_for_ip=False,
            disk_gb=40,
        )

    body = resize_route.calls.last.request.content.decode()
    assert "disk=scsi0" in body
    assert "size=40G" in body


@pytest.mark.asyncio
async def test_create_cloudinit_vm_from_image_enables_import_and_uses_import_from(
    respx_mock,
) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(
            200,
            json={"data": {"content": "backup,iso,vztmpl", "digest": "abc123"}},
        )
    )
    storage_route = respx_mock.put("https://pve.example.test:8006/api2/json/storage/local").mock(
        return_value=Response(200, json={"data": None})
    )
    download_task = "UPID:pve-node-1:1:2:3:download:nodepilot@pve:"
    download_route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/storage/local/download-url"
    ).mock(return_value=Response(200, json={"data": download_task}))
    mock_task(respx_mock, download_task)
    create_task = "UPID:pve-node-1:1:2:4:qmcreate:150:nodepilot@pve:"
    create_route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu"
    ).mock(return_value=Response(200, json={"data": create_task}))
    mock_task(respx_mock, create_task)
    config_route = respx_mock.put(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/150/config"
    ).mock(return_value=Response(200, json={"data": None}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.create_cloudinit_vm_from_image(
            image_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
            name="ubuntu-cloud",
            vmid=150,
            user="demo",
            ssh_public_key="ssh-ed25519 AAA test",
            memory=4096,
            cores=2,
            start=False,
            wait_for_ip=False,
        )

    storage_body = storage_route.calls.last.request.content.decode()
    download_body = download_route.calls.last.request.content.decode()
    create_body = create_route.calls.last.request.content.decode()
    config_body = config_route.calls.last.request.content.decode()
    assert "content=backup%2Ciso%2Cvztmpl%2Cimport" in storage_body
    assert "content=import" in download_body
    assert "filename=noble-server-cloudimg-amd64.img" in download_body
    assert "vmid=150" in create_body
    assert (
        "scsi0=local-lvm%3A0%2Cimport-from%3Dlocal%3Aimport%2F"
        "noble-server-cloudimg-amd64.img%2Cdiscard%3Don"
    ) in create_body
    assert "ide2=local-lvm%3Acloudinit" in create_body
    assert "boot=order%3Dscsi0" in create_body
    assert "net0=virtio%2Cbridge%3Dvmbr0" in create_body
    assert "ciuser=demo" in config_body
    assert result["image_volid"] == "local:import/noble-server-cloudimg-amd64.img"


def test_lxc_feature_normalization_removes_unsupported_only_features() -> None:
    result = normalize_lxc_config_for_token({"features": "keyctl=1,mknod=1", "hostname": "kali"})

    assert result["config"] == {"hostname": "kali"}
    assert result["changed"] is True
    assert result["skipped_features"] == {"keyctl": "1", "mknod": "1"}


def test_lxc_feature_normalization_keeps_config_without_features() -> None:
    config = {"hostname": "kali", "memory": 2048}

    result = normalize_lxc_config_for_token(config)

    assert result["config"] == config
    assert result["changed"] is False
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_lxc_create_sends_sanitized_features_and_returns_warning(respx_mock) -> None:
    route = respx_mock.post("https://pve.example.test:8006/api2/json/nodes/pve-node-1/lxc").mock(
        return_value=Response(
            200,
            json={"data": "UPID:pve-node-1:1:2:3:vzcreate:128:nodepilot@pve:"},
        )
    )
    encoded = "UPID%3Apve-node-1%3A1%3A2%3A3%3Avzcreate%3A128%3Anodepilot%40pve%3A"
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/pve-node-1/tasks/{encoded}/status"
    ).mock(return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}}))

    async with ProxmoxClient(settings()) as client:
        ops = ProxmoxOperations(client, "pve-node-1")
        result = await ops.lxc_create(
            {
                "vmid": 128,
                "ostemplate": "local:vztmpl/kali.tar.xz",
                "hostname": "kali",
                "features": "nesting=1,keyctl=1",
            },
        )

    body = route.calls.last.request.content.decode()
    assert "features=nesting%3D1" in body
    assert "keyctl" not in body
    assert result["result"]["status"]["exitstatus"] == "OK"
    assert result["skipped_features"] == {"keyctl": "1"}
    assert result["warnings"][0]["code"] == "lxc_feature_skipped_for_api_token"
