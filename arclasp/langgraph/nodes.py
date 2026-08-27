"""
Pre-execution LangGraph node governance helpers.

The compiled-graph ``govern()`` wrapper observes LangGraph lifecycle events.
For consequential node bodies, use ``governed_node()`` when adding the node to
the graph so Arclasp policy is resolved before the customer's callable runs.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from arclasp.langgraph.callbacks import _state_to_dict

if TYPE_CHECKING:
    from arclasp.chain import Chain


_CURRENT_CHAIN: contextvars.ContextVar["Chain | None"] = contextvars.ContextVar(
    "arclasp_langgraph_current_chain",
    default=None,
)
_PRE_GOVERNED_NAMES: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "arclasp_langgraph_pre_governed_names",
    default=None,
)
_EXECUTED_PRE_GOVERNED_NAMES: contextvars.ContextVar[
    set[str] | None
] = contextvars.ContextVar(
    "arclasp_langgraph_executed_pre_governed_names",
    default=None,
)


def governed_node(
    fn: Callable[..., Any],
    *,
    name: str,
    action_name: str | None = None,
    action_type: str = "node_execution",
    payload: Callable[..., dict[str, Any]] | dict[str, Any] | None = None,
    parent_agent_name: str | None = None,
) -> Callable[..., Any]:
    """
    Wrap a LangGraph node callable with pre-execution Arclasp governance.

    The returned callable must be supplied to ``StateGraph.add_node()`` before
    ``compile()``. It requires execution inside ``arclasp.langgraph.govern()``
    so the wrapper can use the single Chain opened for the graph run.
    """

    async def _record_before_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        chain = _CURRENT_CHAIN.get()
        if chain is None:
            raise RuntimeError(
                "governed_node() must execute inside arclasp.langgraph.govern(...)."
            )
        event_payload = _resolve_payload(payload, args, kwargs)
        await chain.record_agent_action(
            agent_name=name,
            action_type=action_type,
            action_name=action_name or name,
            payload=event_payload,
            parent_agent_name=parent_agent_name,
        )
        executed = _EXECUTED_PRE_GOVERNED_NAMES.get()
        if executed is not None:
            executed.add(name)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
            await _record_before_call(args, kwargs)
            return await fn(*args, **kwargs)

        wrapped: Callable[..., Any] = _async_wrapped
    else:

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            await _record_before_call(args, kwargs)
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        wrapped = _wrapped

    setattr(wrapped, "__arclasp_langgraph_governed_node_name__", name)
    return wrapped


def _resolve_payload(
    payload: Callable[..., dict[str, Any]] | dict[str, Any] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if callable(payload):
        return dict(payload(*args, **kwargs))
    if payload is not None:
        return dict(payload)
    if args:
        return _state_to_dict(args[0])
    return _state_to_dict(kwargs)


def _set_current_chain(
    chain: "Chain", pre_governed_names: set[str]
) -> tuple[contextvars.Token["Chain | None"], contextvars.Token[set[str] | None], contextvars.Token[set[str] | None]]:
    chain_token = _CURRENT_CHAIN.set(chain)
    names_token = _PRE_GOVERNED_NAMES.set(set(pre_governed_names))
    executed_token = _EXECUTED_PRE_GOVERNED_NAMES.set(set())
    return chain_token, names_token, executed_token


def _reset_current_chain(
    tokens: tuple[contextvars.Token["Chain | None"], contextvars.Token[set[str] | None], contextvars.Token[set[str] | None]]
) -> None:
    chain_token, names_token, executed_token = tokens
    _EXECUTED_PRE_GOVERNED_NAMES.reset(executed_token)
    _PRE_GOVERNED_NAMES.reset(names_token)
    _CURRENT_CHAIN.reset(chain_token)


def _is_pre_governed_node(name: str) -> bool:
    names = _PRE_GOVERNED_NAMES.get()
    executed = _EXECUTED_PRE_GOVERNED_NAMES.get()
    return (names is not None and name in names) or (
        executed is not None and name in executed
    )


def _discover_pre_governed_nodes(graph: Any) -> set[str]:
    found: set[str] = set()

    def visit(obj: Any, depth: int = 0) -> None:
        if obj is None or depth > 5:
            return
        marker = getattr(obj, "__arclasp_langgraph_governed_node_name__", None)
        if isinstance(marker, str):
            found.add(marker)
        for attr in ("func", "afunc", "bound", "runnable"):
            with _suppress_attribute_error():
                visit(getattr(obj, attr), depth + 1)

    builder = getattr(graph, "builder", None)
    for name, spec in getattr(builder, "nodes", {}).items():
        visit(spec)
        runnable = getattr(spec, "runnable", None)
        if getattr(runnable, "__arclasp_langgraph_governed_node_name__", None):
            found.add(name)
    for name, node in getattr(graph, "nodes", {}).items():
        visit(node)
        bound = getattr(node, "bound", None)
        if getattr(bound, "__arclasp_langgraph_governed_node_name__", None):
            found.add(name)
    return found


class _suppress_attribute_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is AttributeError
