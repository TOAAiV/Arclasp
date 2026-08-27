from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import arclasp
from arclasp.exceptions import ActionDeniedError, BackendUnavailableError


pytest.importorskip("langgraph")


ALLOW = {
    "policy_decision": "allow",
    "decision_reason": "Allowed",
    "decision_source": "backend_evaluation",
}


def _real_langgraph_graph():
    langgraph_mod = sys.modules.get("langgraph")
    if langgraph_mod is not None and not hasattr(langgraph_mod, "__path__"):
        sys.modules.pop("langgraph", None)
    return pytest.importorskip("langgraph.graph")


def _build_single_executor_graph(side_effects: list[str]):
    from arclasp.langgraph import govern, governed_node
    from typing_extensions import TypedDict

    graph_mod = _real_langgraph_graph()
    END = graph_mod.END
    StateGraph = graph_mod.StateGraph

    class State(TypedDict, total=False):
        amount_usd: float
        executed: bool

    def executor(state: State) -> State:
        side_effects.append("executor-ran")
        return {"executed": True}

    graph = StateGraph(State)
    graph.add_node("Executor", governed_node(executor, name="Executor"))
    graph.set_entry_point("Executor")
    graph.add_edge("Executor", END)
    return govern(graph.compile(), chain_name="langgraph-pre-exec")


def _init() -> None:
    arclasp.init(
        api_key="prail_test",
        backend_url="http://localhost:9999",
        environment="development",
        default_approval_timeout_hours=0,
    )


@pytest.mark.asyncio
async def test_governed_node_blocks_body_while_approval_is_pending():
    side_effects: list[str] = []
    _init()

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-pending"}
        if path.endswith("/complete"):
            return {"id": "chain-langgraph-pending", "status": "completed"}
        if body.get("action_name") == "Executor":
            return {
                "policy_decision": "require_approval",
                "decision_reason": "Approval required before executor",
                "decision_source": "backend_evaluation",
                "policy_name": "financial_approval",
            }
        return ALLOW

    governed = _build_single_executor_graph(side_effects)
    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(arclasp.ChainTimeoutError):
            await governed.ainvoke({"amount_usd": 12000.0})

    assert side_effects == []


@pytest.mark.asyncio
async def test_governed_node_executes_once_after_approval():
    side_effects: list[str] = []
    _init()
    event_calls: list[dict] = []

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-approved"}
        if path.endswith("/complete"):
            return {"id": "chain-langgraph-approved", "status": "completed"}
        event_calls.append(body)
        if body.get("action_name") == "Executor":
            return {
                "policy_decision": "require_approval",
                "decision_reason": "Approval required before executor",
                "decision_source": "backend_evaluation",
                "policy_name": "financial_approval",
            }
        return ALLOW

    async def approved(_self):
        return "approved in test"

    governed = _build_single_executor_graph(side_effects)
    with (
        patch("arclasp.client._post", side_effect=mock_post),
        patch("arclasp.chain.Chain._poll_for_approval", approved),
    ):
        result = await governed.ainvoke({"amount_usd": 12000.0})

    assert result["executed"] is True
    assert side_effects == ["executor-ran"]
    assert [call["action_name"] for call in event_calls] == ["Executor"]


@pytest.mark.asyncio
async def test_governed_node_denial_prevents_body_execution():
    side_effects: list[str] = []
    _init()

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-denied"}
        if path.endswith("/complete"):
            return {"id": "chain-langgraph-denied", "status": "completed"}
        if body.get("action_name") == "Executor":
            return {
                "policy_decision": "require_approval",
                "decision_reason": "Approval required before executor",
                "decision_source": "backend_evaluation",
                "policy_name": "financial_approval",
            }
        return ALLOW

    async def denied(_self):
        raise ActionDeniedError(
            message="Approval denied",
            policy_name="human_approval_denied",
            condition="denied in test",
            chain_context={},
            decision_source="human_approval",
        )

    governed = _build_single_executor_graph(side_effects)
    with (
        patch("arclasp.client._post", side_effect=mock_post),
        patch("arclasp.chain.Chain._poll_for_approval", denied),
    ):
        with pytest.raises(ActionDeniedError):
            await governed.ainvoke({"amount_usd": 12000.0})

    assert side_effects == []


@pytest.mark.asyncio
async def test_governed_node_backend_unavailable_fails_closed_before_body():
    side_effects: list[str] = []
    _init()

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-unavailable"}
        if body.get("action_name") == "Executor":
            raise BackendUnavailableError("Backend unavailable")
        return ALLOW

    governed = _build_single_executor_graph(side_effects)
    with patch("arclasp.client._post", side_effect=mock_post):
        with pytest.raises(BackendUnavailableError):
            await governed.ainvoke({"amount_usd": 12000.0})

    assert side_effects == []


