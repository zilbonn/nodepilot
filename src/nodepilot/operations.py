"""High-level Proxmox operations exposed as MCP tools."""

from __future__ import annotations

import asyncio
import ipaddress
import shlex
import time
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from nodepilot.client import ProxmoxClient, quote_path_segment

GuestType = Literal["qemu", "lxc"]
PowerAction = Literal["start", "stop", "shutdown", "reboot", "reset", "suspend", "resume"]
DownloadContent = Literal["iso", "vztmpl", "import"]

TOKEN_SAFE_LXC_FEATURES = {"nesting"}
QEMU_DISK_PREFIXES = ("scsi", "virtio", "sata", "ide")


def _parse_lxc_features(features: Any) -> dict[str, str]:
    """Parse Proxmox's comma-separated LXC feature string into a mapping."""

    if features is None:
        return {}
    if isinstance(features, dict):
        return {str(key): str(value) for key, value in features.items() if value is not None}
    if not isinstance(features, str):
        raise ValueError("LXC features must be a comma-separated string or object")

    parsed: dict[str, str] = {}
    for item in features.split(","):
        item = item.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        parsed[key.strip()] = value.strip() if separator else "1"
    return parsed


def _format_lxc_features(features: dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(features.items()))


def normalize_lxc_config_for_token(config: dict[str, Any]) -> dict[str, Any]:
    """Return a token-safe LXC config plus warnings for skipped feature flags."""

    payload = dict(config)
    warnings: list[dict[str, str]] = []

    if "features" not in payload or payload["features"] in (None, ""):
        return {
            "config": payload,
            "warnings": warnings,
            "changed": False,
            "skipped_features": {},
        }

    requested_features = _parse_lxc_features(payload["features"])
    safe_features: dict[str, str] = {}
    skipped_features: dict[str, str] = {}

    for feature, value in requested_features.items():
        enabled = value not in {"0", "false", "False", "no", "No"}
        if feature in TOKEN_SAFE_LXC_FEATURES or not enabled:
            safe_features[feature] = value
        else:
            skipped_features[feature] = value

    if safe_features:
        payload["features"] = _format_lxc_features(safe_features)
    else:
        payload.pop("features", None)

    if skipped_features:
        skipped = ", ".join(f"{key}={value}" for key, value in sorted(skipped_features.items()))
        warnings.append(
            {
                "code": "lxc_feature_skipped_for_api_token",
                "message": (
                    "Proxmox only allows API tokens to set token-safe LXC features. "
                    f"Skipped feature flags: {skipped}."
                ),
            }
        )

    return {
        "config": payload,
        "warnings": warnings,
        "changed": payload != config,
        "skipped_features": skipped_features,
    }


def _first_usable_ip(interfaces: Any, prefer_ipv4: bool = True) -> str | None:
    if not isinstance(interfaces, list):
        return None

    candidates: list[str] = []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        for ip_info in interface.get("ip-addresses", []):
            if not isinstance(ip_info, dict):
                continue
            address = ip_info.get("ip-address")
            if not isinstance(address, str):
                continue
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified:
                continue
            if prefer_ipv4 and parsed.version == 4:
                return address
            candidates.append(address)
    return candidates[0] if candidates else None


def _find_qemu_disk(config: dict[str, Any], disk: str | None = None) -> str | None:
    if disk:
        return disk

    bootdisk = config.get("bootdisk")
    if isinstance(bootdisk, str) and bootdisk:
        return bootdisk

    for prefix in QEMU_DISK_PREFIXES:
        for index in range(32):
            key = f"{prefix}{index}"
            if key in config:
                return key
    return None


def _package_install_command(packages: list[str]) -> str:
    quoted_packages = " ".join(shlex.quote(package) for package in packages)
    return (
        "set -eu; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "export DEBIAN_FRONTEND=noninteractive; "
        f"apt-get update && apt-get install -y {quoted_packages}; "
        "elif command -v dnf >/dev/null 2>&1; then "
        f"dnf install -y {quoted_packages}; "
        "elif command -v yum >/dev/null 2>&1; then "
        f"yum install -y {quoted_packages}; "
        "elif command -v apk >/dev/null 2>&1; then "
        f"apk add --no-cache {quoted_packages}; "
        "else echo 'No supported package manager found' >&2; exit 127; fi"
    )


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _redact_sensitive_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(config)
    if "cipassword" in redacted:
        redacted["cipassword"] = "<redacted>"
    return redacted


