"""
aag.mcp — MCP (Model Context Protocol) adapter for the aag SDK.

Provides :class:`AagMcpAdapter`, a lightweight wrapper that records every
MCP tool call through an aag :class:`~aag.chain.Chain` before the underlying
tool handler executes.

Requires the ``mcp`` extra::

    pip install aag[mcp]

Usage::

    from aag.mcp import AagMcpAdapter

    adapter = AagMcpAdapter(chain=chain, agent_name="my-mcp-server")

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        return await adapter.handle_tool_call(name, arguments, _execute_tool)
"""

from aag.mcp.adapter import AagMcpAdapter

__all__ = ["AagMcpAdapter"]
