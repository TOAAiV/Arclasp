"""
arclasp.mcp.adapter — Governance adapter for MCP tool servers.

:class:`ArclaspMcpAdapter` intercepts MCP tool calls, records each
invocation through a Arclasp :class:`~arclasp.chain.Chain`, and only
forwards to the real handler when the policy engine allows it.

The ``mcp`` package itself is only imported when
:meth:`ArclaspMcpAdapter.install` is called (so importing this module does
not require the extra to be installed).

Typical integration pattern
---------------------------
::

    import arclasp
    from arclasp.mcp import ArclaspMcpAdapter
    from mcp.server import Server

    arclasp.init(api_key="prail_...")
    server = Server("my-tools")

    async with arclasp.Chain("mcp-session") as chain:
        adapter = ArclaspMcpAdapter(chain=chain, agent_name="my-tools")

        @server.call_tool()
        async def handle_call_tool(name: str, arguments: dict):
            return await adapter.handle_tool_call(
                tool_name=name,
                arguments=arguments,
                handler=your_actual_handler,
            )

        await server.run(...)

Manual / framework-independent pattern
---------------------------------------
::

    result = await adapter.handle_tool_call(
        tool_name="send_email",
        arguments={"to": "user@example.com", "subject": "Hi"},
        handler=my_send_email_func,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Type alias for a coroutine that executes a single tool call.
ToolHandler = Callable[[str, dict], Awaitable[Any]]


class ArclaspMcpAdapter:
    """
    Records every MCP tool invocation through a Arclasp chain before
    execution.

    Parameters
    ----------
    chain : arclasp.chain.Chain
        An **already-entered** chain context (i.e. the chain has been started
        and its ``chain_id`` is available).
    agent_name : str
        The ``agent_name`` that will be attached to each recorded event.
        Defaults to ``"mcp-agent"``.
    parent_agent_name : str | None
        Optional parent agent name forwarded to every recorded event.
    """

    def __init__(
        self,
        chain: Any,  # arclasp.chain.Chain — typed as Any to avoid circular import
        agent_name: str = "mcp-agent",
        parent_agent_name: str | None = None,
    ) -> None:
        self.chain = chain
        self.agent_name = agent_name
        self.parent_agent_name = parent_agent_name

    # ------------------------------------------------------------------
    # Core intercept method
    # ------------------------------------------------------------------

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        handler: ToolHandler,
    ) -> Any:
        """
        Record *tool_name* through Arclasp governance, then call *handler*.

        Parameters
        ----------
        tool_name : str
            The MCP tool name (maps to ``action_name``).
        arguments : dict
            Raw tool arguments passed by the model.
        handler : async callable
            The underlying tool implementation.  Called as
            ``await handler(tool_name, arguments)`` only when the policy
            engine returns an allow decision.

        Returns
        -------
        Any
            The value returned by *handler*.

        Raises
        ------
        arclasp.ActionDeniedError
            When the policy engine denies the tool call.
        arclasp.ArclaspKillSwitchError
            When the organisation kill switch is active.
        """
        await self.chain.record_agent_action(
            agent_name=self.agent_name,
            action_type="tool_call",
            action_name=tool_name,
            payload=arguments,
            parent_agent_name=self.parent_agent_name,
        )

        logger.debug("Arclasp governance passed for tool '%s' — executing", tool_name)
        return await handler(tool_name, arguments)

    # ------------------------------------------------------------------
    # Convenience: patch an MCP Server instance in-place
    # ------------------------------------------------------------------

    def install(self, server: Any) -> None:
        """
        Not supported with mcp >= 1.0.

        The mcp SDK no longer exposes a patchable ``_call_tool_handler``
        attribute.  Wire Arclasp governance directly in your
        ``@server.call_tool()`` handler instead::

            @server.call_tool()
            async def handle_call_tool(name: str, arguments: dict):
                return await adapter.handle_tool_call(
                    tool_name=name,
                    arguments=arguments,
                    handler=your_actual_handler,
                )
        """
        raise RuntimeError(
            "ArclaspMcpAdapter.install() is not supported with the current mcp "
            "SDK (>= 1.0).  Use handle_tool_call() directly inside your "
            "@server.call_tool() handler instead.  See the Arclasp README for "
            "an example."
        )

    # ------------------------------------------------------------------
    # Decorator helper
    # ------------------------------------------------------------------

    def tool(self, tool_name: str) -> Callable[[ToolHandler], ToolHandler]:
        """
        Decorator that wraps a bare async tool implementation with Arclasp
        governance.  The decorated function receives ``(tool_name, arguments)``
        and returns the tool result.

        Usage::

            @adapter.tool("query_database")
            async def query_database(name: str, arguments: dict):
                ...  # your implementation
        """

        def decorator(fn: ToolHandler) -> ToolHandler:
            async def _governed(name: str, arguments: dict) -> Any:
                return await self.handle_tool_call(name, arguments, fn)

            _governed.__name__ = fn.__name__
            _governed.__doc__ = fn.__doc__
            return _governed

        return decorator
