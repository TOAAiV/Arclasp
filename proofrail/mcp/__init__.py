"""
proofrail.mcp — MCP (Model Context Protocol) adapter for the ProofRail SDK.

Provides :class:`ProofRailMcpAdapter`, a lightweight wrapper that records
every MCP tool call through a ProofRail :class:`~proofrail.chain.Chain`
before the underlying tool handler executes.

Requires the ``mcp`` extra::

    pip install proofrail[mcp]

Usage::

    from proofrail.mcp import ProofRailMcpAdapter

    adapter = ProofRailMcpAdapter(chain=chain, agent_name="my-mcp-server")

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        return await adapter.handle_tool_call(name, arguments, _execute_tool)
"""

from proofrail.mcp.adapter import ProofRailMcpAdapter

__all__ = ["ProofRailMcpAdapter"]
