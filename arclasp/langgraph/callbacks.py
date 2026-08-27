"""
arclasp.langgraph.callbacks — LangGraph-compatible callback handler.

Records LangGraph node start events as Arclasp chain events.

This module intentionally does NOT import langchain_core at module level so
that ``arclasp`` itself can be imported without langgraph/langchain installed.
The ``_AsLangChainCallback`` inner class is constructed lazily and only when
the adapter detects that langchain_core is available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from arclasp._constants import _ACTION_NAME_MAX
from arclasp._utils import sanitize_log_field
from arclasp.exceptions import (
    ActionDeniedError,
    BackendUnavailableError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ArclaspKillSwitchError,
)

if TYPE_CHECKING:
    from arclasp.chain import Chain

logger = logging.getLogger(__name__)

# Internal node names that LangGraph adds automatically — we skip them so
# only real user-defined nodes are recorded as governance events.
_INTERNAL_NODES: frozenset[str] = frozenset(
    {"__start__", "__end__", "_start", "_end", ""}
)


class _StrategyBPolicyBreak(BaseException):
    """
    Internal control-flow signal that bypasses LangGraph 1.x's callback manager
    ``except Exception`` clause.  Raised inside ``_AsLangChainCallback`` when a
    policy decision fires; caught and unwrapped in the adapter so the original
    policy exception reaches the SDK caller.

    Inherits BaseException (NOT Exception) — comparable to CancelledError.
    Not exported; do not reference outside this package.
    """

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(f"Strategy B policy break: {type(original).__name__}")


# ---------------------------------------------------------------------------
# State → dict conversion helper
# ---------------------------------------------------------------------------


def _state_to_dict(state: Any) -> dict:
    """
    Convert a LangGraph state value to a plain ``dict`` suitable for the
    Arclasp payload.  Handles TypedDict, Pydantic v1/v2 models, NamedTuples,
    and plain dicts gracefully.

    When the input is already a plain dict, each value is recursively
    normalized via ``_safe_value`` so non-JSON-serializable objects nested
    inside (e.g. LangChain message objects in MessagesState) don't break
    downstream JSON encoding of the payload.
    """
    if state is None:
        return {}
    if hasattr(state, "model_dump"):  # Pydantic v2
        return state.model_dump()
    if hasattr(state, "dict"):  # Pydantic v1
        return state.dict()
    if hasattr(state, "_asdict"):  # NamedTuple
        return state._asdict()
    if isinstance(state, dict):
        # Recursively convert values — the dict may contain non-serializable
        # objects (e.g. LangChain HumanMessage objects in MessagesState).
        return {k: _safe_value(v) for k, v in state.items()}
    try:
        return dict(state)
    except (TypeError, ValueError):
        return {"state_repr": str(state)[:500]}


def _safe_value(v: Any) -> Any:
    """Convert a single value to a JSON-safe type."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {k: _safe_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe_value(item) for item in v]
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump()
        except Exception:
            return str(v)[:500]
    if hasattr(v, "dict"):
        try:
            return v.dict()
        except Exception:
            return str(v)[:500]
    return str(v)[:500]


# ---------------------------------------------------------------------------
# Primary callback class — clean public interface
# ---------------------------------------------------------------------------


class ArclaspLangGraphCallback:
    """
    Callback handler that records LangGraph node starts as Arclasp governance
    chain events.

    LangGraph completion/error callbacks are intentionally treated as local
    lifecycle observations in this SDK release. The backend event API evaluates
    every submitted event as a governed action, so result/error telemetry is not
    submitted through ``record_agent_action()`` where it could re-gate an
    already-authorized business action merely because cumulative chain metrics
    remain above a threshold.

    This class is framework-agnostic.  The :class:`_AsLangChainCallback`
    below bridges it to LangChain's ``AsyncCallbackHandler`` interface so it
    can be injected into LangGraph via ``config={"callbacks": [...]}``.
    """

    def __init__(self, chain: "Chain") -> None:
        self._chain = chain

    async def on_node_start(
        self, node_name: str, input_state: Any, *, parent_agent_name: str | None = None
    ) -> None:
        logger.debug("LangGraph node starting: %s", sanitize_log_field(node_name))
        await self._chain.record_agent_action(
            agent_name=node_name,
            action_type="node_execution",
            action_name=node_name,
            payload=_state_to_dict(input_state),
            parent_agent_name=parent_agent_name,
        )

    async def on_node_end(
        self,
        node_name: str,
        output_state: Any,
        error: BaseException | None = None,
        *,
        parent_agent_name: str | None = None,
    ) -> None:
        """
        Observe completion (or failure) of a node execution locally.

        The current backend has no observational-only event endpoint. Submitting
        node result/error telemetry via ``record_agent_action()`` would make it
        a second governed business action, so this callback logs the lifecycle
        observation without persisting a separate ChainEvent.
        """
        if error is not None:
            logger.debug(
                "LangGraph node errored: %s — %s", sanitize_log_field(node_name), error
            )
        else:
            logger.debug("LangGraph node completed: %s", sanitize_log_field(node_name))

    # ------------------------------------------------------------------
    # LangChain bridge — constructed on demand
    # ------------------------------------------------------------------

    def as_langchain_callback(self) -> "_AsLangChainCallback":
        """
        Return a ``langchain_core.callbacks.AsyncCallbackHandler`` wrapper
        that delegates node-level events to this instance.

        Raises ``ImportError`` if ``langchain_core`` is not installed.
        """
        return _AsLangChainCallback(self)


