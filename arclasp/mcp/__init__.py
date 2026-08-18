"""
arclasp.mcp — MCP (Model Context Protocol) adapter for the Arclasp SDK.

Provides :class:`ArclaspMcpAdapter`, a lightweight wrapper that records
every MCP tool call through a Arclasp :class:`~arclasp.chain.Chain`
before the underlying tool handler executes.

Requires the ``mcp`` extra::

    pip install arclasp[mcp]

Usage::

    from arclasp.mcp import ArclaspMcpAdapter

    adapter = ArclaspMcpAdapter(chain=chain, agent_name="my-mcp-server")

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        return await adapter.handle_tool_call(name, arguments, _execute_tool)
"""

from arclasp.mcp.adapter import ArclaspMcpAdapter

__all__ = ["ArclaspMcpAdapter"]
