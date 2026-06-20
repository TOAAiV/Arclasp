"""
proofrail.mcp.adapter — Governance adapter for MCP tool servers.

:class:`ProofRailMcpAdapter` intercepts MCP tool calls, records each
invocation through a ProofRail :class:`~proofrail.chain.Chain`, and only
forwards to the real handler when the policy engine allows it.

The ``mcp`` package itself is only imported when
:meth:`ProofRailMcpAdapter.install` is called (so importing this module does
not require the extra to be installed).

Typical integration pattern
---------------------------
::

    import proofrail
    from proofrail.mcp import ProofRailMcpAdapter
    from mcp.server import Server

    proofrail.init(api_key="prail_...")
    server = Server("my-tools")

    async with proofrail.Chain("mcp-session") as chain:
        adapter = ProofRailMcpAdapter(chain=chain, agent_name="my-tools")
        adapter.install(server)            # patches server.call_tool handler
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

# Internal attribute name used by the reference mcp SDK to store the registered
# call_tool handler.  Checked at install() time so the error message is specific.
_CALL_TOOL_HANDLER_ATTR = "_call_tool_handler"


class ProofRailMcpAdapter:
    """
    Records every MCP tool invocation through a ProofRail chain before
    execution.

    Parameters
    ----------
    chain : proofrail.chain.Chain
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
        chain: Any,  # proofrail.chain.Chain — typed as Any to avoid circular import
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
        Record *tool_name* through ProofRail governance, then call *handler*.

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
        proofrail.ActionDeniedError
            When the policy engine denies the tool call.
        proofrail.ProofRailKillSwitchError
            When the organisation kill switch is active.
        """
        await self.chain.record_agent_action(
            agent_name=self.agent_name,
            action_type="tool_call",
            action_name=tool_name,
            payload=arguments,
            parent_agent_name=self.parent_agent_name,
        )

        logger.debug("ProofRail governance passed for tool '%s' — executing", tool_name)
        return await handler(tool_name, arguments)

    # ------------------------------------------------------------------
    # Convenience: patch an MCP Server instance in-place
    # ------------------------------------------------------------------

    def install(self, server: Any) -> None:
        """
        Patch *server*'s ``call_tool`` handler so every tool call is
        recorded through this adapter automatically.

        This wraps whatever coroutine is already registered on
        ``server._call_tool_handler`` (the internal attribute used by the
        reference ``mcp`` SDK).  If the attribute does not exist, a
        ``RuntimeError`` is raised with instructions for manual wiring.

        Parameters
        ----------
        server : mcp.server.Server
            An MCP ``Server`` instance whose ``call_tool`` handler should be
            wrapped.
        """
        original: ToolHandler | None = getattr(server, _CALL_TOOL_HANDLER_ATTR, None)
        if original is None:
            raise RuntimeError(
                f"Cannot find '{_CALL_TOOL_HANDLER_ATTR}' on {server!r}.  Either the "
                "mcp Server API has changed or no @server.call_tool() handler "
                "has been registered yet.  Use handle_tool_call() directly "
                "instead of install()."
            )

        async def _wrapped(tool_name: str, arguments: dict) -> Any:
            return await self.handle_tool_call(tool_name, arguments, original)

        setattr(server, _CALL_TOOL_HANDLER_ATTR, _wrapped)
        logger.info(
            "ProofRail MCP adapter installed on server %r (agent_name=%r)",
            server,
            self.agent_name,
        )

    # ------------------------------------------------------------------
    # Decorator helper
    # ------------------------------------------------------------------

    def tool(self, tool_name: str) -> Callable[[ToolHandler], ToolHandler]:
        """
        Decorator that wraps a bare async tool implementation with ProofRail
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
