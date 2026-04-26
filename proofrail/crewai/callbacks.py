"""
proofrail.crewai.callbacks — CrewAI task and agent event handler.

Records task start and end events as ProofRail governance chain events.
Designed to be called from both monkey-patched synchronous ``execute_task``
wrappers (via ``asyncio.run_coroutine_threadsafe``) and from native CrewAI
callback hooks where available.

This module has no top-level imports from ``crewai`` so that
``import proofrail.crewai`` never raises ``ImportError`` when CrewAI is not
installed — the error is deferred to the first governed invocation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proofrail.chain import Chain

logger = logging.getLogger(__name__)

# Maximum characters used for ``action_name`` (task description truncated).
_ACTION_NAME_MAX = 100

# Maximum characters included in the output payload field.
_OUTPUT_MAX = 1000


# ---------------------------------------------------------------------------
# Attribute extraction helpers — all duck-typed, no crewai imports
# ---------------------------------------------------------------------------

def _task_description(task: Any) -> str:
    """Return the task's description string, falling back gracefully."""
    return str(getattr(task, "description", task))


def _task_expected_output(task: Any) -> str:
    """Return the task's expected_output field if present."""
    return str(getattr(task, "expected_output", ""))


def _agent_role(agent: Any) -> str:
    """Return the agent's role label, falling back to the class name."""
    return str(getattr(agent, "role", type(agent).__name__))


def _extract_task_output(task_output: Any) -> dict:
    """
    Extract a plain dict from a CrewAI ``TaskOutput`` object without
    importing CrewAI types.

    Handles both newer CrewAI (has ``.raw``, ``.summary``, ``.agent``)
    and older versions where the output may be a plain string.
    """
    if isinstance(task_output, str):
        return {"output": task_output[:_OUTPUT_MAX]}

    result: dict = {}

    raw = getattr(task_output, "raw", None)
    if raw is not None:
        result["output"] = str(raw)[:_OUTPUT_MAX]

    summary = getattr(task_output, "summary", None)
    if summary is not None:
        result["summary"] = str(summary)[:500]

    agent_label = getattr(task_output, "agent", None)
    if agent_label is not None:
        result["agent"] = str(agent_label)

    if not result:
        result["output"] = str(task_output)[:_OUTPUT_MAX]

    return result


# ---------------------------------------------------------------------------
# Callback class
# ---------------------------------------------------------------------------

class ProofRailCrewAICallback:
    """
    Handles CrewAI task and agent lifecycle events, recording each as a
    ProofRail governance chain event via ``chain.record_agent_action``.

    Parameters
    ----------
    chain : proofrail.chain.Chain
        The active ProofRail chain context (obtained from inside an
        ``async with Chain(...) as chain:`` block in the adapter).

    Methods
    -------
    on_task_start(task, agent)
        Called before a task is executed.  Records
        ``action_type="task_execution"``.
    on_task_end(task, agent, output)
        Called after a task completes successfully.  Records
        ``action_type="task_result"``.
    on_task_end_from_output(task_output)
        Variant of ``on_task_end`` that accepts a native CrewAI
        ``TaskOutput`` object — used when the native ``task_callback``
        hook is available.
    on_task_error(task, agent, error)
        Called when a task raises an exception.  Records
        ``action_type="task_error"``.
    """

    def __init__(self, chain: "Chain") -> None:
        self._chain = chain

    # ------------------------------------------------------------------
    # Pre-task event
    # ------------------------------------------------------------------

    async def on_task_start(self, task: Any, agent: Any) -> None:
        """
        Record the start of a CrewAI task execution as a ProofRail chain event.

        Parameters
        ----------
        task :
            The CrewAI ``Task`` object being executed.
        agent :
            The CrewAI ``Agent`` executing the task.
        """
        description = _task_description(task)
        agent_name = _agent_role(agent)
        action_name = description[:_ACTION_NAME_MAX]

        logger.debug("CrewAI task starting: %r (agent=%s)", action_name, agent_name)

        await self._chain.record_agent_action(
            agent_name=agent_name,
            action_type="task_execution",
            action_name=action_name,
            payload={
                "task": description,
                "expected_output": _task_expected_output(task),
            },
        )

    # ------------------------------------------------------------------
    # Post-task events
    # ------------------------------------------------------------------

    async def on_task_end(self, task: Any, agent: Any, output: Any) -> None:
        """
        Record the successful completion of a CrewAI task.

        Parameters
        ----------
        task :
            The CrewAI ``Task`` object that was executed.
        agent :
            The CrewAI ``Agent`` that executed the task.
        output :
            The raw return value from ``execute_task`` — typically a string
            containing the agent's response.
        """
        description = _task_description(task)
        agent_name = _agent_role(agent)
        action_name = f"{description[:_ACTION_NAME_MAX - 7]}:result"

        logger.debug("CrewAI task completed: %r (agent=%s)", description[:60], agent_name)

        await self._chain.record_agent_action(
            agent_name=agent_name,
            action_type="task_result",
            action_name=action_name,
            payload={
                "task": description,
                "output": str(output)[:_OUTPUT_MAX],
            },
        )

    async def on_task_end_from_output(
        self,
        task_output: Any,
        task: Any | None = None,
    ) -> None:
        """
        Variant of ``on_task_end`` that accepts a native CrewAI
        ``TaskOutput`` object directly.

        Used when the native ``crew.task_callback`` hook is available
        (CrewAI >= 0.28).  ``task_output.agent`` provides the role label;
        ``task`` is optional additional context.
        """
        agent_name = str(getattr(task_output, "agent", "unknown_agent"))
        description = (
            _task_description(task)
            if task is not None
            else str(getattr(task_output, "description", "task"))
        )
        action_name = f"{description[:_ACTION_NAME_MAX - 7]}:result"

        logger.debug(
            "CrewAI task_callback fired: %r (agent=%s)", description[:60], agent_name
        )

        await self._chain.record_agent_action(
            agent_name=agent_name,
            action_type="task_result",
            action_name=action_name,
            payload=_extract_task_output(task_output),
        )

    async def on_task_error(
        self, task: Any, agent: Any, error: BaseException
    ) -> None:
        """
        Record a task that raised an exception.

        Parameters
        ----------
        task :
            The CrewAI ``Task`` that failed.
        agent :
            The CrewAI ``Agent`` that was executing it.
        error :
            The exception that was raised.
        """
        description = _task_description(task)
        agent_name = _agent_role(agent)
        action_name = f"{description[:_ACTION_NAME_MAX - 6]}:error"

        logger.debug(
            "CrewAI task errored: %r (agent=%s) — %s",
            description[:60],
            agent_name,
            error,
        )

        await self._chain.record_agent_action(
            agent_name=agent_name,
            action_type="task_error",
            action_name=action_name,
            payload={
                "task": description,
                "error_type": type(error).__name__,
                "error_message": str(error)[:500],
            },
        )
