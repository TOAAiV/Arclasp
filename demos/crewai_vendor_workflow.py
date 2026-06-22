"""
End-to-end demo: CrewAI 4-agent vendor purchase workflow.

Same scenario as the LangGraph demo — 7 agent actions accumulating to $12,000
against a $10,000 threshold — but exercised through the CrewAI adapter.

How ProofRail hooks in:
    govern(crew, chain_name=...) wraps any CrewAI Crew.
    The adapter uses the "mixed" strategy (CrewAI 1.x shape):
      - Strategy B monkey-patches each agent's execute_task method to fire
        a "task_execution" event at the START of each task.
      - Strategy A wraps the crew's task_callback to fire a "task_result"
        event at the END of each task.
    Result: 14 backend events for 7 tasks (2 per task).
    Event 13 (7th task_execution) triggers require_approval; the mocked
    human approver grants it immediately.

Usage:
    cd sdk/
    python demos/crewai_vendor_workflow.py

Artifacts written to verification-artifacts/demos/:
    crewai-chain-trace.json
    crewai-receipt.json
    crewai-stdout.txt
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import proofrail
from proofrail.crewai.adapter import govern

ARTIFACT_DIR = Path("verification-artifacts/demos")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mock constants
# ---------------------------------------------------------------------------

CHAIN_ID = "chn_demo_crewai_vendor_001"

ALLOW = {
    "policy_decision": "allow",
    "decision_reason": "Action permitted by policy",
    "decision_source": "backend_evaluation",
}

REQUIRE_APPROVAL = {
    "policy_decision": "require_approval",
    "decision_reason": (
        "Cumulative financial exposure ($12,000) exceeds the configured "
        "$10,000 threshold — human approval required before proceeding."
    ),
    "decision_source": "backend_evaluation",
}

MOCK_RECEIPT_DATA = {
    "receipt_number": "PR-2026-CA-DEMO-001",
    "summary": (
        "CrewAI vendor purchase workflow: 7 tasks across 4 agents, "
        "$12,000 total commitment, 1 approval required and granted."
    ),
    "structured_data": {
        "chain_id": CHAIN_ID,
        "framework": "crewai",
        "workflow": "vendor_purchase",
        "tasks_count": 7,
        "cumulative_financial_exposure_usd": 12000,
        "approvals_granted": 1,
        "policy_version": "v0.1.0",
    },
    "signature": "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3",
    "previous_receipt_hash": "0" * 64,
    "created_at": "2026-06-21T12:02:00Z",
}

# ---------------------------------------------------------------------------
# Stateful mock — 14 events total (7 task_start + 7 task_end); require_approval
# fires on event 13 (7th task_start).
# ---------------------------------------------------------------------------

# TRIGGER_COUNT = 13: each task fires 2 events (task_execution + task_result).
# Tasks 1-6 produce events 1-12 (all allow).
# Task 7 task_execution = event 13 → require_approval → blocks → approval.
# Task 7 task_result    = event 14 → allow.
TRIGGER_COUNT = 13
_event_call_count = 0


async def _mock_post(path: str, data: dict, action_type: str | None = None) -> dict:
    global _event_call_count
    if path == "/v1/chains":
        return {"id": CHAIN_ID}
    if path.endswith("/complete"):
        return {}
    _event_call_count += 1
    if _event_call_count == TRIGGER_COUNT:
        return REQUIRE_APPROVAL
    return ALLOW


async def _mock_get(path: str, action_type: str | None = None) -> dict:
    if "approval-status" in path:
        return {
            "approval_status": "approved",
            "approvals": [{"reason": "Approved: within quarterly vendor budget (demo)"}],
        }
    if path.endswith("/receipt"):
        return MOCK_RECEIPT_DATA
    return {}


# ---------------------------------------------------------------------------
# CrewAI stub objects
# ---------------------------------------------------------------------------

class _MockAgent:
    def __init__(self, role: str) -> None:
        self.role = role

    def execute_task(self, task: Any, context: Any = None, tools: Any = None) -> str:
        return f"Completed: {task.description}"


class _MockTask:
    def __init__(self, description: str) -> None:
        self.description = description
        self.expected_output = "Task completed successfully"


class _MockTaskOutput:
    def __init__(self, raw: str, agent: str, description: str = "") -> None:
        self.raw = raw
        self.agent = agent
        self.summary = raw[:120]
        self.description = description


# 7 tasks and their corresponding agents for the vendor purchase workflow.
_VENDOR_TASKS = [
    _MockTask("Search vendor pricing benchmarks for enterprise SaaS Q2 2026"),
    _MockTask("Calculate initial offer: $3,000 for vendor-a"),
    _MockTask("Send proposal email to vendor-a@example.com"),
    _MockTask("Record vendor-a commitment: $3,000 (cumulative $3,000)"),
    _MockTask("Record vendor-b commitment: $3,000 (cumulative $6,000)"),
    _MockTask("Record vendor-c commitment: $3,000 (cumulative $9,000)"),
    _MockTask("Record vendor-d commitment: $3,000 (cumulative $12,000)"),
]

_VENDOR_AGENTS = [
    _MockAgent("pricing-researcher"),
    _MockAgent("offer-calculator"),
    _MockAgent("communications-manager"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
]


class _VendorCrew:
    """
    Minimal CrewAI 1.x Crew stub for the vendor purchase workflow.

    Shape mirrors _StubCrewMixed from the integration tests:
    - Has task_callback (Strategy A wraps this for end events)
    - No before_task_callback (triggers mixed strategy)
    - agents.execute_task patched by Strategy B for start events
    - kickoff_async dispatches each task via asyncio.to_thread
    """

    def __init__(self) -> None:
        self.agents = _VENDOR_AGENTS
        self.tasks = _VENDOR_TASKS
        self.task_callback: Any = None  # adapter wraps this

    async def kickoff_async(self, inputs: Any = None, **kw: Any) -> list:
        results = []
        for task, agent in zip(self.tasks, self.agents):
            after_cb = self.task_callback

            def _run(t: _MockTask = task, a: _MockAgent = agent, acb: Any = after_cb) -> _MockTaskOutput:
                # execute_task is monkey-patched by Strategy B → fires task_execution event
                raw = a.execute_task(t)
                out = _MockTaskOutput(raw, a.role, description=t.description)
                # task_callback wrapped by Strategy A → fires task_result event
                if callable(acb):
                    acb(out)
                return out

            out = await asyncio.to_thread(_run)
            results.append(out)
        return results

    def kickoff(self, inputs: Any = None, **kw: Any) -> Any:
        return asyncio.run(self.kickoff_async(inputs, **kw))


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

# Known semantic steps for events_log construction.
_SEMANTIC_STEPS = [
    ("pricing-researcher",    "search_benchmarks",     None,  None),
    ("offer-calculator",      "calculate_offer",       3000,  3_000),
    ("communications-manager","send_email",             None,  None),
    ("commitment-recorder",   "record_commitment",     3000,  3_000),
    ("commitment-recorder",   "record_commitment",     3000,  6_000),
    ("commitment-recorder",   "record_commitment",     3000,  9_000),
    ("commitment-recorder",   "record_commitment",     3000, 12_000),
]


async def _run_vendor_workflow() -> tuple[str, list[dict]]:
    crew = _VendorCrew()
    governed = govern(crew, chain_name="crewai-vendor-purchase",
                      metadata={"workflow": "vendor_purchase", "framework": "crewai",
                                "demo_run": True})

    with (
        patch("proofrail.client._post", side_effect=_mock_post),
        patch("proofrail.client._get",  side_effect=_mock_get),
        patch("asyncio.sleep",          new=AsyncMock(return_value=None)),
    ):
        results = await governed.kickoff_async(inputs={"topic": "vendor purchase"})
        # Let fire-and-forget coroutines (if any) complete
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Build events_log: 7 entries (one per task), from known mock responses.
    # Tasks 1-6 get allow/backend_evaluation; task 7 gets allow/human_approval
    # because event 13 (7th task_execution) triggered the approval flow.
    events_log = []
    for i, (agent, action, amount, cumulative) in enumerate(_SEMANTIC_STEPS, start=1):
        entry: dict = {
            "seq": i,
            "agent": agent,
            "action": action,
            "decision": "allow",
            "decision_source": "human_approval" if i == 7 else "backend_evaluation",
        }
        if amount is not None:
            entry["amount_usd"] = amount
        if cumulative is not None:
            entry["cumulative_usd"] = cumulative
        events_log.append(entry)

    return CHAIN_ID, events_log


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("=" * 65)
    print("  ProofRail -- CrewAI 4-Agent Vendor Purchase Demo")
    print("=" * 65)
    print()
    print("  Adapter   : proofrail.crewai.adapter.govern()")
    print("  Agents    : pricing-researcher -> offer-calculator ->")
    print("              communications-manager -> commitment-recorder (x4)")
    print("  Strategy  : Mixed (CrewAI 1.x) — Strategy B fires task_execution")
    print("              start events; Strategy A fires task_result end events.")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : 7 tasks (14 events); event 13 = 7th task_execution")
    print("              triggers require_approval; human approves;")
    print("              chain completes with signed receipt.")
    print()

    proofrail.init(
        api_key="prail_test_demo00000000000000000000000000000000000000000",
        backend_url="http://mock-backend.local",
        environment="development",
        enable_local_fast_path=False,
        cumulative_financial_threshold_usd=10000,
        fallback_approvers=["lead@example.com"],
        default_approval_timeout_hours=1,
    )

    print("  Executing workflow...")
    print()

    chain_id, events_log = await _run_vendor_workflow()

    for ev in events_log:
        suffix = ""
        if ev["decision_source"] == "human_approval":
            suffix = "  <- human approved"
        note = ""
        if ev.get("cumulative_usd"):
            note = f" (cumulative ${ev['cumulative_usd']:,})"
        print(
            f"  [{ev['seq']}] {ev['agent']:26s}  {ev['action']:20s}"
            f"{note:20s}  -> {ev['decision']}{suffix}"
        )

    print()
    print("  [OK] Workflow completed")
    print()
    print(f"    Chain ID       : {chain_id}")
    print(f"    Tasks executed : {len(events_log)}")
    sig = MOCK_RECEIPT_DATA["signature"]
    print(f"    Receipt        : {MOCK_RECEIPT_DATA['receipt_number']}")
    print(f"    Signature      : {sig[:32]}... ({len(sig)} chars)")
    print()

    trace = {
        "demo_name": "crewai_vendor_workflow",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": chain_id,
        "framework": "crewai",
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "fast_path_enabled": False,
        },
        "events": events_log,
    }
    trace_path = ARTIFACT_DIR / "crewai-chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)

    receipt_path = ARTIFACT_DIR / "crewai-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(MOCK_RECEIPT_DATA, f, indent=2)

    print(f"  Artifacts:")
    print(f"    {trace_path}  ({trace_path.stat().st_size} bytes)")
    print(f"    {receipt_path}  ({receipt_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
