"""MCP bridge: load external MCP servers (e.g. 1Password) and expose their tools
as browser-use actions so Bellamy can call them mid-browsing.

Config format (BELLAMY_HOME/mcp.json) — same as Claude Desktop:

{
  "mcpServers": {
    "1password": {"command": "npx", "args": ["-y", "@1password/mcp-server"], "env": {}}
  }
}
"""

import asyncio
import json
import contextlib
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, create_model
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .paths import MCP_CONFIG_PATH


def load_mcp_config() -> dict:
    if not MCP_CONFIG_PATH.exists():
        return {}
    with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("mcpServers", {})


def _schema_to_model(server: str, tool_name: str, schema: dict) -> type[BaseModel] | None:
    """Convert a JSON-schema inputSchema into a dynamic pydantic model."""
    props: dict = (schema or {}).get("properties", {})
    required: list = (schema or {}).get("required", [])
    if not props:
        return None
    fields = {}
    for fname, fdef in props.items():
        ftype = fdef.get("type", "string")
        py_type: Any = {
            "string": str,
            "number": float,
            "integer": int,
            "boolean": bool,
            "array": list[Any],
            "object": dict[str, Any],
        }.get(ftype, Any)
        if fname in required:
            fields[fname] = (py_type, ...)
        else:
            default = fdef.get("default") if "default" in fdef else None
            fields[fname] = (py_type | None, default)
    return create_model(f"{server}_{tool_name}_params", **fields)


class MCPServerBridge:
    """One MCP server connection with lazily-created tool-call actions."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.session: ClientSession | None = None
        self.tools: list = []
        self._ctx = None
        self._cm_stack = contextlib.AsyncExitStack()

    async def start(self):
        params = StdioServerParameters(
            command=self.spec.get("command", "npx"),
            args=self.spec.get("args", []),
            # Preserve PATH, HOME and desktop/1Password session variables.
            env={**os.environ, **(self.spec.get("env") or {})},
        )
        read, write = await self._cm_stack.enter_async_context(stdio_client(params))
        self.session = await self._cm_stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        result = await self.session.list_tools()
        self.tools = result.tools
        return self

    async def call(self, tool_name: str, arguments: dict) -> str:
        if self.session is None:
            return f"[bellamy-mcp] server '{self.name}' is not connected"
        res = await self.session.call_tool(tool_name, arguments)
        parts = []
        for c in (res.content or []):
            if hasattr(c, "text"):
                parts.append(c.text)
        return "\n".join(parts) or "(empty response)"

    async def stop(self):
        with contextlib.suppress(Exception):
            await self._cm_stack.aclose()


class MCPManager:
    """Starts all configured MCP servers and registers their tools on a browser-use Tools object."""

    def __init__(self):
        self.bridges: dict[str, MCPServerBridge] = {}
        self.enabled = False

    async def start(self) -> "MCPManager":
        servers = load_mcp_config()
        for name, spec in servers.items():
            if spec.get("disabled"):
                continue
            bridge = MCPServerBridge(name, spec)
            try:
                await asyncio.wait_for(bridge.start(), timeout=20)
                self.bridges[name] = bridge
                self.enabled = True
                tool_names = [t.name for t in bridge.tools]
                print(f"🔌 [bellamy-mcp] connected '{name}' — tools: {', '.join(tool_names) or '(none)'}")
            except Exception as e:
                await bridge.stop()
                print(f"⚠️  [bellamy-mcp] failed to start '{name}': {e}")
        return self

    def register_tools(self, tools):
        """Register every MCP tool as a browser-use custom action."""
        for server_name, bridge in self.bridges.items():
            for tool in bridge.tools:
                self._register_one(tools, server_name, bridge, tool)

    def _register_one(self, tools, server_name: str, bridge: MCPServerBridge, tool):
        tool_name = tool.name
        desc = (tool.description or f"Call MCP tool '{tool_name}' on server '{server_name}'.").strip()
        param_model = _schema_to_model(server_name, tool_name, tool.inputSchema)
        safe_server = re.sub(r"[^a-zA-Z0-9_]", "_", server_name)
        safe_tool = re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)
        action_name = f"mcp_{safe_server}_{safe_tool}"

        # Set the function name before decoration: Registry keys actions by
        # __name__ at decoration time. This prevents different MCP tools from
        # overwriting each other under a shared `_mcp_action` key.
        if param_model is not None:
            async def mcp_action(params):
                return await bridge.call(tool_name, params.model_dump(exclude_none=True))

            mcp_action.__name__ = action_name
            tools.action(desc, param_model=param_model)(mcp_action)
        else:
            async def mcp_action():
                return await bridge.call(tool_name, {})

            mcp_action.__name__ = action_name
            tools.action(desc)(mcp_action)

    async def stop(self):
        for bridge in self.bridges.values():
            await bridge.stop()
        self.bridges.clear()
