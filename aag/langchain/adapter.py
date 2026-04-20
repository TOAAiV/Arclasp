"""
aag.langchain.adapter — Governance wrapper for LangChain chains and agents.

3-line integration pattern
--------------------------
    import aag
    from aag.langchain import govern

    aag.init(api_key="aag_...")
    governed = govern(agent_executor, chain_name="customer-support-agent")

    # Drop-in replacement — same interface as the original executor:
    result = await governed.ainvoke({"input": "What is the weather in Paris?"})

How it works
------------
1.  ``govern()`` wraps a LangChain ``AgentExecutor`` (or any ``Runnable``
    with ``.invoke`` / ``.ainvoke``) in a ``GovernedChain`` instance.
2.  On every invocation, an aag ``Chain`` context manager is opened so the
    full agent run appears as a single governed chain in the dashboard.
3.  An :class:`AagLangChainCallback` instance is injected into every call
    via ``config={"callbacks": [...]}`` — LangChain forwards it to all
    nested tool and LLM invocations automatically.
4.  The callback fires ``record_agent_action`` for each of:
        tool_call     — before every tool execution
        tool_result   — after a tool returns successfully
        tool_error    — when a tool raises an exception
        llm_call      — before every LLM invocation
        llm_result    — after an LLM returns
        llm_error     — when an LLM raises an exception

Policy enforcement
------------------
``record_agent_action`` is called for every tool and LLM event.  If the aag
backend returns a ``"deny"`` decision, ``ActionDeniedError`` propagates out
of ``ainvoke`` / ``invoke`` — the agent execution is halted at that point.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aag import client as _aag_client
from aag.chain import Chain
from aag.langchain.callbacks import AagLangChainCallback, _BaseCallbackHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def govern(
    agent_executor_or_chain: Any,
    chain_name: str = "langchain_workflow",
    metadata: dict | None = None,
) -> "GovernedChain":
    """
    Wrap a LangChain ``AgentExecutor`` or ``Runnable`` chain with aag
    governance.

    Parameters
    ----------
    agent_executor_or_chain :
        Any LangChain object with ``.invoke`` and ``.ainvoke`` methods —
        typically an ``AgentExecutor``, ``LLMChain``, or LCEL ``Runnable``.
    chain_name : str
        Name recorded in the aag dashboard for each invocation.  Defaults
        to ``"langchain_workflow"``.
    metadata : dict, optional
        Extra key/value pairs attached to every chain opened by this wrapper
        (e.g. ``{"agent_version": "2.1", "team": "support"}``).

    Returns
    -------
    GovernedChain
        A drop-in wrapper with identical ``.invoke`` / ``.ainvoke``
        signatures.

    Raises
    ------
    ImportError
        If neither ``langchain_core`` nor ``langchain`` is installed.
    TypeError
        If the object does not have ``.invoke`` / ``.ainvoke`` methods.
    """
    # Verify LangChain is actually available before we hand back a wrapper
    # that would silently do nothing useful.
    if _BaseCallbackHandler is object:
        raise ImportError(
            "Neither langchain_core nor langchain is installed.\n"
            "Install one of them with:\n\n"
            "    pip install langchain-core\n"
            "    # or\n"
            "    pip install langchain\n"
        )

    if not (
        hasattr(agent_executor_or_chain, "invoke")
        and hasattr(agent_executor_or_chain, "ainvoke")
    ):
        raise TypeError(
            f"Expected a LangChain chain or AgentExecutor with .invoke and "
            f".ainvoke methods, got {type(agent_executor_or_chain).__name__!r}."
        )

    # Derive a sensible agent label from the wrapped object's class name.
    agent_name = type(agent_executor_or_chain).__name__

    return GovernedChain(
        chain=agent_executor_or_chain,
        chain_name=chain_name,
        metadata=metadata or {},
        agent_name=agent_name,
    )


# ---------------------------------------------------------------------------
# Governed chain wrapper
# ---------------------------------------------------------------------------

class GovernedChain:
    """
    Drop-in replacement for a LangChain chain / ``AgentExecutor`` that wraps
    every invocation in an aag governance chain.

    Do not instantiate directly — use :func:`govern`.
    """

    def __init__(
        self,
        chain: Any,
        chain_name: str,
        metadata: dict,
        agent_name: str,
    ) -> None:
        self._chain = chain
        self._chain_name = chain_name
        self._chain_metadata = metadata
        self._agent_name = agent_name

    # ------------------------------------------------------------------
    # Async invocation (preferred)
    # ------------------------------------------------------------------

    async def ainvoke(
        self,
        input: Any,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Async-invoke the governed chain.

        Opens an aag chain, injects the governance callback, runs the
        original chain, then closes the aag chain.  Returns the chain's
        output unchanged.

        Parameters
        ----------
        input :
            The input passed to the underlying chain's ``ainvoke``.
            Typically a ``dict`` (e.g. ``{"input": "..."}`` for
            ``AgentExecutor``) or a plain string for simple chains.
        config : dict, optional
            LangChain ``RunnableConfig``.  Any existing callbacks are
            preserved — the aag callback is appended, not replaced.

        Raises
        ------
        RuntimeError
            If ``aag.init()`` has not been called.
        ActionDeniedError
            If any tool or LLM call is denied by the aag policy engine.
        """
        # Fail fast with a clear message if the SDK was never initialised.
        _aag_client.get_config()

        async with Chain(self._chain_name, metadata=self._chain_metadata) as aag_chain:
            aag_callback = AagLangChainCallback(
                chain=aag_chain,
                agent_name=self._agent_name,
            )
            merged_config = _merge_config(config, {"callbacks": [aag_callback]})
            return await self._chain.ainvoke(input, config=merged_config, **kwargs)

    # ------------------------------------------------------------------
    # Sync invocation (non-async scripts only)
    # ------------------------------------------------------------------

    def invoke(
        self,
        input: Any,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Synchronously invoke the governed chain.

        Runs :meth:`ainvoke` via ``asyncio.run()``.  Raises ``RuntimeError``
        if called from inside a running event loop — use ``ainvoke`` instead.

        .. note::
            LangChain's sync ``invoke`` does not natively propagate async
            callbacks.  For full governance coverage in sync contexts, use
            ``asyncio.run(governed.ainvoke(...))`` rather than
            ``governed.invoke(...)``.
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "Cannot use GovernedChain.invoke() inside a running async "
                "event loop.  Use 'await governed_chain.ainvoke(...)' instead."
            )
        except RuntimeError as exc:
            if (
                "no running event loop" not in str(exc)
                and "no current event loop" not in str(exc)
            ):
                raise

        return asyncio.run(self.ainvoke(input, config, **kwargs))

    # ------------------------------------------------------------------
    # Pass-through attributes (agent.tools, agent.agent, etc.)
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """
        Proxy unknown attribute lookups to the wrapped chain so that
        ``governed.tools``, ``governed.agent``, ``governed.memory``, etc.
        all continue to work unchanged.
        """
        return getattr(self._chain, name)

    def __repr__(self) -> str:
        return (
            f"GovernedChain(chain_name={self._chain_name!r}, "
            f"agent_name={self._agent_name!r}, "
            f"chain={self._chain!r})"
        )


# ---------------------------------------------------------------------------
# Config merge helper (identical logic to langgraph adapter)
# ---------------------------------------------------------------------------

def _merge_config(base: dict | None, extras: dict) -> dict:
    """
    Return a new config dict that merges *extras* into *base*.

    The ``"callbacks"`` key is handled specially: existing and new callback
    lists are concatenated rather than overwritten, so user-provided
    callbacks coexist with the aag governance callback.
    """
    result: dict = dict(base or {})
    for key, value in extras.items():
        if key == "callbacks" and "callbacks" in result:
            existing = list(result["callbacks"])
            result["callbacks"] = existing + list(value)
        else:
            result[key] = value
    return result
