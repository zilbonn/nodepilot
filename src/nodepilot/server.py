"""FastMCP server exposing Proxmox VE controls."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from nodepilot.client import ProxmoxClient
from nodepilot.config import Settings, load_settings
from nodepilot.operations import DownloadContent, GuestType, PowerAction, ProxmoxOperations


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


@asynccontextmanager
async def operations_context(settings: Settings | None = None) -> AsyncIterator[ProxmoxOperations]:
    runtime_settings = settings or load_settings()
    async with ProxmoxClient(runtime_settings) as client:
        yield ProxmoxOperations(client, runtime_settings.default_node)


def create_mcp(settings: Settings | None = None) -> FastMCP:
    runtime_settings = settings or load_settings()
    mcp = FastMCP(
        "NodePilot",
        instructions=(
            "Control Proxmox VE through its HTTPS REST API. "
            "This server exposes direct admin operations; destructive tools execute immediately."
        ),
        host=os.getenv("MCP_HTTP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_HTTP_PORT", "8000")),
        json_response=True,
    )

    @mcp.resource("proxmox://version")
    async def proxmox_version_resource() -> str:
        """Current Proxmox VE version."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.version())

    @mcp.resource("proxmox://cluster/resources")
    async def proxmox_cluster_resources_resource() -> str:
        """Current cluster resource inventory."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.cluster_resources())

    @mcp.resource("proxmox://nodes")
    async def proxmox_nodes_resource() -> str:
        """Current Proxmox nodes."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.nodes())

    @mcp.resource("proxmox://nodes/{node}/storage")
    async def proxmox_node_storage_resource(node: str) -> str:
        """Storage configured on a node."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.storage(node))

    @mcp.resource("proxmox://nodes/{node}/qemu/{vmid}/config")
    async def proxmox_qemu_config_resource(node: str, vmid: str) -> str:
        """Configuration for a QEMU VM."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.guest_config("qemu", int(vmid), node))

    @mcp.resource("proxmox://nodes/{node}/lxc/{vmid}/config")
    async def proxmox_lxc_config_resource(node: str, vmid: str) -> str:
        """Configuration for an LXC container."""

        async with operations_context(runtime_settings) as ops:
            return _json(await ops.guest_config("lxc", int(vmid), node))

    @mcp.tool(description="Call any Proxmox API endpoint under /api2/json.")
    async def proxmox_api_request(
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        params: dict[str, Any] | None = None,
        wait_for_task: bool = True,
        task_timeout: float = 300.0,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.api_request(method, path, params, wait_for_task, task_timeout)

    @mcp.tool(description="List Proxmox nodes.")
    async def proxmox_list_nodes() -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.nodes()

    @mcp.tool(description="Get Proxmox cluster status.")
    async def proxmox_cluster_status() -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.cluster_status()

    @mcp.tool(description="List cluster resources, optionally filtered by type and node.")
    async def proxmox_list_resources(
        resource_type: str | None = None,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.cluster_resources(resource_type, node)

    @mcp.tool(description="List QEMU VMs and LXC containers, optionally filtered.")
    async def proxmox_list_guests(
        guest_type: GuestType | None = None,
        status: str | None = None,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.guests(guest_type, status, node)

    @mcp.tool(description="Get the next available VM/container ID.")
    async def proxmox_next_id() -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.next_id()

    @mcp.tool(description="Get node status.")
    async def proxmox_node_status(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.node_status(node)

    @mcp.tool(description="List storage on a node.")
    async def proxmox_list_storage(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage(node)

    @mcp.tool(description="Get cluster-level storage configuration.")
    async def proxmox_get_storage_config(storage: str) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage_config(storage)

    @mcp.tool(
        description=(
            "Ensure a cluster storage allows a content type such as import. "
            "This modifies Proxmox storage configuration."
        )
    )
    async def proxmox_ensure_storage_content(storage: str, content: str) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.ensure_storage_content(storage, content)

    @mcp.tool(description="List content in a Proxmox storage.")
    async def proxmox_list_storage_content(
        storage: str,
        node: str | None = None,
        content: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage_content(storage, node, content)

    @mcp.tool(description="List ISO images available in storage, usually for VM CD/DVD attachment.")
    async def proxmox_list_isos(
        storage: str = "local",
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage_content(storage, node, "iso")

    @mcp.tool(description="List backup archives in storage.")
    async def proxmox_list_backups(
        storage: str = "local",
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage_content(storage, node, "backup")

    @mcp.tool(description="List downloaded LXC template files in storage.")
    async def proxmox_list_storage_templates(
        storage: str = "local",
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.storage_content(storage, node, "vztmpl")

    @mcp.tool(description="Delete storage content such as ISO, template, backup, or unused disk.")
    async def proxmox_delete_storage_content(
        storage: str,
        volume: str,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.delete_storage_content(storage, volume, node)

    @mcp.tool(
        description=(
            "Download an ISO or LXC container template from an external HTTP(S) URL into "
            "Proxmox storage."
        )
    )
    async def proxmox_download_url_to_storage(
        url: str,
        filename: str,
        content: DownloadContent,
        storage: str = "local",
        node: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
        | None = None,
        compression: str | None = None,
        verify_certificates: bool = True,
        task_timeout: float = 900.0,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.download_url_to_storage(
                url,
                filename,
                content,
                storage,
                node,
                checksum,
                checksum_algorithm,
                compression,
                verify_certificates,
                task_timeout,
            )

    @mcp.tool(
        description="Download an external HTTP(S) URL into Proxmox storage as an LXC template."
    )
    async def proxmox_download_ct_template_url(
        url: str,
        filename: str,
        storage: str = "local",
        node: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
        | None = None,
        compression: str | None = None,
        verify_certificates: bool = True,
        task_timeout: float = 900.0,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.download_url_to_storage(
                url,
                filename,
                "vztmpl",
                storage,
                node,
                checksum,
                checksum_algorithm,
                compression,
                verify_certificates,
                task_timeout,
            )

    @mcp.tool(description="Download an external HTTP(S) URL into Proxmox storage as an ISO image.")
    async def proxmox_download_iso_url(
        url: str,
        filename: str,
        storage: str = "local",
        node: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
        | None = None,
        verify_certificates: bool = True,
        task_timeout: float = 900.0,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.download_url_to_storage(
                url,
                filename,
                "iso",
                storage,
                node,
                checksum,
                checksum_algorithm,
                None,
                verify_certificates,
                task_timeout,
            )

    @mcp.tool(
        description=(
            "Download an external HTTP(S) cloud disk image into Proxmox storage as import content. "
            "The target storage must allow the import content type."
        )
    )
    async def proxmox_download_cloud_image_url(
        url: str,
        filename: str,
        storage: str = "local",
        node: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
        | None = None,
        verify_certificates: bool = True,
        task_timeout: float = 900.0,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.download_url_to_storage(
                url,
                filename,
                "import",
                storage,
                node,
                checksum,
                checksum_algorithm,
                None,
                verify_certificates,
                task_timeout,
            )

    @mcp.tool(description="Get VM or container current status.")
    async def proxmox_guest_status(
        guest_type: GuestType,
        vmid: int,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.guest_status(guest_type, vmid, node)

    @mcp.tool(description="Get VM or container configuration.")
    async def proxmox_get_guest_config(
        guest_type: GuestType,
        vmid: int,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.guest_config(guest_type, vmid, node)

    @mcp.tool(description="Update VM or container configuration with Proxmox API parameters.")
    async def proxmox_set_guest_config(
        guest_type: GuestType,
        vmid: int,
        config: dict[str, Any],
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.update_guest_config(guest_type, vmid, config, node)

    @mcp.tool(description="Create a QEMU VM using Proxmox /nodes/{node}/qemu parameters.")
    async def proxmox_create_vm(
        config: dict[str, Any],
        node: str | None = None,
        wait_for_task: bool = True,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.qemu_create(config, node, wait_for_task)

    @mcp.tool(
        description=(
            "Clone an existing cloud-init QEMU template, apply user/SSH/network/CPU/RAM/disk "
            "settings, optionally start it, wait for a guest-agent IP, and run package or shell "
            "initialization commands through QEMU guest agent."
        )
    )
    async def proxmox_create_cloudinit_vm(
        template_vmid: int,
        name: str,
        vmid: int | None = None,
        node: str | None = None,
        storage: str | None = None,
        full: bool = True,
        start: bool = True,
        wait_for_ip: bool = True,
        ip_timeout: float = 300.0,
        poll_interval: float = 5.0,
        user: str | None = None,
        password: str | None = None,
        ssh_public_key: str | None = None,
        ipconfig0: str | None = "ip=dhcp",
        nameserver: str | None = None,
        searchdomain: str | None = None,
        memory: int | None = None,
        cores: int | None = None,
        disk_gb: int | None = None,
        disk: str | None = None,
        packages: list[str] | None = None,
        commands: list[str] | None = None,
        extra_config: dict[str, Any] | None = None,
        fail_on_init_error: bool = False,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.create_cloudinit_vm(
                template_vmid,
                name,
                vmid,
                node,
                storage,
                full,
                start,
                wait_for_ip,
                ip_timeout,
                poll_interval,
                user,
                password,
                ssh_public_key,
                ipconfig0,
                nameserver,
                searchdomain,
                memory,
                cores,
                disk_gb,
                disk,
                packages,
                commands,
                extra_config,
                fail_on_init_error,
            )

    @mcp.tool(
        description=(
            "Create a cloud-init QEMU VM directly from an external cloud disk image URL. "
            "The workflow can enable import content on storage, download the image as import "
            "content, create the VM with import-from, attach a cloud-init drive, optionally start "
            "it, wait for a guest-agent IP, and run package or shell initialization commands."
        )
    )
    async def proxmox_create_cloudinit_vm_from_image(
        image_url: str,
        name: str,
        vmid: int | None = None,
        node: str | None = None,
        image_filename: str | None = None,
        image_storage: str = "local",
        vm_storage: str = "local-lvm",
        enable_import_content: bool = True,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
        | None = None,
        verify_certificates: bool = True,
        task_timeout: float = 1200.0,
        disk_options: str | None = "discard=on",
        bridge: str = "vmbr0",
        start: bool = True,
        wait_for_ip: bool = True,
        ip_timeout: float = 300.0,
        poll_interval: float = 5.0,
        user: str | None = None,
        password: str | None = None,
        ssh_public_key: str | None = None,
        ipconfig0: str | None = "ip=dhcp",
        nameserver: str | None = None,
        searchdomain: str | None = None,
        memory: int | None = None,
        cores: int | None = None,
        disk_gb: int | None = None,
        packages: list[str] | None = None,
        commands: list[str] | None = None,
        extra_config: dict[str, Any] | None = None,
        fail_on_init_error: bool = False,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.create_cloudinit_vm_from_image(
                image_url,
                name,
                vmid,
                node,
                image_filename,
                image_storage,
                vm_storage,
                enable_import_content,
                checksum,
                checksum_algorithm,
                verify_certificates,
                task_timeout,
                disk_options,
                bridge,
                start,
                wait_for_ip,
                ip_timeout,
                poll_interval,
                user,
                password,
                ssh_public_key,
                ipconfig0,
                nameserver,
                searchdomain,
                memory,
                cores,
                disk_gb,
                packages,
                commands,
                extra_config,
                fail_on_init_error,
            )

    @mcp.tool(description="Clone a QEMU VM.")
    async def proxmox_clone_vm(
        vmid: int,
        newid: int,
        node: str | None = None,
        name: str | None = None,
        target_node: str | None = None,
        full: bool = True,
        storage: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.qemu_clone(vmid, newid, node, name, target_node, full, storage)

    @mcp.tool(description="Delete a QEMU VM and optionally purge config/unreferenced disks.")
    async def proxmox_delete_vm(
        vmid: int,
        node: str | None = None,
        purge: bool = True,
        destroy_unreferenced_disks: bool = True,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.qemu_delete(vmid, node, purge, destroy_unreferenced_disks)

    @mcp.tool(
        description=(
            "Run a QEMU VM power action: start, stop, shutdown, reboot, reset, suspend, resume."
        )
    )
    async def proxmox_vm_power(
        vmid: int,
        action: PowerAction,
        node: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.power_action("qemu", vmid, action, node, params)

    @mcp.tool(description="Create an LXC container using Proxmox /nodes/{node}/lxc parameters.")
    async def proxmox_create_container(
        config: dict[str, Any],
        node: str | None = None,
        wait_for_task: bool = True,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.lxc_create(config, node, wait_for_task)

    @mcp.tool(
        description=(
            "Validate and normalize an LXC container config for Proxmox API-token-safe creation."
        )
    )
    async def proxmox_validate_lxc_config(config: dict[str, Any]) -> Any:
        async with operations_context(runtime_settings) as ops:
            return ops.validate_lxc_config(config)

    @mcp.tool(description="Delete an LXC container and optionally purge config/unreferenced disks.")
    async def proxmox_delete_container(
        vmid: int,
        node: str | None = None,
        purge: bool = True,
        destroy_unreferenced_disks: bool = True,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.lxc_delete(vmid, node, purge, destroy_unreferenced_disks)

    @mcp.tool(
        description="Run an LXC power action: start, stop, shutdown, reboot, suspend, resume."
    )
    async def proxmox_container_power(
        vmid: int,
        action: PowerAction,
        node: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.power_action("lxc", vmid, action, node, params)

    @mcp.tool(description="List VM or container snapshots.")
    async def proxmox_list_snapshots(
        guest_type: GuestType,
        vmid: int,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.snapshots(guest_type, vmid, node)

    @mcp.tool(description="Create a VM or container snapshot.")
    async def proxmox_create_snapshot(
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
        description: str | None = None,
        vmstate: bool | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.create_snapshot(guest_type, vmid, snapname, node, description, vmstate)

    @mcp.tool(description="Delete a VM or container snapshot.")
    async def proxmox_delete_snapshot(
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.delete_snapshot(guest_type, vmid, snapname, node)

    @mcp.tool(description="Rollback a VM or container to a snapshot.")
    async def proxmox_rollback_snapshot(
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.rollback_snapshot(guest_type, vmid, snapname, node)

    @mcp.tool(description="Create a vzdump backup for a VM or container.")
    async def proxmox_backup_guest(
        vmid: int,
        node: str | None = None,
        storage: str = "local",
        mode: Literal["snapshot", "suspend", "stop"] = "snapshot",
        compress: str | None = "zstd",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.backup_guest(vmid, node, storage, mode, compress, extra)

    @mcp.tool(description="List available LXC appliance templates from Proxmox.")
    async def proxmox_list_templates(
        node: str | None = None,
        section: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.templates(node, section)

    @mcp.tool(description="Download an LXC template to a storage.")
    async def proxmox_download_template(
        template: str,
        node: str | None = None,
        storage: str = "local",
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.download_template(template, node, storage)

    @mcp.tool(description="List Proxmox node tasks.")
    async def proxmox_list_tasks(
        node: str | None = None,
        limit: int = 50,
        source: str | None = None,
        userfilter: str | None = None,
        typefilter: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.tasks(node, limit, source, userfilter, typefilter)

    @mcp.tool(description="Get a Proxmox task status by UPID.")
    async def proxmox_task_status(upid: str, node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.task_status(upid, node)

    @mcp.tool(description="Get a Proxmox task log by UPID.")
    async def proxmox_task_log(
        upid: str,
        node: str | None = None,
        start: int = 0,
        limit: int = 200,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.task_log(upid, node, start, limit)

    @mcp.tool(description="List system services on a Proxmox node.")
    async def proxmox_list_services(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.services(node)

    @mcp.tool(description="Get a system service state on a Proxmox node.")
    async def proxmox_service_state(service: str, node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.service_state(service, node)

    @mcp.tool(description="Run a service action on a Proxmox node: start, stop, restart, reload.")
    async def proxmox_service_action(
        service: str,
        action: Literal["start", "stop", "restart", "reload"],
        node: str | None = None,
    ) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.service_action(service, action, node)

    @mcp.tool(description="List node network interfaces.")
    async def proxmox_list_network(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.network(node)

    @mcp.tool(description="List node firewall rules.")
    async def proxmox_list_firewall_rules(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.firewall_rules(node)

    @mcp.tool(description="List pending apt package updates on a node.")
    async def proxmox_apt_updates(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.apt_updates(node)

    @mcp.tool(description="List installed package versions on a node.")
    async def proxmox_apt_versions(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.apt_versions(node)

    @mcp.tool(description="Run apt update on a node through the Proxmox API.")
    async def proxmox_run_apt_update(node: str | None = None) -> Any:
        async with operations_context(runtime_settings) as ops:
            return await ops.run_apt_update(node)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NodePilot server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="MCP transport to run. Defaults to stdio.",
    )
    args = parser.parse_args()
    create_mcp().run(transport=args.transport)


if __name__ == "__main__":
    main()
