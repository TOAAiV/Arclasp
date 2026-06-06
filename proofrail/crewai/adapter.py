"""
proofrail.crewai.adapter — Governance wrapper for CrewAI Crew objects.

Quick start
-----------
    import proofrail
    from proofrail.crewai import govern

    proofrail.init(api_key="prail_...")
    governed = govern(crew, chain_name="research-crew")

    # Drop-in replacement — same interface as the original crew:
    result = await governed.kickoff_async(inputs={"topic": "AI safety"})

How it works
------------
1.  ``govern()`` wraps a CrewAI ``Crew`` in a ``GovernedCrew`` instance that
    exposes identical ``.kickoff`` / ``.kickoff_async`` signatures.
2.  On every invocation, a ProofRail ``Chain`` context manager is opened so
    the full crew run appears as a single governed chain in the dashboard.
3.  Instrumentation is applied via one of two strategies, tried in order:

    Strategy A — Native callbacks (CrewAI >= 0.28, preferred)
        If ``crew.task_callback`` exists, the existing value is wrapped so
        that ``ProofRailCrewAICallback.on_task_end_from_output`` fires for
        every completed task while preserving any existing user-provided hook.
        Additionally, if ``crew.before_task_callback`` exists, it is wrapped
        to fire ``on_task_start``.

    Strategy B — Monkey-patch ``Agent.execute_task`` (fallback / start-only supplement)
        For each agent in ``crew.agents``, the instance-level
        ``execute_task`` method is replaced with a wrapper.  Two modes:

        *  ``emit_end=True`` (pure Strategy B, no Strategy A present):
           fires ``on_task_start`` before *and* ``on_task_end`` /
           ``on_task_error`` after the original executes.
        *  ``emit_end=False`` (mixed scenario — Strategy A wraps
           ``task_callback`` but ``before_task_callback`` is absent,
           the default for all CrewAI 1.x crews): fires only
           ``on_task_start``; ``on_task_end`` is left to Strategy A's
           ``task_callback`` wrapper so each task emits exactly one
           ``task_result`` event.  ``on_task_error`` still fires here
           because Strategy A has no error path.

        Because ``execute_task`` is *synchronous* and ``record_agent_action``
        is *async*, the wrapper uses ``asyncio.run_coroutine_threadsafe``
        with the event loop captured at patch-installation time.  This works
        correctly whether CrewAI runs the crew directly in the async context
        or dispatches it to a thread executor.

Cleanup
-------
All patches are removed in a ``finally`` block, guaranteeing that every
agent and crew object is restored to its original state even if an
``ActionDeniedError`` or any other exception terminates the run early.

Known limitations
-----------------
*   When ``kickoff_async`` is unavailable (older CrewAI) the crew is run via
    ``loop.run_in_executor``.  Governance events are recorded synchronously
    (``_fire()`` blocks the executor worker thread while the event loop
    processes each coroutine).  High-concurrency scenarios may see events
    slightly out of order across parallel crews.
"""

from __future__ import annotations

import asyncio
import logging
import concurrent.futures as _cf
from typing import Any

