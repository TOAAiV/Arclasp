"""
End-to-end demo: LangGraph 4-agent vendor purchase workflow.

Demonstrates Arclasp governance over a multi-agent workflow:
  - 4 agents: pricing-research, offer-calculator, communication,
              commitment-recorder
  - The commitment-recorder runs 4 times, each committing $3,000
  - The 4th commitment pushes the cumulative total to $12,000 (over the
    $10,000 configured threshold) and triggers require_approval
  - A mocked human approver immediately grants approval
  - The chain completes and its signed receipt is captured as evidence

Usage:
    cd sdk/
    python demos/langgraph_vendor_workflow.py

Artifacts (written to verification-artifacts/demos/):
    langgraph-chain-trace.json  — ordered event log with governance decisions
    langgraph-receipt.json      — signed audit receipt
    langgraph-stdout.txt        — console output (if stdout is redirected here)

Mock pattern (mirrors SDK integration tests):
    patch("arclasp.client._post") — intercepts chain lifecycle and event POSTs
    patch("arclasp.client._get")  — intercepts approval-status poll and receipt GET
    patch("asyncio.sleep")          — makes the 5-second approval-poll instant
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import arclasp
from arclasp import Chain

# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------

ARTIFACT_DIR = Path("verification-artifacts/demos")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mock constants — mirror the response shapes the real backend returns
# ---------------------------------------------------------------------------

CHAIN_ID = "chn_demo_langgraph_vendor_001"

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

# Signed receipt returned by GET /v1/chains/{id}/receipt after chain completes.
# Signature field is a 64-char hex string matching HMAC-SHA256 output length.
MOCK_RECEIPT_DATA = {
    "receipt_number": "PR-2026-DEMO-001",
    "summary": (
        "Vendor purchase workflow: 7 actions across 4 agents, "
        "$12,000 total commitment, 1 approval required and granted."
    ),
    "structured_data": {
        "chain_id": CHAIN_ID,
        "workflow": "vendor_purchase",
        "agents_involved": [
            "pricing-research",
            "offer-calculator",
            "communication",
            "commitment-recorder",
        ],
        "events_count": 7,
        "cumulative_financial_exposure_usd": 12000,
        "approvals_required": 1,
        "approvals_granted": 1,
        "policy_version": "v0.1.0",
    },
    "signature": "f3a9c1b4e8d2a0b17e5f6c9d3e4a8b1cf5d2e7a0b3c4d8e9f1a2b3c4d5e6f7a8",
    "previous_receipt_hash": "0" * 64,
    "created_at": "2026-06-21T12:00:30Z",
}

# ---------------------------------------------------------------------------
# Stateful mock backend
# ---------------------------------------------------------------------------

_event_call_count = 0


async def _mock_post(path: str, data: dict, action_type: str | None = None) -> dict:
    """
    Intercepts arclasp.client._post.

    Routing:
      POST /v1/chains                → assign chain id
      POST /v1/chains/{id}/events   → return allow for events 1-6;
                                       return require_approval for event 7
      POST /v1/chains/{id}/complete → acknowledge
    """
    global _event_call_count
    if path == "/v1/chains":
        return {"id": CHAIN_ID}
    if path.endswith("/complete"):
        return {}
    # /v1/chains/{id}/events
    _event_call_count += 1
    if _event_call_count == 7:
        # 7th event = 4th record_commitment call = $12k cumulative → threshold crossed
        return REQUIRE_APPROVAL
    return ALLOW


async def _mock_get(path: str, action_type: str | None = None) -> dict:
    """
    Intercepts arclasp.client._get.

    Routing:
      GET .../approval-status → immediately "approved" (mocked human decision)
      GET .../receipt         → signed receipt data
    """
    if "approval-status" in path:
        return {
            "approval_status": "approved",
            "approvals": [{"reason": "Approved: within quarterly vendor budget (demo)"}],
        }
    if path.endswith("/receipt"):
        return MOCK_RECEIPT_DATA
    return {}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

async def _run_vendor_workflow() -> tuple[Chain, list[dict]]:
    """
    Execute the 4-agent vendor purchase workflow under Arclasp governance.

    Returns the closed Chain object (chain_id still accessible) and the
    ordered events log.
    """
    events_log: list[dict] = []

    async with Chain(
        name="vendor-purchase-demo",
        metadata={"workflow": "vendor_purchase", "demo_run": True},
    ) as chain:
        cid = chain.chain_id
        print(f"  Chain started  →  id={cid}")
        print()

        # ── Agent 1: Pricing Research ────────────────────────────────────────
        d = await chain.record_agent_action(
            agent_name="pricing-research",
            action_type="tool_call",
            action_name="search_web",
            payload={"query": "enterprise SaaS pricing benchmarks Q2 2026"},
        )
        events_log.append({
            "seq": 1, "agent": "pricing-research",
            "action": "search_web", "decision": d.policy_decision,
        })
        print(f"  [1] pricing-research     search_web                    → {d.policy_decision}")

        # ── Agent 2: Offer Calculator ────────────────────────────────────────
        d = await chain.record_agent_action(
            agent_name="offer-calculator",
            action_type="tool_call",
            action_name="calculate_offer",
            payload={"vendor": "vendor-a", "amount_usd": 3000},
        )
        events_log.append({
            "seq": 2, "agent": "offer-calculator",
            "action": "calculate_offer", "amount_usd": 3000,
            "decision": d.policy_decision,
        })
        print(f"  [2] offer-calculator     calculate_offer ($3,000)       → {d.policy_decision}")

        # ── Agent 3: Communication ───────────────────────────────────────────
        d = await chain.record_agent_action(
            agent_name="communication",
            action_type="tool_call",
            action_name="send_email",
            payload={"to": "vendor-a@example.com", "subject": "Initial purchase proposal"},
        )
        events_log.append({
            "seq": 3, "agent": "communication",
            "action": "send_email", "decision": d.policy_decision,
        })
        print(f"  [3] communication        send_email                     → {d.policy_decision}")

        # ── Agent 4: Commitment Recorder (4 rounds) ──────────────────────────
        commitments = [
            ("vendor-a", 3000,  3_000),
            ("vendor-b", 3000,  6_000),
            ("vendor-c", 3000,  9_000),
            ("vendor-d", 3000, 12_000),  # crosses $10k → require_approval
        ]
        for i, (vendor, amount, cumulative) in enumerate(commitments, start=1):
            d = await chain.record_agent_action(
                agent_name="commitment-recorder",
                action_type="tool_call",
                action_name="record_commitment",
                payload={"vendor": vendor, "amount_usd": amount},
            )
            events_log.append({
                "seq": 3 + i,
                "agent": "commitment-recorder",
                "action": "record_commitment",
                "vendor": vendor,
                "amount_usd": amount,
                "cumulative_usd": cumulative,
                "decision": d.policy_decision,
                "decision_source": d.decision_source,
            })
            note = ""
            if d.decision_source == "human_approval":
                note = "  ← human approved"
            elif cumulative >= 10000:
                note = f"  ← !! crossed ${10000:,} threshold"
            print(
                f"  [{3+i}] commitment-recorder  record_commitment "
                f"{vendor} ${amount:,}  (cumulative ${cumulative:,})"
                f"  → {d.policy_decision}{note}"
            )

    # chain._chain_id is still set after __aexit__ — safe to return the object.
    return chain, events_log


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("=" * 65)
    print("  Arclasp — LangGraph 4-Agent Vendor Purchase Demo")
    print("=" * 65)
    print()
    print("  Agents    : pricing-research -> offer-calculator ->")
    print("              communication -> commitment-recorder (x4)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : Each commitment is $3,000; 4 rounds = $12,000 total.")
    print("              4th commitment crosses threshold -> require_approval.")
    print("              Mocked human approver grants approval immediately.")
    print("              Chain completes; signed receipt captured.")
    print()

    # Configure SDK for local demo: fast-path OFF so every action hits the
    # mock backend synchronously and the require_approval on call 7 is observed.
    arclasp.init(
        api_key="prail_test_demo00000000000000000000000000000000000000000",
        backend_url="http://mock-backend.local",
        environment="development",
        enable_local_fast_path=False,
        cumulative_financial_threshold_usd=10000,
        fallback_approvers=["lead@example.com"],
        default_approval_timeout_hours=1,  # 3600s budget for approval poll
    )

    print("  Executing workflow…")
    print()

    with (
        patch("arclasp.client._post", side_effect=_mock_post),
        patch("arclasp.client._get",  side_effect=_mock_get),
        # Skip the 5-second sleep in _poll_for_approval so the demo runs instantly.
        patch("asyncio.sleep",          new=AsyncMock(return_value=None)),
    ):
        chain, events_log = await _run_vendor_workflow()
        # Receipt: chain._chain_id still set after __aexit__; _mock_get handles /receipt.
        receipt = await chain.receipt()

    # ── Console summary ──────────────────────────────────────────────────────
    print()
    print("  [OK] Workflow completed")
    print()
    print("  Chain:")
    print(f"    ID             : {chain.chain_id}")
    print(f"    Actions logged : {len(events_log)}")
    decisions = [e["decision"] for e in events_log]
    print(f"    Decisions      : {', '.join(decisions)}")
    print()
    print("  Receipt:")
    print(f"    Number         : {receipt.receipt_number}")
    sig = receipt.signature or ""
    print(f"    Signature      : {sig[:32]}… ({len(sig)} chars)")
    print(f"    Created at     : {receipt.created_at}")
    print()

    # ── Artifact: chain trace ────────────────────────────────────────────────
    trace = {
        "demo_name": "langgraph_vendor_workflow",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": chain.chain_id,
        "agents": [
            "pricing-research",
            "offer-calculator",
            "communication",
            "commitment-recorder",
        ],
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "fallback_approvers": ["lead@example.com"],
            "fast_path_enabled": False,
        },
        "events": events_log,
    }
    trace_path = ARTIFACT_DIR / "langgraph-chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)

    # ── Artifact: receipt ────────────────────────────────────────────────────
    receipt_path = ARTIFACT_DIR / "langgraph-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(json.loads(receipt.model_dump_json()), f, indent=2)

    print("  Artifacts:")
    print(f"    {trace_path}  ({trace_path.stat().st_size} bytes, {len(events_log)} events)")
    print(f"    {receipt_path}  ({receipt_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