@pytest.mark.asyncio
async def test_governed_nodes_support_cumulative_planner_researcher_executor_flow():
    from arclasp.langgraph import govern, governed_node
    from typing_extensions import TypedDict

    _init()
    side_effects: list[str] = []
    event_calls: list[dict] = []
    graph_mod = _real_langgraph_graph()
    END = graph_mod.END
    StateGraph = graph_mod.StateGraph

    class State(TypedDict, total=False):
        plan: str
        amount_usd: float
        ready_to_execute: bool
        executed: bool

    def planner(state: State) -> State:
        side_effects.append("planner")
        return {"plan": "research before execution", "amount_usd": 1500.0}

    def researcher(state: State) -> State:
        side_effects.append("researcher")
        return {"ready_to_execute": state.get("plan") == "research before execution"}

    def executor(state: State) -> State:
        side_effects.append("executor")
        return {"executed": bool(state.get("ready_to_execute"))}

    graph = StateGraph(State)
    graph.add_node("Planner", governed_node(planner, name="Planner"))
    graph.add_node("Researcher", governed_node(researcher, name="Researcher"))
    graph.add_node("Executor", governed_node(executor, name="Executor"))
    graph.set_entry_point("Planner")
    graph.add_edge("Planner", "Researcher")
    graph.add_edge("Researcher", "Executor")
    graph.add_edge("Executor", END)

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-cumulative"}
        if path.endswith("/complete"):
            return {"id": "chain-langgraph-cumulative", "status": "completed"}
        event_calls.append(body)
        if body.get("action_name") == "Executor":
            return {
                "policy_decision": "require_approval",
                "decision_reason": "Cumulative financial exposure exceeds threshold",
                "decision_source": "backend_evaluation",
                "policy_name": "cumulative_financial_threshold",
            }
        return ALLOW

    async def approved(_self):
        assert side_effects == ["planner", "researcher"]
        return "approved in test"

    governed = govern(graph.compile(), chain_name="langgraph-cumulative")
    with (
        patch("arclasp.client._post", side_effect=mock_post),
        patch("arclasp.chain.Chain._poll_for_approval", approved),
    ):
        result = await governed.ainvoke({"amount_usd": 1500.0})

    assert result["executed"] is True
    assert side_effects == ["planner", "researcher", "executor"]
    assert [call["action_name"] for call in event_calls] == [
        "Planner",
        "Researcher",
        "Executor",
    ]


@pytest.mark.asyncio
async def test_result_telemetry_does_not_regate_but_new_later_action_does():
    from arclasp.langgraph import govern, governed_node
    from typing_extensions import TypedDict

    _init()
    side_effects: list[str] = []
    event_calls: list[dict] = []
    approvals: list[str] = []
    graph_mod = _real_langgraph_graph()
    END = graph_mod.END
    StateGraph = graph_mod.StateGraph

    class State(TypedDict, total=False):
        plan: str
        amount_usd: float
        ready_to_execute: bool
        executed: bool
        follow_up_done: bool

    def planner(state: State) -> State:
        side_effects.append("planner")
        return {"plan": "research before execution", "amount_usd": 1500.0}

    def researcher(state: State) -> State:
        side_effects.append("researcher")
        return {"ready_to_execute": state.get("plan") == "research before execution"}

    def executor(state: State) -> State:
        side_effects.append("executor")
        return {"executed": bool(state.get("ready_to_execute"))}

    def follow_up(state: State) -> State:
        side_effects.append("follow-up")
        return {"follow_up_done": bool(state.get("executed"))}

    graph = StateGraph(State)
    graph.add_node(
        "Planner",
        governed_node(planner, name="Planner", payload={"amount_usd": 1500.0}),
    )
    graph.add_node(
        "Researcher",
        governed_node(researcher, name="Researcher", payload={"amount_usd": 1000.0}),
    )
    graph.add_node(
        "Executor",
        governed_node(executor, name="Executor", payload={"amount_usd": 700.0}),
    )
    graph.add_node(
        "FollowUp",
        governed_node(follow_up, name="FollowUp", payload={"amount_usd": 5000.0}),
    )
    graph.set_entry_point("Planner")
    graph.add_edge("Planner", "Researcher")
    graph.add_edge("Researcher", "Executor")
    graph.add_edge("Executor", "FollowUp")
    graph.add_edge("FollowUp", END)

    async def mock_post(path, body, action_type=None, headers=None, config=None):
        if path == "/v1/chains":
            return {"id": "chain-langgraph-p2"}
        if path.endswith("/complete"):
            return {"id": "chain-langgraph-p2", "status": "completed"}
        event_calls.append(body)
        action_name = body.get("action_name")
        if action_name in {"Executor", "FollowUp"}:
            approvals.append(action_name)
            return {
                "policy_decision": "require_approval",
                "decision_reason": "Cumulative financial exposure exceeds threshold",
                "decision_source": "backend_evaluation",
                "policy_name": "cumulative_financial_threshold",
                "cumulative_metrics": {
                    "financial_exposure_usd": 3200.0
                    if action_name == "Executor"
                    else 8200.0
                },
            }
        return {
            **ALLOW,
            "cumulative_metrics": {
                "financial_exposure_usd": 1500.0
                if action_name == "Planner"
                else 2500.0
            },
        }

    async def approved(_self):
        return "approved in test"

    governed = govern(graph.compile(), chain_name="langgraph-p2-regating")
    with (
        patch("arclasp.client._post", side_effect=mock_post),
        patch("arclasp.chain.Chain._poll_for_approval", approved),
    ):
        result = await governed.ainvoke({})

    assert result["executed"] is True
    assert result["follow_up_done"] is True
    assert side_effects == ["planner", "researcher", "executor", "follow-up"]
    assert approvals == ["Executor", "FollowUp"]
    assert [call["action_name"] for call in event_calls] == [
        "Planner",
        "Researcher",
        "Executor",
        "FollowUp",
    ]
    assert not any(
        call["action_name"].endswith(":result") for call in event_calls
    )
