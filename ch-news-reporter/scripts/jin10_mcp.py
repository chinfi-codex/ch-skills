#!/usr/bin/env python3
"""Minimal Jin10 MCP client for ch-news-reporter."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import http_utils


JIN10_SERVER_URL = "https://mcp.jin10.com/mcp"
JIN10_PROTOCOL_VERSION = "2025-11-25"
JIN10_AUTH_TOKEN = os.getenv("JIN10_AUTH_TOKEN")

# The MCP server can be slow to answer list_flash; keep the historical 30s.
JIN10_TIMEOUT = 30.0


class Jin10McpError(RuntimeError):
    pass


class Jin10McpClient:
    def __init__(self, server_url: str = JIN10_SERVER_URL, token: str | None = None):
        self.server_url = server_url
        self.token = token or JIN10_AUTH_TOKEN
        if not self.token:
            raise Jin10McpError("Missing JIN10_AUTH_TOKEN environment variable")
        self._headers = {
            # MCP spec: clients must accept both JSON and SSE responses.
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        self._initialized = False
        self._session_id: str | None = None

    def close(self) -> None:
        # No persistent connection is held (requests go through http_utils);
        # kept so existing callers' finally: client.close() still works.
        pass

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "protocolVersion": JIN10_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "ch-news-reporter-skill",
                    "version": "0.1.0",
                },
            },
        )
        self._notify("notifications/initialized", {})
        self._initialized = True

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.ensure_initialized()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") is True:
            raise Jin10McpError(f"Tool business error: {name}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        raise Jin10McpError(f"Missing structuredContent in MCP result: {name}")

    def list_flash(self, cursor: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if cursor:
            arguments["cursor"] = cursor
        return self.call_tool("list_flash", arguments)

    def search_flash(self, keyword: str) -> dict[str, Any]:
        return self.call_tool("search_flash", {"keyword": keyword})

    def get_quote(self, code: str) -> dict[str, Any]:
        return self.call_tool("get_quote", {"code": code})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._post(payload)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        response = self._post(payload)
        session_id = http_utils.get_header(response.headers, "mcp-session-id")
        if session_id:
            self._session_id = session_id
        data = self._decode_response_payload(response)
        if "error" in data:
            message = data["error"].get("message") or f"JSON-RPC error: {method}"
            raise Jin10McpError(message)
        return data.get("result", {})

    def _post(self, payload: dict[str, Any]) -> http_utils.FetchResult:
        return http_utils.fetch_response(
            self.server_url,
            method="POST",
            headers={**self._headers, **self._session_headers()},
            body=json.dumps(payload).encode("utf-8"),
            timeout=JIN10_TIMEOUT,
        )

    def _session_headers(self) -> dict[str, str]:
        if not self._session_id:
            return {}
        return {"mcp-session-id": self._session_id}

    def _decode_response_payload(
        self, response: http_utils.FetchResult
    ) -> dict[str, Any]:
        content_type = (
            http_utils.get_header(response.headers, "content-type") or ""
        ).lower()
        if "text/event-stream" in content_type:
            return self._parse_sse_jsonrpc(response.text or "")
        return json.loads(response.text or "")

    def _parse_sse_jsonrpc(self, body: str) -> dict[str, Any]:
        data_lines: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            raise Jin10McpError("Missing SSE data payload")
        return json.loads("\n".join(data_lines))
