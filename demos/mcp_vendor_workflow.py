"""
End-to-end demo: MCP vendor purchase workflow.

Same scenario as the LangGraph demo — 7 agent actions accumulating to $12,000
against a $10,000 threshold — but exercised through the MCP adapter.

How Arclasp hooks in:
    No govern() wrapper for MCP.  Instead:
      1. Open a Arclasp Chain context as usual.
      2. Create ArclaspMcpAdapter(chain=chain, agent_name=...).
      3. For each tool invocation, call adapter.handle_tool_call(tool_name,
         arguments, handler).  The adapter calls chain.record_agent_action()
         before invoking the handler function.
    Result: 7 backend events (one per handle_tool_call, action_type="tool_call").
    Event 7 (record_commitment for vendor-d) triggers require_approval; the
    mocked human approver grants it immediately.

Usage:
    cd sdk/
    python demos/mcp_vendor_workflow.py

Artifacts written to verification-artifacts/demos/:
    mcp-chain-trace.json
    mcp-receipt.json
    mcp-stdout.txt
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import arclasp
from arclasp import Chain
from arclasp.mcp.adapter import ArclaspMcpAdapter

ARTIFACT_DIR = Path("verification-artifacts/demos")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mock constants
# ---------------------------------------------------------------------------

CHAIN_ID = "chn_demo_mcp_vendor_001"

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
    "receipt_number": "PR-2026-MCP-DEMO-001",
    "summary": (
        "MCP vendor purchase workflow: 7 tool calls, $12,000 total commitment, "
        "1 approval required and granted."
    ),
    "structured_data": {
        "chain_id": CHAIN_ID,
        "framework": "mcp",
        "workflow": "vendor_purchase",
        "tool_calls_count": 7,
        "cumulative_financial_exposure_usd": 12000,
        "approvals_granted": 1,
        "policy_version": "v0.1.0",
    },
    "signature": "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4",
    "previous_receipt_hash": "0" * 64,
    "created_at": "2026-06-21T12:03:00Z",
}

# ---------------------------------------------------------------------------
# Stateful mock — 7 events (one per handle_tool_call); require_approval on 7.
# ---------------------------------------------------------------------------

TRIGGER_COUNT = 7
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
# Tool handler and workflow definition
# ---------------------------------------------------------------------------

async def _tool_handler(tool_name: str, arguments: dict) -> dict:
    """Mock MCP tool executor — returns a structured result for each tool."""
    return {"tool": tool_name, "status": "ok", "arguments": arguments}


# 7 tool invocations representing the vendor purchase workflow.
_VENDOR_TOOL_CALLS: list[tuple[str, dict]] = [
    ("search_web",        {"query": "enterprise SaaS pricing benchmarks Q2 2026"}),
    ("calculate_offer",   {"vendor": "vendor-a", "amount_usd": 3000}),
    ("send_email",        {"to": "vendor-a@example.com", "subject": "Initial proposal"}),
    ("record_commitment", {"vendor": "vendor-a", "amount_usd": 3000}),  # cumulative $3k
    ("record_commitment", {"vendor": "vendor-b", "amount_usd": 3000}),  # cumulative $6k
    ("record_commitment", {"vendor": "vendor-c", "amount_usd": 3000}),  # cumulative $9k
    ("record_commitment", {"vendor": "vendor-d", "amount_usd": 3000}),  # cumulative $12k
]

# Known semantic steps for events_log construction.
_SEMANTIC_STEPS = [
    ("vendor-purchase-mcp", "search_web",        None,  None),
    ("vendor-purchase-mcp", "calculate_offer",   3000,  3_000),
    ("vendor-purchase-mcp", "send_email",        None,  None),
    ("vendor-purchase-mcp", "record_commitment", 3000,  3_000),
    ("vendor-purchase-mcp", "record_commitment", 3000,  6_000),
    ("vendor-purchase-mcp", "record_commitment", 3000,  9_000),
    ("vendor-purchase-mcp", "record_commitment", 3000, 12_000),
]


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

async def _run_vendor_workflow() -> tuple[Chain, list[dict]]:
    events_log: list[dict] = []

    with (
        patch("arclasp.client._post", side_effect=_mock_post),
        patch("arclasp.client._get",  side_effect=_mock_get),
        patch("asyncio.sleep",          new=AsyncMock(return_value=None)),
    ):
        async with Chain(
            "mcp-vendor-purchase",
            metadata={"workflow": "vendor_purchase", "framework": "mcp", "demo_run": True},
        ) as chain:
            adapter = ArclaspMcpAdapter(chain=chain, agent_name="vendor-purchase-mcp")

            for tool_name, tool_args in _VENDOR_TOOL_CALLS:
                await adapter.handle_tool_call(tool_name, tool_args, _tool_handler)

        # chain._chain_id persists after __aexit__ — safe to call receipt()
        receipt = await chain.receipt()

    # Build events_log from known semantic steps + known mock decisions.
    # Steps 1-6 get allow/backend_evaluation; step 7 gets allow/human_approval
    # because event 7 (7th handle_tool_call) triggered the approval flow.
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

    return chain, events_log, receipt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("=" * 65)
    print("  Arclasp -- MCP Vendor Purchase Demo")
    print("=" * 65)
    print()
    print("  Adapter   : arclasp.mcp.adapter.ArclaspMcpAdapter")
    print("  Interface : adapter.handle_tool_call(tool, args, handler)")
    print("  Agent     : vendor-purchase-mcp (single MCP server)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : 7 handle_tool_call invocations; call 7")
    print("              (record_commitment vendor-d, $12k cumulative)")
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

    chain, events_log, receipt = await _run_vendor_workflow()

    for ev in events_log:
        suffix = ""
        if ev["decision_source"] == "human_approval":
            suffix = "  <- human approved"
        note = ""
        if ev.get("cumulative_usd"):
            note = f" (cumulative ${ev['cumulative_usd']:,})"
        print(
            f"  [{ev['seq']}] {ev['agent']:22s}  {ev['action']:20s}"
            f"{note:20s}  -> {ev['decision']}{suffix}"
        )

    print()
    print("  [OK] Workflow completed")
    print()
    print(f"    Chain ID       : {chain.chain_id}")
    print(f"    Tool calls     : {len(events_log)}")
    sig = receipt.signature or ""
    print(f"    Receipt        : {receipt.receipt_number}")
    print(f"    Signature      : {sig[:32]}... ({len(sig)} chars)")
    print()

    trace = {
        "demo_name": "mcp_vendor_workflow",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": chain.chain_id,
        "framework": "mcp",
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "fast_path_enabled": False,
        },
        "events": events_log,
    }
    trace_path = ARTIFACT_DIR / "mcp-chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)

    receipt_path = ARTIFACT_DIR / "mcp-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(json.loads(receipt.model_dump_json()), f, indent=2)

    print(f"  Artifacts:")
    print(f"    {trace_path}  ({trace_path.stat().st_size} bytes)")
    print(f"    {receipt_path}  ({receipt_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