from proofrail import client as _proofrail_client
from proofrail.chain import Chain
from proofrail.crewai.callbacks import ProofRailCrewAICallback
from proofrail.exceptions import (
    ActionDeniedError,
    BackendUnavailableError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ProofRailKillSwitchError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def govern(
    crew: Any,
    chain_name: str = "crewai_workflow",
    metadata: dict | None = None,
) -> "GovernedCrew":
    """
    Wrap a CrewAI ``Crew`` with ProofRail governance.

    Parameters
    ----------
    crew :
        A CrewAI ``Crew`` instance with a ``.kickoff()`` method.
    chain_name : str
        Name recorded in the ProofRail dashboard for each invocation.
        Defaults to ``"crewai_workflow"``.
    metadata : dict, optional
        Extra key/value pairs attached to every chain opened by this wrapper
        (e.g. ``{"crew_version": "1.0", "department": "research"}``).

    Returns
    -------
    GovernedCrew
        A drop-in wrapper exposing ``.kickoff`` and ``.kickoff_async``.

    Raises
    ------
    ImportError
        If ``crewai`` is not installed.
    TypeError
        If the object does not have a ``.kickoff`` method.

    """
    try:
        import crewai  # noqa: F401
    except ImportError:
        raise ImportError(
            "crewai is not installed.  Install it with:\n\n"
            "    pip install crewai\n"
        )

    if not hasattr(crew, "kickoff"):
        raise TypeError(
            f"Expected a CrewAI Crew with a .kickoff method, "
            f"got {type(crew).__name__!r}."
        )

    return GovernedCrew(
        crew=crew,
        chain_name=chain_name,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Governed crew wrapper
# ---------------------------------------------------------------------------

class GovernedCrew:
    """
    Drop-in replacement for a CrewAI ``Crew`` that wraps every invocation
    in a ProofRail governance chain.

    Do not instantiate directly — use :func:`govern`.
    """

    def __init__(self, crew: Any, chain_name: str, metadata: dict) -> None:
        self._crew = crew
        self._chain_name = chain_name
        self._chain_metadata = metadata

    # ------------------------------------------------------------------
    # Async kickoff (preferred)
    # ------------------------------------------------------------------

    async def kickoff_async(self, inputs: dict | None = None, **kwargs: Any) -> Any:
        """
        Async-kickoff the governed crew.

        Opens a ProofRail chain, instruments all agents, runs the crew,
        restores original methods, then closes the chain.  Returns the
        crew's result unchanged.

        Raises
        ------
        RuntimeError
            If ``proofrail.init()`` has not been called.
        ActionDeniedError
            If a task execution is denied by the ProofRail policy engine.
        """
        _proofrail_client.get_config()

        # Capture the running loop now — passed into patches so they can
        # schedule coroutines from synchronous worker threads.
        loop = asyncio.get_running_loop()

        async with Chain(self._chain_name, metadata=self._chain_metadata) as proofrail_chain:
            callback = ProofRailCrewAICallback(proofrail_chain)
            patch_records = _install_instrumentation(self._crew, callback, loop)

            try:
                if hasattr(self._crew, "kickoff_async"):
                    result = await self._crew.kickoff_async(inputs, **kwargs)
                else:
                    # Older CrewAI only has a sync kickoff — run it in a
                    # thread pool so we don't block the event loop.
                    result = await loop.run_in_executor(
                        None,
                        lambda: self._crew.kickoff(inputs),
                    )
            finally:
                _restore_instrumentation(patch_records)

        return result

    # ------------------------------------------------------------------
    # Sync kickoff (non-async scripts only)
    # ------------------------------------------------------------------

    def kickoff(self, inputs: dict | None = None, **kwargs: Any) -> Any:
        """
        Synchronously kickoff the governed crew.

        Runs :meth:`kickoff_async` via ``asyncio.run()``.  Raises
        ``RuntimeError`` if called from inside a running event loop — use
        ``kickoff_async`` instead.
        """
        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "Cannot use GovernedCrew.kickoff() inside a running async "
                "event loop.  Use 'await governed_crew.kickoff_async(...)' instead."
            )
        except RuntimeError as exc:
            if (
                "no running event loop" not in str(exc)
                and "no current event loop" not in str(exc)
            ):
                raise

        return asyncio.run(self.kickoff_async(inputs, **kwargs))

    # ------------------------------------------------------------------
    # Pass-through attributes (crew.agents, crew.tasks, etc.)
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._crew, name)

    def __repr__(self) -> str:
        return (
            f"GovernedCrew(chain_name={self._chain_name!r}, crew={self._crew!r})"
        )


# ---------------------------------------------------------------------------
# Instrumentation — installation
# ---------------------------------------------------------------------------

