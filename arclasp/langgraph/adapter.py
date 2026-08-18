"""
arclasp.langgraph.adapter — Governance wrapper for compiled LangGraph graphs.

Quick start
-----------
    import arclasp
    from arclasp.langgraph import govern

    arclasp.init(api_key="prail_...")
    governed = govern(compiled_graph, chain_name="my-workflow")

    # Drop-in replacement — same interface as the original graph:
    result = await governed.ainvoke({"input": "..."})

How it works
------------
1.  ``govern()`` wraps the compiled graph in a ``GovernedGraph`` instance that
    has identical ``.invoke`` / ``.ainvoke`` signatures.
2.  On every invocation, a Arclasp ``Chain`` context manager is opened so the
    full workflow appears as a single governed chain in the dashboard.
3.  Node-level events are captured via one of two strategies, tried in order:

    Strategy A — ``astream_events`` (LangGraph >= 0.1, preferred)
        ``astream_events(version="v2")`` yields structured events with
        ``metadata["langgraph_node"]`` identifying each node.  We consume the
        stream, fire ``ArclaspLangGraphCallback.on_node_start`` / ``on_node_end``
        for each real node, and collect the graph's final output from the
        root-level ``on_chain_end`` event.

    Strategy B — LangChain ``AsyncCallbackHandler`` (fallback)
        Injects an ``_AsLangChainCallback`` instance via
        ``config={"callbacks": [...]}`` and calls the original
        ``graph.ainvoke``.  LangGraph calls ``on_chain_start`` /
        ``on_chain_end`` for every node; we filter by ``langgraph_node``
        in the event metadata.

Policy enforcement
------------------
``record_agent_action`` is called for every node.  If the Arclasp backend
returns a ``"deny"`` decision, ``ActionDeniedError`` propagates out of
``ainvoke`` / ``invoke`` — the graph execution is halted at that node.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arclasp import client as _arclasp_client
from arclasp._utils import _merge_config
from arclasp.chain import Chain
from arclasp.exceptions import (
    ActionDeniedError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ArclaspKillSwitchError,
)
from arclasp.langgraph.callbacks import (
    ArclaspLangGraphCallback,
    _INTERNAL_NODES,
    _StrategyBPolicyBreak,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def govern(
    compiled_graph: Any,
    chain_name: str = "langgraph_workflow",
    metadata: dict | None = None,
) -> "GovernedGraph":
    """
    Wrap a compiled LangGraph graph with Arclasp governance.

    Parameters
    ----------
    compiled_graph :
        A compiled LangGraph graph — any object with ``.invoke`` and
        ``.ainvoke`` methods (typically the result of
        ``StateGraph.compile()``).
    chain_name : str
        Name recorded in the Arclasp dashboard for each invocation of this
        graph.  Defaults to ``"langgraph_workflow"``.
    metadata : dict, optional
        Extra key/value pairs attached to every chain created by this
        governed graph (e.g. workflow version, team name).

    Returns
    -------
    GovernedGraph
        A drop-in wrapper with the same ``.invoke`` / ``.ainvoke``
        interface as the original graph.

    Raises
    ------
    RuntimeError
        If ``arclasp.init()`` has not been called before ``govern()`` is
        used to invoke the graph.
    ImportError
        If ``langgraph`` is not installed in the current environment.
    """
    try:
        import langgraph  # noqa: F401
    except ImportError:
        raise ImportError(
            "langgraph is not installed.  Install it with:\n\n"
            "    pip install langgraph\n"
        )

    if not (hasattr(compiled_graph, "invoke") and hasattr(compiled_graph, "ainvoke")):
        raise TypeError(
            f"Expected a compiled LangGraph graph with .invoke and .ainvoke "
            f"methods, got {type(compiled_graph).__name__!r}."
        )

    return GovernedGraph(
        graph=compiled_graph,
        chain_name=chain_name,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Governed graph wrapper
# ---------------------------------------------------------------------------


class GovernedGraph:
    """
    Drop-in replacement for a compiled LangGraph graph that wraps every
    invocation in a Arclasp governance chain.

    Do not instantiate directly — use :func:`govern`.
    """

    def __init__(
        self,
        graph: Any,
        chain_name: str,
        metadata: dict,
    ) -> None:
        self._graph = graph
        self._chain_name = chain_name
        self._chain_metadata = metadata

    # ------------------------------------------------------------------
    # Async invocation (preferred)
    # ------------------------------------------------------------------

    async def ainvoke(
        self,
        state: Any,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Async-invoke the governed graph.

        Opens a Arclasp chain, records every node execution, enforces policy
        decisions, then closes the chain.  Returns the graph's final output
        unchanged.

        Raises
        ------
        RuntimeError
            If ``arclasp.init()`` has not been called.
        ActionDeniedError
            If any node's action is denied by the Arclasp policy engine.
        """
        # Fail fast with a clear message if the SDK was never initialised.
        _arclasp_client.get_config()

        async with Chain(
            self._chain_name, metadata=self._chain_metadata
        ) as arclasp_chain:
            callback = ArclaspLangGraphCallback(arclasp_chain)

            if hasattr(self._graph, "astream_events"):
                return await self._ainvoke_via_streaming(
                    arclasp_chain, callback, state, config, **kwargs
                )
            else:
                return await self._ainvoke_via_callbacks(
                    arclasp_chain, callback, state, config, **kwargs
                )

    # ------------------------------------------------------------------
    # Sync invocation (non-async scripts only)
    # ------------------------------------------------------------------

    def invoke(
        self,
        state: Any,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Synchronously invoke the governed graph.

        Runs :meth:`ainvoke` via ``asyncio.run()``.  Raises ``RuntimeError``
        if called from inside a running event loop — use ``ainvoke`` instead.
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "Cannot use GovernedGraph.invoke() inside a running async "
                "event loop.  Use 'await governed_graph.ainvoke(...)' instead."
            )
        except RuntimeError as exc:
            if "no running event loop" not in str(
                exc
            ) and "no current event loop" not in str(exc):
                raise

        return asyncio.run(self.ainvoke(state, config, **kwargs))

    # ------------------------------------------------------------------
    # Strategy A — astream_events (preferred, LangGraph >= 0.1)
    # ------------------------------------------------------------------

    async def _ainvoke_via_streaming(
        self,
        arclasp_chain: Chain,
        callback: ArclaspLangGraphCallback,
        state: Any,
        config: dict | None,
        **kwargs: Any,
    ) -> Any:
        """
        Consume ``astream_events(version='v2')`` to intercept node lifecycle
        events without changing the graph's output.

        Event filtering
        ~~~~~~~~~~~~~~~
        - ``event == "on_chain_start"``  +  ``metadata.langgraph_node`` set
          and not in :data:`_INTERNAL_NODES`  → fire ``on_node_start``
        - ``event == "on_chain_end"``    +  same node guard            → fire ``on_node_end``
        - Root-level ``on_chain_end``    (run_id matches the first seen
          run_id with no parent)         → capture final graph output
        """
        root_run_id: str | None = None
        active_nodes: dict[str, str] = {}  # run_id → node_name (active; popped on end)
        all_nodes: dict[
            str, str
        ] = {}  # run_id → node_name (never evicted; parent lookup)
        active_node_parents: dict[str, str | None] = {}  # run_id → parent_agent_name
        final_output: Any = None

        stream = self._graph.astream_events(
            state, config=config, version="v2", **kwargs
        )
        _policy_exc: BaseException | None = None
        # Catch policy exceptions INSIDE the loop body and break so that
        # `async for` invokes stream.aclose() through its normal break path.
        # Any HTTPStatusError(409) raised by LangGraph's cleanup during that
        # aclose() call is caught and discarded by the outer except below,
        # preserving the original policy exception for the caller.
        try:
            async for event in stream:
                event_type: str = event.get("event", "")
                run_id: str = str(event.get("run_id", ""))
                metadata: dict = event.get("metadata") or {}
                node_name: str = metadata.get("langgraph_node", "")

                # Track the root run (first event seen)
                if root_run_id is None:
                    root_run_id = run_id

                try:
                    # --- Node start ---
                    if (
                        event_type == "on_chain_start"
                        and node_name
                        and node_name not in _INTERNAL_NODES
                    ):
                        parent_agent_name: str | None = None
                        for pid in event.get("parent_ids") or []:
                            parent = all_nodes.get(str(pid))
                            if parent:
                                parent_agent_name = parent
                                break
                        active_nodes[run_id] = node_name
                        all_nodes[run_id] = node_name
                        active_node_parents[run_id] = parent_agent_name
                        input_state = (event.get("data") or {}).get("input")
                        await callback.on_node_start(
                            node_name, input_state, parent_agent_name=parent_agent_name
                        )

                    # --- Node end ---
                    elif event_type == "on_chain_end" and run_id in active_nodes:
                        finished_node = active_nodes.pop(run_id)
                        par = active_node_parents.pop(run_id, None)
                        output_state = (event.get("data") or {}).get("output")
                        await callback.on_node_end(
                            finished_node, output_state, parent_agent_name=par
                        )

                    # --- Node error ---
                    elif event_type == "on_chain_error" and run_id in active_nodes:
                        finished_node = active_nodes.pop(run_id)
                        par = active_node_parents.pop(run_id, None)
                        error = (event.get("data") or {}).get("error")
                        await callback.on_node_end(
                            finished_node, None, error=error, parent_agent_name=par
                        )

                except (
                    ChainTimeoutError,
                    ActionDeniedError,
                    ChainAutoPausedError,
                    ArclaspKillSwitchError,
                ) as exc:
                    _policy_exc = exc
                    break  # triggers aclose(); cleanup exc caught by outer except

                # --- Capture final graph output (root chain end) ---
                if event_type == "on_chain_end" and run_id == root_run_id:
                    final_output = (event.get("data") or {}).get("output")

        except Exception as _cleanup_exc:
            if _policy_exc is not None:
                # aclose() raised during break cleanup — discard so it doesn't
                # replace the original policy exception.
                logger.debug(
                    "Strategy A: stream cleanup raised %s after %s — discarding cleanup error",
                    type(_cleanup_exc).__name__,
                    type(_policy_exc).__name__,
                )
            else:
                raise

        if _policy_exc is not None:
            raise _policy_exc

        return final_output

    # ------------------------------------------------------------------
    # Strategy B — LangChain AsyncCallbackHandler (fallback)
    # ------------------------------------------------------------------

    async def _ainvoke_via_callbacks(
        self,
        arclasp_chain: Chain,
        callback: ArclaspLangGraphCallback,
        state: Any,
        config: dict | None,
        **kwargs: Any,
    ) -> Any:
        """
        Inject an ``AsyncCallbackHandler`` via the LangGraph ``config`` dict
        and call the original ``graph.ainvoke``.

        LangGraph routes node execution through LangChain's callback system;
        ``_AsLangChainCallback`` listens for ``on_chain_start`` /
        ``on_chain_end`` events that carry ``metadata["langgraph_node"]`` and
        delegates them to the :class:`ArclaspLangGraphCallback`.

        Falls back to plain ``ainvoke`` with no node tracking if
        ``langchain_core`` is not available, logging a warning.
        """
        try:
            lc_callback = callback.as_langchain_callback()
        except ImportError as exc:
            logger.warning(
                "arclasp: langchain_core not available (%s). "
                "Node-level events will not be recorded — only the chain "
                "open/close will be tracked.  Install langchain-core or use "
                "a LangGraph version that supports astream_events.",
                exc,
            )
            return await self._graph.ainvoke(state, config=config, **kwargs)

        merged = _merge_config(config, {"callbacks": [lc_callback]})
        try:
            return await self._graph.ainvoke(state, config=merged, **kwargs)
        except _StrategyBPolicyBreak as wrapper:
            raise wrapper.original from None

    # ------------------------------------------------------------------
    # Pass-through attributes (allow governed_graph.get_graph() etc.)
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    def __repr__(self) -> str:
        return f"GovernedGraph(chain_name={self._chain_name!r}, graph={self._graph!r})"
