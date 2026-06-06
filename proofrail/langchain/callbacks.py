"""
proofrail.langchain.callbacks — LangChain BaseCallbackHandler that records
tool calls and LLM calls as ProofRail governance chain events.

This module loads ``langchain_core.callbacks.BaseCallbackHandler`` (or falls
back to ``langchain.callbacks.BaseCallbackHandler`` for older installs) at
*import time* using a safe loader that substitutes ``object`` when neither
package is available.  This means ``import proofrail.langchain`` never raises
``ImportError`` — the error is deferred until an attempt is made to inject
the callback into a real LangChain object.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from proofrail.chain import Chain

from proofrail.exceptions import (
    ActionDeniedError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ProofRailKillSwitchError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy base-class loader
# ---------------------------------------------------------------------------

def _load_base_handler() -> type:
    """
    Attempt to import ``BaseCallbackHandler`` from langchain_core first,
    then from the legacy langchain package.

    Returns ``object`` as a no-op base if neither is installed, so the module
    loads cleanly even without LangChain present.  The adapter raises a
    descriptive ``ImportError`` before any governed invocation is attempted.
    """
    for module_path in (
        "langchain_core.callbacks.base",
        "langchain.callbacks.base",
    ):
        try:
            mod = importlib.import_module(module_path)
            handler = getattr(mod, "BaseCallbackHandler", None)
            if handler is not None:
                logger.debug("proofrail: loaded BaseCallbackHandler from %s", module_path)
                return handler
        except ImportError:
            continue

    logger.debug(
        "proofrail: langchain / langchain_core not found — "
        "ProofRailLangChainCallback will use object as base until LangChain is installed"
    )
    return object


_BaseCallbackHandler: type = _load_base_handler()


class _StrategyBPolicyBreak(BaseException):
    """
    Internal control-flow signal that bypasses LangChain's callback manager
    ``except Exception`` clause.  Raised inside callback methods when a policy
    decision fires; caught and unwrapped in the adapter so the original policy
    exception reaches the SDK caller.

    Inherits BaseException (NOT Exception) — comparable to CancelledError.
    Not exported; do not reference outside this package.
    """

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(f"Strategy B policy break: {type(original).__name__}")


# ---------------------------------------------------------------------------
# Output extraction helpers (duck-typed — no LangChain type imports)
# ---------------------------------------------------------------------------

def _extract_llm_output(response: Any) -> str:
    """
    Pull the first generated text out of a ``LLMResult`` without importing
    any LangChain types.

    Handles both chat (``AIMessage.content``) and completion
    (``Generation.text``) responses.  Falls back to ``str(response)`` if
    the expected attributes are absent.
    """
    try:
        generations = getattr(response, "generations", None)
        if generations and generations[0]:
            first = generations[0][0]
            # Completion-style Generation
            if hasattr(first, "text") and first.text:
                return str(first.text)[:500]
            # Chat-style ChatGeneration → message.content
            msg = getattr(first, "message", None)
            if msg is not None:
                content = getattr(msg, "content", "")
                return str(content)[:500]
    except Exception:
        pass
    return str(response)[:500]


def _serialized_name(serialized: dict[str, Any] | None, fallback: str) -> str:
    """Extract a human-readable name from a LangChain ``serialized`` dict."""
    if not serialized:
        return fallback
    # LangChain puts the name at the top level; some versions nest it under id
    name = serialized.get("name") or serialized.get("id", [fallback])[-1]
    return str(name) if name else fallback


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

class ProofRailLangChainCallback(_BaseCallbackHandler):  # type: ignore[misc]
    """
    LangChain ``BaseCallbackHandler`` that records tool calls and LLM calls
    as ProofRail governance chain events.

    Pass an instance of this class to any LangChain chain or
    ``AgentExecutor`` via ``config={"callbacks": [callback]}``.

    Parameters
    ----------
    chain : proofrail.chain.Chain
        The active ProofRail chain context (obtained from inside an
        ``async with Chain(...) as chain:`` block).
    agent_name : str
        Label used as ``agent_name`` in every recorded event.  Defaults to
        ``"langchain_agent"`` but should be set to something meaningful
        (e.g. the executor class name or the agent's role).
    """

    def __init__(self, chain: "Chain", agent_name: str = "langchain_agent") -> None:
        # Call super().__init__() only when we have a real BaseCallbackHandler
        # base — object.__init__ doesn't accept extra args.
        if _BaseCallbackHandler is not object:
            super().__init__()

        self._chain = chain
        self._agent_name = agent_name

        # run_id → name maps for correlating end events back to their starts
        self._active_tools: dict[UUID, str] = {}
        self._active_llms: dict[UUID, str] = {}

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Fired when a LangChain tool begins execution.

        Records an event with ``action_type="tool_call"`` and the tool's
        input as the payload.
        """
        tool_name = _serialized_name(serialized, fallback="unknown_tool")
        self._active_tools[run_id] = tool_name

        logger.debug("LangChain tool starting: %s (run_id=%s)", tool_name, run_id)

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="tool_call",
                action_name=tool_name,
                payload={
                    "tool_input": str(input_str)[:1000],
                    "tags": tags or [],
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    async def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fired when a LangChain tool returns successfully.

        Records an event with ``action_type="tool_result"`` and the tool's
        output as the payload.
        """
        tool_name = self._active_tools.pop(run_id, "unknown_tool")

        logger.debug("LangChain tool completed: %s (run_id=%s)", tool_name, run_id)

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="tool_result",
                action_name=f"{tool_name}:result",
                payload={
                    "tool_output": str(output)[:1000],
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    async def on_tool_error(
        self,
        error: BaseException | str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fired when a LangChain tool raises an exception.

        Records an event with ``action_type="tool_error"`` and structured
        error details as the payload.
        """
        tool_name = self._active_tools.pop(run_id, "unknown_tool")

        logger.debug(
            "LangChain tool errored: %s — %s (run_id=%s)", tool_name, error, run_id
        )

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="tool_error",
                action_name=f"{tool_name}:error",
                payload={
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    # ------------------------------------------------------------------
    # LLM callbacks
    # ------------------------------------------------------------------

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Fired when an LLM is invoked.

        Records an event with ``action_type="llm_call"``.  Prompt text is
        truncated to 500 characters per prompt to stay within payload limits.
        """
        model_name = _serialized_name(serialized, fallback="unknown_llm")
        self._active_llms[run_id] = model_name

        logger.debug("LangChain LLM starting: %s (run_id=%s)", model_name, run_id)

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="llm_call",
                action_name=model_name,
                payload={
                    "prompt_count": len(prompts),
                    "prompts": [p[:500] for p in (prompts or [])],
                    "tags": tags or [],
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fired when an LLM call completes.

        Records an event with ``action_type="llm_result"``.  The first
        generated text is extracted from the ``LLMResult`` via duck-typing
        so that no LangChain types need to be imported.
        """
        model_name = self._active_llms.pop(run_id, "unknown_llm")

        logger.debug("LangChain LLM completed: %s (run_id=%s)", model_name, run_id)

        output_text = _extract_llm_output(response)

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="llm_result",
                action_name=f"{model_name}:result",
                payload={
                    "output": output_text,
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc

    async def on_llm_error(
        self,
        error: BaseException | str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """
        Fired when an LLM call raises an exception.

        Records an event with ``action_type="llm_error"``.
        """
        model_name = self._active_llms.pop(run_id, "unknown_llm")

        logger.debug(
            "LangChain LLM errored: %s — %s (run_id=%s)", model_name, error, run_id
        )

        try:
            await self._chain.record_agent_action(
                agent_name=self._agent_name,
                action_type="llm_error",
                action_name=f"{model_name}:error",
                payload={
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                },
            )
        except (ActionDeniedError, ChainTimeoutError, ChainAutoPausedError, ProofRailKillSwitchError) as exc:
            raise _StrategyBPolicyBreak(exc) from exc