def _install_instrumentation(
    crew: Any,
    callback: ProofRailCrewAICallback,
    loop: asyncio.AbstractEventLoop,
) -> list:
    """
    Instrument *crew* to fire ProofRail governance events for every task
    execution.

    Tries Strategy A (native CrewAI callbacks) first.  Falls back to
    Strategy B (per-agent ``execute_task`` monkey-patches) if the native
    hooks are not present.

    Returns a list of *patch records* that :func:`_restore_instrumentation`
    uses to undo all changes.
    """
    patch_records: list = []

    # ------------------------------------------------------------------
    # Strategy A — native CrewAI task callbacks
    # ------------------------------------------------------------------
    has_before = hasattr(crew, "before_task_callback")
    has_after = hasattr(crew, "task_callback")

    if has_before or has_after:
        logger.debug("proofrail: using native CrewAI task callbacks (Strategy A)")

        if has_before:
            original_before = crew.before_task_callback

            def _wrapped_before(task: Any, agent: Any) -> None:
                _fire(callback.on_task_start(task, agent), loop)
                if callable(original_before):
                    original_before(task, agent)

            crew.before_task_callback = _wrapped_before
            patch_records.append(("crew_before", crew, original_before))

        if has_after:
            original_after = crew.task_callback

            def _wrapped_after(task_output: Any) -> None:
                _fire(callback.on_task_end_from_output(task_output), loop)
                if callable(original_after):
                    original_after(task_output)

            crew.task_callback = _wrapped_after
            patch_records.append(("crew_after", crew, original_after))

        # Mixed scenario: task_callback present but no before_task_callback
        # (the default for all CrewAI 1.x crews).  Patch execute_task to
        # capture pre-task start events, but set emit_end=False so Strategy B
        # does NOT also fire on_task_end — that slot belongs to Strategy A's
        # task_callback wrapper.  on_task_error remains in Strategy B because
        # Strategy A has no error path.
        if has_after and not has_before:
            logger.debug(
                "proofrail: no before_task_callback — patching execute_task "
                "for pre-task start events only (Strategy A owns on_task_end)"
            )
            _patch_all_agents(crew, callback, loop, patch_records, emit_end=False)

        return patch_records

    # ------------------------------------------------------------------
    # Strategy B — monkey-patch Agent.execute_task
    # ------------------------------------------------------------------
    logger.debug(
        "proofrail: no native CrewAI callbacks found — monkey-patching "
        "execute_task on %d agent(s) (Strategy B)",
        len(getattr(crew, "agents", [])),
    )
    _patch_all_agents(crew, callback, loop, patch_records)
    return patch_records


def _patch_all_agents(
    crew: Any,
    callback: ProofRailCrewAICallback,
    loop: asyncio.AbstractEventLoop,
    patch_records: list,
    emit_end: bool = True,
) -> None:
    """Patch ``execute_task`` on every agent in *crew.agents*.

    ``emit_end`` is forwarded to :func:`_make_patched_execute_task`.  Pass
    ``False`` in the mixed-callback scenario where Strategy A's
    ``task_callback`` wrapper already handles ``on_task_end``.
    """
    for agent in getattr(crew, "agents", []):
        if not hasattr(agent, "execute_task"):
            logger.debug(
                "proofrail: agent %r has no execute_task — skipping",
                getattr(agent, "role", agent),
            )
            continue

        original_method = agent.execute_task
        patched = _make_patched_execute_task(
            original_method, agent, callback, loop, emit_end=emit_end
        )
        # crewai.Agent inherits pydantic.BaseModel; execute_task is a method,
        # not a declared model field.  Direct assignment raises ValueError in
        # Pydantic v2.  object.__setattr__ bypasses Pydantic's __setattr__
        # validation.  (BUG-CR-02)
        object.__setattr__(agent, "execute_task", patched)
        patch_records.append(("agent", agent, original_method))
        logger.debug(
            "proofrail: patched execute_task on agent %r",
            getattr(agent, "role", agent),
        )


