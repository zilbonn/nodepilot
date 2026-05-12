from __future__ import annotations

import pytest
from httpx import Response

from nodepilot.client import ProxmoxAPIError, ProxmoxClient, ProxmoxTaskError
from nodepilot.config import Settings


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


@pytest.mark.asyncio
async def test_request_adds_auth_header_and_unwraps_data(respx_mock) -> None:
    route = respx_mock.get("https://pve.example.test:8006/api2/json/version").mock(
        return_value=Response(200, json={"data": {"version": "8.2.2"}})
    )

    async with ProxmoxClient(settings()) as client:
        result = await client.request("GET", "/version")

    assert result == {"version": "8.2.2"}
    assert (
        route.calls.last.request.headers["authorization"]
        == "PVEAPIToken=nodepilot@pve!mcp=secret"
    )


@pytest.mark.asyncio
async def test_request_normalizes_full_api_path(respx_mock) -> None:
    route = respx_mock.get("https://pve.example.test:8006/api2/json/nodes").mock(
        return_value=Response(200, json={"data": []})
    )

    async with ProxmoxClient(settings()) as client:
        await client.request("GET", "/api2/json/nodes")

    assert route.called


@pytest.mark.asyncio
async def test_post_encodes_form_values(respx_mock) -> None:
    route = respx_mock.post("https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu").mock(
        return_value=Response(
            200,
            json={"data": "UPID:pve-node-1:1:2:3:qmcreate:128:nodepilot@pve:"},
        )
    )

    async with ProxmoxClient(settings()) as client:
        result = await client.request(
            "POST",
            "/nodes/pve-node-1/qemu",
            {"vmid": 128, "name": "test", "start": True, "tags": ["mcp", "test"]},
            wait_for_task=False,
        )

    assert result.startswith("UPID:pve-node-1:")
    body = route.calls.last.request.content.decode()
    assert "vmid=128" in body
    assert "start=1" in body
    assert "tags=mcp%2Ctest" in body


@pytest.mark.asyncio
async def test_post_can_repeat_sequence_form_values(respx_mock) -> None:
    route = respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/agent/exec"
    ).mock(return_value=Response(200, json={"data": {"pid": 42}}))

    async with ProxmoxClient(settings()) as client:
        result = await client.request(
            "POST",
            "/nodes/pve-node-1/qemu/100/agent/exec",
            {"command": ["/bin/sh", "-lc", "echo ready"]},
            repeat_sequence_params=True,
        )

    assert result == {"pid": 42}
    body = route.calls.last.request.content.decode()
    assert body.startswith("command=%2Fbin%2Fsh&command=-lc&command=echo+ready")


@pytest.mark.asyncio
async def test_error_mapping(respx_mock) -> None:
    respx_mock.get("https://pve.example.test:8006/api2/json/version").mock(
        return_value=Response(500, json={"message": "boom", "errors": {"field": "bad"}})
    )

    async with ProxmoxClient(settings()) as client:
        with pytest.raises(ProxmoxAPIError) as exc_info:
            await client.request("GET", "/version")

    assert exc_info.value.status_code == 500
    assert exc_info.value.errors == {"field": "bad"}


@pytest.mark.asyncio
async def test_wait_for_task_success(respx_mock) -> None:
    upid = "UPID:pve-node-1:0001:0002:0003:qmstart:100:nodepilot@pve:"
    encoded = "UPID%3Apve-node-1%3A0001%3A0002%3A0003%3Aqmstart%3A100%3Anodepilot%40pve%3A"
    respx_mock.post(
        "https://pve.example.test:8006/api2/json/nodes/pve-node-1/qemu/100/status/start"
    ).mock(return_value=Response(200, json={"data": upid}))
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/pve-node-1/tasks/{encoded}/status"
    ).mock(
        return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
    )

    async with ProxmoxClient(settings()) as client:
        result = await client.request(
            "POST",
            "/nodes/pve-node-1/qemu/100/status/start",
            wait_for_task=True,
        )

    assert result["upid"] == upid
    assert result["status"]["exitstatus"] == "OK"


@pytest.mark.asyncio
async def test_wait_for_task_failure(respx_mock) -> None:
    upid = "UPID:pve-node-1:0001:0002:0003:qmstart:100:nodepilot@pve:"
    encoded = "UPID%3Apve-node-1%3A0001%3A0002%3A0003%3Aqmstart%3A100%3Anodepilot%40pve%3A"
    respx_mock.get(
        f"https://pve.example.test:8006/api2/json/nodes/pve-node-1/tasks/{encoded}/status"
    ).mock(
        return_value=Response(200, json={"data": {"status": "stopped", "exitstatus": "failed"}})
    )

    async with ProxmoxClient(settings()) as client:
        with pytest.raises(ProxmoxTaskError):
            await client.wait_for_task(upid)
