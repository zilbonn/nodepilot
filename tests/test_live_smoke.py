from __future__ import annotations

import os

import pytest

from nodepilot.client import ProxmoxClient
from nodepilot.config import load_settings
from nodepilot.operations import ProxmoxOperations

pytestmark = pytest.mark.skipif(
    os.getenv("PROXMOX_RUN_LIVE_TESTS") != "1",
    reason="live Proxmox smoke tests are opt-in",
)


@pytest.mark.asyncio
async def test_live_read_only_smoke() -> None:
    settings = load_settings()
    async with ProxmoxClient(settings) as client:
        ops = ProxmoxOperations(client, settings.default_node)
        version = await ops.version()
        nodes = await ops.nodes()
        resources = await ops.cluster_resources()
        storage = await ops.storage()

    assert "version" in version
    assert isinstance(nodes, list)
    assert isinstance(resources, list)
    assert isinstance(storage, list)
