"""Small async Proxmox VE REST client used by the MCP tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from nodepilot.config import Settings

JsonDict = dict[str, Any]


class ProxmoxAPIError(RuntimeError):
    """Raised when Proxmox returns a non-success HTTP response."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        message: str,
        errors: Any | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.errors = errors
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {message}")


class ProxmoxTaskError(RuntimeError):
    """Raised when a Proxmox background task exits unsuccessfully."""

    def __init__(self, upid: str, status: Mapping[str, Any]) -> None:
        self.upid = upid
        self.status = dict(status)
        exit_status = status.get("exitstatus", "unknown")
        super().__init__(f"Proxmox task {upid} failed: {exit_status}")


def _normalize_path(path: str) -> str:
    """Normalize user-provided API paths to `/path` under `/api2/json`."""

    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        marker = "/api2/json"
        _, _, suffix = path.partition(marker)
        path = suffix or "/"
    if path.startswith("/api2/json"):
        path = path.removeprefix("/api2/json")
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _encode_params(
    params: Mapping[str, Any] | None,
    *,
    repeat_sequence_params: bool = False,
) -> dict[str, str] | list[tuple[str, str]]:
    """Convert Python values to Proxmox-friendly form/query parameters."""

    if repeat_sequence_params:
        repeated: list[tuple[str, str]] = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                repeated.append((key, "1" if value else "0"))
            elif isinstance(value, (list, tuple, set)):
                repeated.extend((key, str(item)) for item in value)
            else:
                repeated.append((key, str(value)))
        return repeated

    encoded: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "1" if value else "0"
        elif isinstance(value, (list, tuple, set)):
            encoded[key] = ",".join(str(item) for item in value)
        else:
            encoded[key] = str(value)
    return encoded


def quote_path_segment(value: str) -> str:
    """URL-encode one Proxmox path segment, including slashes inside values."""

    return quote(value, safe="")


class ProxmoxClient:
    """Async Proxmox VE API token client."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            headers={"Authorization": settings.authorization_header},
            timeout=settings.timeout,
            verify=settings.verify_ssl,
        )

    async def __aenter__(self) -> ProxmoxClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_url(self, path: str) -> str:
        return f"{self.settings.api_url}{_normalize_path(path)}"

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        wait_for_task: bool = False,
        task_timeout: float = 300.0,
        poll_interval: float = 1.0,
        repeat_sequence_params: bool = False,
    ) -> Any:
        """Call a Proxmox API endpoint and optionally wait for UPID completion."""

        method = method.upper()
        normalized_path = _normalize_path(path)
        encoded_params = _encode_params(
            params,
            repeat_sequence_params=repeat_sequence_params,
        )
        request_kwargs: dict[str, Any] = {}

        if method in {"GET", "DELETE"}:
            request_kwargs["params"] = encoded_params
        elif repeat_sequence_params:
            request_kwargs["content"] = urlencode(encoded_params)
            request_kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}
        else:
            request_kwargs["data"] = encoded_params

        response = await self._client.request(
            method,
            self.build_url(normalized_path),
            **request_kwargs,
        )
        result = self._unwrap_response(response, method, normalized_path)

        if wait_for_task and isinstance(result, str) and result.startswith("UPID:"):
            return await self.wait_for_task(
                result,
                task_timeout=task_timeout,
                poll_interval=poll_interval,
            )
        return result

    def _unwrap_response(self, response: httpx.Response, method: str, path: str) -> Any:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text}

        if response.status_code >= 400:
            message = "Proxmox API error"
            errors = None
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("data") or message)
                errors = payload.get("errors")
            raise ProxmoxAPIError(
                method=method,
                path=path,
                status_code=response.status_code,
                message=message,
                errors=errors,
            )

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def wait_for_task(
        self,
        upid: str,
        *,
        task_timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> JsonDict:
        """Poll `/nodes/{node}/tasks/{upid}/status` until a task stops."""

        parts = upid.split(":")
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid Proxmox UPID: {upid}")

        node = parts[1]
        encoded_upid = quote_path_segment(upid)
        deadline = time.monotonic() + task_timeout

        while True:
            status = await self.request(
                "GET",
                f"/nodes/{node}/tasks/{encoded_upid}/status",
                wait_for_task=False,
            )
            if isinstance(status, dict) and status.get("status") == "stopped":
                if status.get("exitstatus") not in (None, "OK"):
                    raise ProxmoxTaskError(upid, status)
                return {"upid": upid, "status": status}

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Proxmox task {upid}")
            await asyncio.sleep(poll_interval)