def _parse_storage_content(content: str | None) -> list[str]:
    return [item.strip() for item in (content or "").split(",") if item.strip()]


def _filename_from_url(url: str) -> str:
    filename = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if not filename:
        raise ValueError("Could not determine a filename from the image URL")
    return filename


class ProxmoxOperations:
    """Composable operations backed by the Proxmox REST API."""

    def __init__(self, client: ProxmoxClient, default_node: str) -> None:
        self.client = client
        self.default_node = default_node

    def node(self, node: str | None = None) -> str:
        return node or self.default_node

    async def api_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        wait_for_task: bool = True,
        task_timeout: float = 300.0,
    ) -> Any:
        return await self.client.request(
            method,
            path,
            params,
            wait_for_task=wait_for_task,
            task_timeout=task_timeout,
        )

    async def version(self) -> Any:
        return await self.client.request("GET", "/version")

    async def nodes(self) -> Any:
        return await self.client.request("GET", "/nodes")

    async def cluster_status(self) -> Any:
        return await self.client.request("GET", "/cluster/status")

    async def cluster_resources(
        self,
        resource_type: str | None = None,
        node: str | None = None,
    ) -> list[dict[str, Any]]:
        resources = await self.client.request("GET", "/cluster/resources")
        if not isinstance(resources, list):
            return []
        if resource_type:
            resources = [item for item in resources if item.get("type") == resource_type]
        if node:
            resources = [item for item in resources if item.get("node") == node]
        return resources

    async def guests(
        self,
        guest_type: GuestType | None = None,
        status: str | None = None,
        node: str | None = None,
    ) -> list[dict[str, Any]]:
        resources = await self.cluster_resources(node=node)
        guests = [item for item in resources if item.get("type") in {"qemu", "lxc"}]
        if guest_type:
            guests = [item for item in guests if item.get("type") == guest_type]
        if status:
            guests = [item for item in guests if item.get("status") == status]
        return guests

    async def next_id(self) -> Any:
        return await self.client.request("GET", "/cluster/nextid")

    async def node_status(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/status")

    async def storage(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/storage")

    async def storage_config(self, storage: str) -> dict[str, Any]:
        result = await self.client.request("GET", f"/storage/{storage}")
        return result if isinstance(result, dict) else {}

    async def ensure_storage_content(
        self,
        storage: str,
        content: str,
    ) -> dict[str, Any]:
        config = await self.storage_config(storage)
        current_content = _parse_storage_content(config.get("content"))

        if content in current_content:
            return {
                "storage": storage,
                "content": content,
                "changed": False,
                "previous_content": current_content,
                "new_content": current_content,
            }

        new_content = [*current_content, content]
        payload: dict[str, Any] = {"content": ",".join(new_content)}
        if config.get("digest"):
            payload["digest"] = config["digest"]

        await self.client.request("PUT", f"/storage/{storage}", payload)
        return {
            "storage": storage,
            "content": content,
            "changed": True,
            "previous_content": current_content,
            "new_content": new_content,
        }

    async def storage_content(
        self,
        storage: str,
        node: str | None = None,
        content: str | None = None,
    ) -> Any:
        node = self.node(node)
        params = {"content": content} if content else None
        return await self.client.request("GET", f"/nodes/{node}/storage/{storage}/content", params)

    async def delete_storage_content(
        self,
        storage: str,
        volume: str,
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        volume = quote_path_segment(volume)
        return await self.client.request(
            "DELETE",
            f"/nodes/{node}/storage/{storage}/content/{volume}",
            wait_for_task=True,
        )

    async def download_url_to_storage(
        self,
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
        node = self.node(node)
        params: dict[str, Any] = {
            "url": url,
            "filename": filename,
            "content": content,
            "checksum": checksum,
            "checksum-algorithm": checksum_algorithm,
            "compression": compression,
            "verify-certificates": verify_certificates,
        }
        return await self.client.request(
            "POST",
            f"/nodes/{node}/storage/{storage}/download-url",
            params,
            wait_for_task=True,
            task_timeout=task_timeout,
        )

    async def guest_config(self, guest_type: GuestType, vmid: int, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/{guest_type}/{vmid}/config")

    async def update_guest_config(
        self,
        guest_type: GuestType,
        vmid: int,
        config: dict[str, Any],
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "PUT",
            f"/nodes/{node}/{guest_type}/{vmid}/config",
            config,
            wait_for_task=True,
        )

    async def guest_status(self, guest_type: GuestType, vmid: int, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/{guest_type}/{vmid}/status/current")

    async def qemu_create(
        self,
        config: dict[str, Any],
        node: str | None = None,
        wait_for_task: bool = True,
        task_timeout: float = 300.0,
    ) -> Any:
        node = self.node(node)
        payload = dict(config)
        if "vmid" not in payload:
            payload["vmid"] = await self.next_id()
        return await self.client.request(
            "POST",
            f"/nodes/{node}/qemu",
            payload,
            wait_for_task=wait_for_task,
            task_timeout=task_timeout,
        )

    async def qemu_clone(
        self,
        vmid: int,
        newid: int,
        node: str | None = None,
        name: str | None = None,
        target_node: str | None = None,
        full: bool = True,
        storage: str | None = None,
    ) -> Any:
        node = self.node(node)
        params: dict[str, Any] = {
            "newid": newid,
            "full": full,
            "name": name,
            "target": target_node,
            "storage": storage,
        }
        return await self.client.request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/clone",
            params,
            wait_for_task=True,
        )

    async def qemu_resize_disk(
        self,
        vmid: int,
        disk: str,
        size: str,
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            {"disk": disk, "size": size},
            wait_for_task=True,
        )

    async def qemu_agent_network_interfaces(
        self,
        vmid: int,
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "GET",
            f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
        )

    async def wait_for_qemu_agent_ip(
        self,
        vmid: int,
        node: str | None = None,
        max_wait_seconds: float = 300.0,
        poll_interval: float = 5.0,
        prefer_ipv4: bool = True,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_seconds
        last_error: str | None = None
        interfaces: Any = None

        while True:
            try:
                interfaces = await self.qemu_agent_network_interfaces(vmid, node)
                ip = _first_usable_ip(interfaces, prefer_ipv4)
                if ip:
                    return {"ip": ip, "interfaces": interfaces}
            except Exception as exc:  # noqa: BLE001 - returned as structured workflow warning.
                last_error = str(exc)

            if time.monotonic() >= deadline:
                return {
                    "ip": None,
                    "interfaces": interfaces,
                    "error": last_error or "Timed out waiting for a usable guest-agent IP.",
                }
            await asyncio.sleep(poll_interval)

    async def qemu_agent_exec(
        self,
        vmid: int,
        command: list[str],
        node: str | None = None,
        max_wait_seconds: float = 300.0,
        poll_interval: float = 1.0,
    ) -> Any:
        node = self.node(node)
        result = await self.client.request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            {"command": command},
            repeat_sequence_params=True,
        )
        if not isinstance(result, dict) or "pid" not in result:
            raise RuntimeError(f"Unexpected guest-agent exec response: {result!r}")

        pid = result["pid"]
        deadline = time.monotonic() + max_wait_seconds
        while True:
            status = await self.client.request(
                "GET",
                f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                {"pid": pid},
            )
            if isinstance(status, dict) and status.get("exited"):
                if status.get("exitcode") not in (0, None):
                    error = status.get("err-data") or status.get("out-data") or status
                    raise RuntimeError(
                        "Guest-agent command failed with exit code "
                        f"{status.get('exitcode')}: {error}"
                    )
                return status

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for guest-agent command pid {pid}")
            await asyncio.sleep(poll_interval)

    async def _finish_cloudinit_vm(
        self,
        result: dict[str, Any],
        target_vmid: int,
        name: str,
        node: str,
        start: bool,
        wait_for_ip: bool,
        ip_timeout: float,
        poll_interval: float,
        user: str | None,
        password: str | None,
        ssh_public_key: str | None,
        ipconfig0: str | None,
        nameserver: str | None,
        searchdomain: str | None,
        memory: int | None,
        cores: int | None,
        disk_gb: int | None,
        disk: str | None,
        packages: list[str] | None,
        commands: list[str] | None,
        extra_config: dict[str, Any] | None,
        fail_on_init_error: bool,
    ) -> dict[str, Any]:
        warnings = result.setdefault("warnings", [])

        config: dict[str, Any] = {
            "name": name,
            "agent": "enabled=1",
            "ciuser": user,
            "cipassword": password,
            "sshkeys": ssh_public_key,
            "ipconfig0": ipconfig0,
            "nameserver": nameserver,
            "searchdomain": searchdomain,
            "memory": memory,
            "cores": cores,
        }
        config.update(extra_config or {})
        config = {key: value for key, value in config.items() if value is not None}
        result["sent_config"] = _redact_sensitive_config(config)
        result["configure"] = await self.update_guest_config("qemu", target_vmid, config, node)

        if disk_gb is not None:
            current_config = await self.guest_config("qemu", target_vmid, node)
            selected_disk = _find_qemu_disk(
                current_config if isinstance(current_config, dict) else {},
                disk,
            )
            if selected_disk:
                result["resize"] = await self.qemu_resize_disk(
                    target_vmid,
                    selected_disk,
                    f"{disk_gb}G",
                    node,
                )
            else:
                warnings.append(
                    _warning(
                        "qemu_disk_resize_skipped",
                        "Could not identify a QEMU boot disk to resize.",
                    )
                )

        if start:
            result["start"] = await self.power_action("qemu", target_vmid, "start", node)
        elif packages or commands or wait_for_ip:
            warnings.append(
                _warning(
                    "guest_initialization_skipped_not_started",
                    "The VM was not started, so IP discovery and guest-agent initialization "
                    "were skipped.",
                )
            )
            return result

        guest_agent_needed = wait_for_ip or bool(packages) or bool(commands)
        agent_result: dict[str, Any] | None = None
        if guest_agent_needed:
            agent_result = await self.wait_for_qemu_agent_ip(
                target_vmid,
                node,
                max_wait_seconds=ip_timeout,
                poll_interval=poll_interval,
            )
            result["guest_agent_network"] = agent_result
            if agent_result.get("ip"):
                result["ip"] = agent_result["ip"]
                if user:
                    result["ssh"] = {
                        "user": user,
                        "host": agent_result["ip"],
                        "command": f"ssh {user}@{agent_result['ip']}",
                    }
            else:
                warnings.append(
                    _warning(
                        "guest_agent_ip_unavailable",
                        str(agent_result.get("error") or "No usable guest-agent IP was reported."),
                    )
                )

        init_results: list[dict[str, Any]] = []
        init_commands: list[tuple[str, str]] = []
        if packages:
            init_commands.append(("install_packages", _package_install_command(packages)))
        for index, command in enumerate(commands or [], start=1):
            init_commands.append((f"command_{index}", command))

        for label, command in init_commands:
            try:
                status = await self.qemu_agent_exec(
                    target_vmid,
                    ["/bin/sh", "-lc", command],
                    node=node,
                    max_wait_seconds=ip_timeout,
                    poll_interval=min(poll_interval, 2.0),
                )
                init_results.append({"name": label, "status": status})
            except Exception as exc:  # noqa: BLE001 - workflow returns the partial VM result.
                warning = _warning("guest_initialization_failed", f"{label}: {exc}")
                warnings.append(warning)
                if fail_on_init_error:
                    raise

        if init_results:
            result["guest_initialization"] = init_results

        return result

    async def create_cloudinit_vm(
        self,
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
    ) -> dict[str, Any]:
        """Clone a cloud-init template and optionally initialize it through the guest agent."""

        node = self.node(node)
        target_vmid = vmid or int(await self.next_id())
        result: dict[str, Any] = {
            "node": node,
            "vmid": target_vmid,
            "name": name,
            "warnings": [],
        }

        result["clone"] = await self.qemu_clone(
            template_vmid,
            target_vmid,
            node=node,
            name=name,
            full=full,
            storage=storage,
        )

        return await self._finish_cloudinit_vm(
            result,
            target_vmid,
            name,
            node,
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

    async def create_cloudinit_vm_from_image(
        self,
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
    ) -> dict[str, Any]:
        """Download a cloud image as import content and create a cloud-init VM from it."""

        node = self.node(node)
        target_vmid = vmid or int(await self.next_id())
        filename = image_filename or _filename_from_url(image_url)
        image_volid = f"{image_storage}:import/{filename}"
        disk_import_parts = [f"{vm_storage}:0", f"import-from={image_volid}"]
        if disk_options:
            disk_import_parts.extend(
                item.strip() for item in disk_options.split(",") if item.strip()
            )

        result: dict[str, Any] = {
            "node": node,
            "vmid": target_vmid,
            "name": name,
            "image_volid": image_volid,
            "warnings": [],
        }

        if enable_import_content:
            result["ensure_import_content"] = await self.ensure_storage_content(
                image_storage,
                "import",
            )

        result["download"] = await self.download_url_to_storage(
            image_url,
            filename,
            "import",
            storage=image_storage,
            node=node,
            checksum=checksum,
            checksum_algorithm=checksum_algorithm,
            verify_certificates=verify_certificates,
            task_timeout=task_timeout,
        )

        create_config: dict[str, Any] = {
            "vmid": target_vmid,
            "name": name,
            "memory": memory,
            "cores": cores,
            "scsihw": "virtio-scsi-pci",
            "scsi0": ",".join(disk_import_parts),
            "ide2": f"{vm_storage}:cloudinit",
            "boot": "order=scsi0",
            "net0": f"virtio,bridge={bridge}",
            "agent": "enabled=1",
            "serial0": "socket",
            "vga": "serial0",
            "ostype": "l26",
        }
        create_config = {
            key: value
            for key, value in create_config.items()
            if value is not None
        }
        result["create_config"] = create_config
        result["create"] = await self.qemu_create(
            create_config,
            node=node,
            wait_for_task=True,
            task_timeout=task_timeout,
        )

        return await self._finish_cloudinit_vm(
            result,
            target_vmid,
            name,
            node,
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
            "scsi0",
            packages,
            commands,
            extra_config,
            fail_on_init_error,
        )

    async def qemu_delete(
        self,
        vmid: int,
        node: str | None = None,
        purge: bool = True,
        destroy_unreferenced_disks: bool = True,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "DELETE",
            f"/nodes/{node}/qemu/{vmid}",
            {"purge": purge, "destroy-unreferenced-disks": destroy_unreferenced_disks},
            wait_for_task=True,
        )

    async def lxc_create(
        self,
        config: dict[str, Any],
        node: str | None = None,
        wait_for_task: bool = True,
    ) -> Any:
        node = self.node(node)
        normalization = normalize_lxc_config_for_token(config)
        payload = normalization["config"]
        if "vmid" not in payload:
            payload["vmid"] = await self.next_id()
        result = await self.client.request(
            "POST",
            f"/nodes/{node}/lxc",
            payload,
            wait_for_task=wait_for_task,
        )
        if normalization["warnings"]:
            return {
                "result": result,
                "warnings": normalization["warnings"],
                "sent_config": payload,
                "skipped_features": normalization["skipped_features"],
            }
        return result

    def validate_lxc_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return normalize_lxc_config_for_token(config)

    async def lxc_delete(
        self,
        vmid: int,
        node: str | None = None,
        purge: bool = True,
        destroy_unreferenced_disks: bool = True,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "DELETE",
            f"/nodes/{node}/lxc/{vmid}",
            {"purge": purge, "destroy-unreferenced-disks": destroy_unreferenced_disks},
            wait_for_task=True,
        )

    async def power_action(
        self,
        guest_type: GuestType,
        vmid: int,
        action: PowerAction,
        node: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        node = self.node(node)
        allowed = {"start", "stop", "shutdown", "reboot", "reset", "suspend", "resume"}
        if action not in allowed:
            raise ValueError(f"Unsupported power action: {action}")
        return await self.client.request(
            "POST",
            f"/nodes/{node}/{guest_type}/{vmid}/status/{action}",
            params,
            wait_for_task=True,
        )

    async def snapshots(self, guest_type: GuestType, vmid: int, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/{guest_type}/{vmid}/snapshot")

    async def create_snapshot(
        self,
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
        description: str | None = None,
        vmstate: bool | None = None,
    ) -> Any:
        node = self.node(node)
        params = {"snapname": snapname, "description": description, "vmstate": vmstate}
        return await self.client.request(
            "POST",
            f"/nodes/{node}/{guest_type}/{vmid}/snapshot",
            params,
            wait_for_task=True,
        )

    async def delete_snapshot(
        self,
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        snapname = quote_path_segment(snapname)
        return await self.client.request(
            "DELETE",
            f"/nodes/{node}/{guest_type}/{vmid}/snapshot/{snapname}",
            wait_for_task=True,
        )

    async def rollback_snapshot(
        self,
        guest_type: GuestType,
        vmid: int,
        snapname: str,
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        snapname = quote_path_segment(snapname)
        return await self.client.request(
            "POST",
            f"/nodes/{node}/{guest_type}/{vmid}/snapshot/{snapname}/rollback",
            wait_for_task=True,
        )

    async def backup_guest(
        self,
        vmid: int,
        node: str | None = None,
        storage: str = "local",
        mode: Literal["snapshot", "suspend", "stop"] = "snapshot",
        compress: str | None = "zstd",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        node = self.node(node)
        params: dict[str, Any] = {
            "vmid": vmid,
            "storage": storage,
            "mode": mode,
            "compress": compress,
        }
        params.update(extra or {})
        return await self.client.request(
            "POST",
            f"/nodes/{node}/vzdump",
            params,
            wait_for_task=True,
        )

    async def templates(self, node: str | None = None, section: str | None = None) -> Any:
        node = self.node(node)
        params = {"section": section} if section else None
        return await self.client.request("GET", f"/nodes/{node}/aplinfo", params)

    async def download_template(
        self,
        template: str,
        node: str | None = None,
        storage: str = "local",
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "POST",
            f"/nodes/{node}/aplinfo",
            {"template": template, "storage": storage},
            wait_for_task=True,
        )

    async def tasks(
        self,
        node: str | None = None,
        limit: int = 50,
        source: str | None = None,
        userfilter: str | None = None,
        typefilter: str | None = None,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "GET",
            f"/nodes/{node}/tasks",
            {
                "limit": limit,
                "source": source,
                "userfilter": userfilter,
                "typefilter": typefilter,
            },
        )

    async def task_status(self, upid: str, node: str | None = None) -> Any:
        node = node or upid.split(":")[1]
        return await self.client.request(
            "GET",
            f"/nodes/{node}/tasks/{quote_path_segment(upid)}/status",
        )

    async def task_log(
        self,
        upid: str,
        node: str | None = None,
        start: int = 0,
        limit: int = 200,
    ) -> Any:
        node = node or upid.split(":")[1]
        return await self.client.request(
            "GET",
            f"/nodes/{node}/tasks/{quote_path_segment(upid)}/log",
            {"start": start, "limit": limit},
        )

    async def services(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/services")

    async def service_state(self, service: str, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/services/{service}/state")

    async def service_action(
        self,
        service: str,
        action: Literal["start", "stop", "restart", "reload"],
        node: str | None = None,
    ) -> Any:
        node = self.node(node)
        return await self.client.request(
            "POST",
            f"/nodes/{node}/services/{service}/state/{action}",
            wait_for_task=True,
        )

    async def network(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/network")

    async def firewall_rules(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/firewall/rules")

    async def apt_updates(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/apt/update")

    async def apt_versions(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request("GET", f"/nodes/{node}/apt/versions")

    async def run_apt_update(self, node: str | None = None) -> Any:
        node = self.node(node)
        return await self.client.request(
            "POST",
            f"/nodes/{node}/apt/update",
            wait_for_task=True,
        )
