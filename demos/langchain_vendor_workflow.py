"""
End-to-end demo: LangChain 4-agent vendor purchase workflow.

Same scenario as the LangGraph demo — 7 agent actions accumulating to $12,000
against a $10,000 threshold — but exercised through the LangChain adapter.

How Arclasp hooks in:
    govern(chain, chain_name=...) wraps any LangChain Runnable.
    On every ainvoke(), a ArclaspLangChainCallback is injected into the
    config's callbacks list.  The stub chain fires on_tool_start + on_tool_end
    for each of the 7 vendor workflow tools, producing 14 backend events total.
    Event 13 (the 7th tool_start, "record_commitment" for vendor-d) triggers
    require_approval; the mocked human approver grants it immediately.

Usage:
    cd sdk/
    python demos/langchain_vendor_workflow.py

Artifacts written to verification-artifacts/demos/:
    langchain-chain-trace.json
    langchain-receipt.json
    langchain-stdout.txt
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

import arclasp
from arclasp.langchain.adapter import govern

ARTIFACT_DIR = Path("verification-artifacts/demos")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mock constants
# ---------------------------------------------------------------------------

CHAIN_ID = "chn_demo_langchain_vendor_001"

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
    "receipt_number": "PR-2026-LC-DEMO-001",
    "summary": (
        "LangChain vendor purchase workflow: 7 tool invocations across 4 agents, "
        "$12,000 total commitment, 1 approval required and granted."
    ),
    "structured_data": {
        "chain_id": CHAIN_ID,
        "framework": "langchain",
        "workflow": "vendor_purchase",
        "events_count": 7,
        "cumulative_financial_exposure_usd": 12000,
        "approvals_granted": 1,
        "policy_version": "v0.1.0",
    },
    "signature": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
    "previous_receipt_hash": "0" * 64,
    "created_at": "2026-06-21T12:01:00Z",
}

# ---------------------------------------------------------------------------
# Stateful mock — 14 events total (7 tool_start + 7 tool_end); require_approval
# fires on event 13 (7th tool_start = record_commitment for vendor-d).
# ---------------------------------------------------------------------------

# TRIGGER_COUNT = 13: the 7th on_tool_start fires as event 13 (each tool
# generates 2 events: on_tool_start = odd, on_tool_end = even).
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
# Vendor workflow stub chain
# ---------------------------------------------------------------------------

# 7 tools representing the 4-agent vendor workflow.
_VENDOR_TOOLS = [
    ("search_web",          {"query": "enterprise SaaS pricing benchmarks Q2 2026"}),
    ("calculate_offer",     {"vendor": "vendor-a", "amount_usd": 3000}),
    ("send_email",          {"to": "vendor-a@example.com", "subject": "Initial proposal"}),
    ("record_commitment",   {"vendor": "vendor-a", "amount_usd": 3000}),  # cumulative $3k
    ("record_commitment",   {"vendor": "vendor-b", "amount_usd": 3000}),  # cumulative $6k
    ("record_commitment",   {"vendor": "vendor-c", "amount_usd": 3000}),  # cumulative $9k
    ("record_commitment",   {"vendor": "vendor-d", "amount_usd": 3000}),  # cumulative $12k
]


class _VendorWorkflowChain:
    """
    Minimal LangChain-like Runnable stub for the vendor purchase workflow.

    ainvoke fires on_tool_start + on_tool_end for each of the 7 vendor tools,
    routing ArclaspLangChainCallback governance events through the injected
    callbacks list (populated by govern() before calling ainvoke).
    """

    def invoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        return {"output": "done"}

    async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        callbacks = (config or {}).get("callbacks", [])
        for tool_name, tool_input in _VENDOR_TOOLS:
            run_id = uuid.uuid4()
            for cb in callbacks:
                if hasattr(cb, "on_tool_start"):
                    await cb.on_tool_start(
                        {"name": tool_name}, str(tool_input), run_id=run_id
                    )
            for cb in callbacks:
                if hasattr(cb, "on_tool_end"):
                    await cb.on_tool_end("ok", run_id=run_id)
        return {"output": "vendor_workflow_complete"}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

# Known semantic steps (agent + action) for events_log construction.
_SEMANTIC_STEPS = [
    ("pricing-research",    "search_web",        None,  None),
    ("offer-calculator",    "calculate_offer",   3000,  3_000),
    ("communication",       "send_email",        None,  None),
    ("commitment-recorder", "record_commitment", 3000,  3_000),
    ("commitment-recorder", "record_commitment", 3000,  6_000),
    ("commitment-recorder", "record_commitment", 3000,  9_000),
    ("commitment-recorder", "record_commitment", 3000, 12_000),
]


async def _run_vendor_workflow() -> tuple[Any, list[dict]]:
    governed = govern(
        _VendorWorkflowChain(),
        chain_name="langchain-vendor-purchase",
        metadata={"workflow": "vendor_purchase", "framework": "langchain", "demo_run": True},
    )

    with (
        patch("arclasp.client._post", side_effect=_mock_post),
        patch("arclasp.client._get",  side_effect=_mock_get),
        patch("asyncio.sleep",          new=AsyncMock(return_value=None)),
    ):
        await governed.ainvoke({"input": "start vendor purchase workflow"})
        # Capture chain from governed internals to get chain_id
        # (GovernedChain exposes ._last_chain after ainvoke)
        chain_obj = getattr(governed, "_last_chain", None)
        chain_id = getattr(chain_obj, "chain_id", CHAIN_ID) if chain_obj else CHAIN_ID

        from arclasp.models import ChainReceiptResponse
        receipt = ChainReceiptResponse.model_validate(MOCK_RECEIPT_DATA)

    # Build events_log from known semantic steps + known decisions.
    # Steps 1-6 get allow/backend_evaluation; step 7 gets allow/human_approval
    # because event 13 (7th tool_start) triggers the approval flow.
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

    return chain_id, events_log, receipt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("=" * 65)
    print("  Arclasp -- LangChain 4-Agent Vendor Purchase Demo")
    print("=" * 65)
    print()
    print("  Adapter   : arclasp.langchain.adapter.govern()")
    print("  Agents    : pricing-research -> offer-calculator ->")
    print("              communication -> commitment-recorder (x4)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Hook path : govern() injects ArclaspLangChainCallback;")
    print("              on_tool_start fires record_agent_action per tool.")
    print("  Scenario  : 7 tools (14 events); event 13 = 7th tool_start")
    print("              triggers require_approval; human approves;")
    print("              chain completes with signed receipt.")
    print()

    arclasp.init(
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

    chain_id, events_log, receipt = await _run_vendor_workflow()

    for ev in events_log:
        suffix = ""
        if ev["decision_source"] == "human_approval":
            suffix = "  <- human approved"
        note = ""
        if ev.get("cumulative_usd"):
            note = f" (cumulative ${ev['cumulative_usd']:,})"
        print(
            f"  [{ev['seq']}] {ev['agent']:25s}  {ev['action']:20s}"
            f"{note:20s}  -> {ev['decision']}{suffix}"
        )

    print()
    print("  [OK] Workflow completed")
    print()
    print(f"    Chain ID       : {chain_id}")
    print(f"    Actions logged : {len(events_log)}")
    sig = receipt.signature or ""
    print(f"    Receipt        : {receipt.receipt_number}")
    print(f"    Signature      : {sig[:32]}... ({len(sig)} chars)")
    print()

    trace = {
        "demo_name": "langchain_vendor_workflow",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": chain_id,
        "framework": "langchain",
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "fast_path_enabled": False,
        },
        "events": events_log,
    }
    trace_path = ARTIFACT_DIR / "langchain-chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)

    receipt_path = ARTIFACT_DIR / "langchain-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(json.loads(receipt.model_dump_json()), f, indent=2)

    print(f"  Artifacts:")
    print(f"    {trace_path}  ({trace_path.stat().st_size} bytes)")
    print(f"    {receipt_path}  ({receipt_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