def _make_patched_execute_task(
    original_method: Any,
    agent_ref: Any,
    callback: ProofRailCrewAICallback,
    loop: asyncio.AbstractEventLoop,
    emit_end: bool = True,
) -> Any:
    """
    Return a replacement for ``agent.execute_task`` that fires ProofRail
    governance events before (and optionally after) the original synchronous
    method runs.

    When ``emit_end=False``, the wrapper omits ``on_task_end`` on the success
    path — used in the mixed-callback scenario where Strategy A's
    ``task_callback`` wrapper already covers that slot.  ``on_task_error`` is
    always emitted regardless of ``emit_end`` because Strategy A has no error
    path.

    The wrapper uses ``asyncio.run_coroutine_threadsafe`` so that recording
    coroutines are correctly scheduled on *loop* regardless of whether the
    wrapper is called from the event loop thread or a worker thread.
    """

    def patched(*args: Any, **kwargs: Any) -> Any:
        task = args[0] if args else kwargs.get("task")

        _fire(callback.on_task_start(task, agent_ref), loop)

        error: BaseException | None = None
        result: Any = None
        try:
            result = original_method(*args, **kwargs)
        except BaseException as exc:
            error = exc

        if error is not None:
            # Error path: always record and re-raise. The early return here
            # means the emit_end guard below is never reached on error —
            # keep this raise in place so that guarantee holds.
            _fire(callback.on_task_error(task, agent_ref, error), loop)
            raise error  # re-raise after recording

        if emit_end:
            _fire(callback.on_task_end(task, agent_ref, result), loop)
        return result

    return patched


# ---------------------------------------------------------------------------
# Instrumentation — cleanup
# ---------------------------------------------------------------------------

def _restore_instrumentation(patch_records: list) -> None:
    """
    Undo all patches installed by :func:`_install_instrumentation`.

    Called in a ``finally`` block so cleanup always runs, even when the
    crew run is interrupted by ``ActionDeniedError`` or another exception.
    """
    for record in patch_records:
        kind = record[0]
        try:
            if kind == "agent":
                _, agent, original = record
                object.__setattr__(agent, "execute_task", original)
                logger.debug(
                    "proofrail: restored execute_task on agent %r",
                    getattr(agent, "role", agent),
                )
            elif kind == "crew_before":
                _, crew, original = record
                crew.before_task_callback = original
                logger.debug("proofrail: restored crew.before_task_callback")
            elif kind == "crew_after":
                _, crew, original = record
                crew.task_callback = original
                logger.debug("proofrail: restored crew.task_callback")
        except Exception as exc:
            # Log but never raise from cleanup — we must not mask the
            # original exception that triggered the finally block.
            logger.warning("proofrail: error while restoring patch %r: %s", kind, exc)


# ---------------------------------------------------------------------------
# Coroutine scheduling helper
# ---------------------------------------------------------------------------

def _fire(
    coro: Any,
    loop: asyncio.AbstractEventLoop,
    timeout: int = 30,
) -> Any:
    """
    Schedule *coro* on *loop* from a worker thread and block until it
    completes or *timeout* seconds elapse.

    Must be called from a **worker thread** — not from the event-loop thread
    itself.  CrewAI 1.x always dispatches ``kickoff()`` via
    ``asyncio.to_thread``, so this guarantee holds for all production callers.
    Test stubs must also dispatch through a worker thread (e.g. via
    ``asyncio.to_thread``) to avoid deadlocking the event loop.

    Policy exceptions (``ActionDeniedError``, ``ChainTimeoutError``, etc.)
    propagate directly to the caller so that a backend ``deny`` decision halts
    the crew immediately.  A ``BackendUnavailableError`` is raised when the
    governance coroutine does not complete within *timeout* seconds.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except (
        ActionDeniedError,
        ChainTimeoutError,
        ChainAutoPausedError,
        ProofRailKillSwitchError,
    ):
        raise
    except _cf.TimeoutError:
        logger.error("ProofRail callback timed out after %ds", timeout)
        raise BackendUnavailableError("Backend callback timed out") from None