# ---------------------------------------------------------------------------
# LangChain AsyncCallbackHandler bridge
# ---------------------------------------------------------------------------


def _build_langchain_base():
    """
    Lazily import ``langchain_core.callbacks.AsyncCallbackHandler``.
    Returns the class, or raises ``ImportError`` with install instructions.
    """
    try:
        from langchain_core.callbacks.base import AsyncCallbackHandler  # type: ignore[import]

        return AsyncCallbackHandler
    except ImportError:
        raise ImportError(
            "langchain_core is required to use the LangChain callback bridge.\n"
            "It is normally installed as a dependency of langgraph.  If it is\n"
            "missing, run:\n\n"
            "    pip install langchain-core\n"
        )


class _AsLangChainCallback:
    """
    Thin adapter that implements ``AsyncCallbackHandler`` and delegates
    LangGraph node events to a :class:`ArclaspLangGraphCallback`.

    Constructed via ``ArclaspLangGraphCallback.as_langchain_callback()``.

    LangGraph maps node execution to ``on_chain_start`` / ``on_chain_end``
    callback calls.  The actual node name appears in the *metadata* dict
    under the key ``"langgraph_node"`` (LangGraph >= 0.1).
    """

    def __init__(self, arclasp_callback: ArclaspLangGraphCallback) -> None:
        # Inherit from AsyncCallbackHandler at instantiation time so we
        # don't import langchain_core at module level.
        base = _build_langchain_base()

        # Dynamically create a concrete subclass that holds our arclasp_callback
        # reference and overrides the relevant handler methods.
        cls = type(
            "_ArclaspNodeEventHandler",
            (base,),
            {
                "__init__": lambda self_inner, cb: setattr(
                    self_inner, "_arclasp", cb
                ),
                "on_chain_start": _make_on_chain_start(),
                "on_chain_end": _make_on_chain_end(),
                "on_chain_error": _make_on_chain_error(),
            },
        )
        self._handler = cls(arclasp_callback)  # type: ignore[call-arg]  # Dynamic type() build — runtime-correct, pyright can't infer signature

    def __getattr__(self, name: str) -> Any:
        # Proxy everything to the dynamically built handler.
        return getattr(self._handler, name)


def _make_on_chain_start():
    async def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node_name = (metadata or {}).get("langgraph_node", "")
        if not node_name or node_name in _INTERNAL_NODES:
            return
        node_name = node_name[: _ACTION_NAME_MAX - 7]
        # Track run_id → node_name so on_chain_end can look it up
        if not hasattr(self, "_node_runs"):
            self._node_runs = {}
        # _all_nodes is never evicted — used for parent resolution after a node ends
        if not hasattr(self, "_all_nodes"):
            self._all_nodes = {}
        parent_agent_name: str | None = None
        if parent_run_id is not None:
            parent_node = self._all_nodes.get(parent_run_id)
            if parent_node and parent_node not in _INTERNAL_NODES:
                parent_agent_name = parent_node
        self._node_runs[run_id] = node_name
        self._all_nodes[run_id] = node_name
        if not hasattr(self, "_node_parents"):
            self._node_parents = {}
        self._node_parents[run_id] = parent_agent_name
        try:
            await self._arclasp.on_node_start(
                node_name, inputs, parent_agent_name=parent_agent_name
            )
        except (
            ActionDeniedError,
            BackendUnavailableError,
            ChainTimeoutError,
            ChainAutoPausedError,
            ArclaspKillSwitchError,
        ) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    return on_chain_start


def _make_on_chain_end():
    async def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        node_name = getattr(self, "_node_runs", {}).pop(run_id, None)
        if not node_name:
            return
        parent_agent_name = getattr(self, "_node_parents", {}).pop(run_id, None)
        try:
            await self._arclasp.on_node_end(
                node_name, outputs, error=None, parent_agent_name=parent_agent_name
            )
        except (
            ActionDeniedError,
            BackendUnavailableError,
            ChainTimeoutError,
            ChainAutoPausedError,
            ArclaspKillSwitchError,
        ) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    return on_chain_end


def _make_on_chain_error():
    async def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        node_name = getattr(self, "_node_runs", {}).pop(run_id, None)
        if not node_name:
            return
        parent_agent_name = getattr(self, "_node_parents", {}).pop(run_id, None)
        try:
            await self._arclasp.on_node_end(
                node_name, None, error=error, parent_agent_name=parent_agent_name
            )
        except (
            ActionDeniedError,
            BackendUnavailableError,
            ChainTimeoutError,
            ChainAutoPausedError,
            ArclaspKillSwitchError,
        ) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    return on_chain_error
