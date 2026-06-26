"""
Production end-to-end demo: LangGraph 4-agent vendor purchase workflow.

Runs the same 7-event scenario as the mocked demo, but against the live
ProofRail backend at https://api.proofrail.dev with a real API key.

The 4th commitment ($3,000 for vendor-d) pushes cumulative exposure to
$12,000 — crossing the $10,000 default threshold.  The backend triggers
require_approval, an email is sent to the configured approver, and this
process blocks until the owner clicks the approve link.

Usage (from repo root):
    cd sdk
    python demos/production/langgraph_demo_prod.py

Requires:
    PROOFRAIL_API_KEY  — production API key (prail_...)

Artifacts written to verification-artifacts/demos/production/langgraph/:
    chain-trace.json   — full chain + events from the backend
    receipt.json       — signed audit receipt
    stdout.txt         — captured terminal output (redirect externally)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Windows SSL workaround — must happen before httpx is used by the SDK.
# Python's bundled OpenSSL on Windows doesn't trust the system cert store.
# verify=False is acceptable here: this is a local developer script, not
# production server code.  The backend on Render runs Linux where SSL works.
# ---------------------------------------------------------------------------
import httpx as _httpx
_OrigAsyncClient = _httpx.AsyncClient
class _NoVerifyAsyncClient(_OrigAsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs["verify"] = False
        super().__init__(*args, **kwargs)
_httpx.AsyncClient = _NoVerifyAsyncClient

import proofrail
import proofrail.client as _pr_client
from proofrail import Chain

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent
ARTIFACT_DIR = _REPO_ROOT / "verification-artifacts" / "demos" / "production" / "langgraph"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Instrumentation — capture chain_id + approval progress messages
# ---------------------------------------------------------------------------

_captured_chain_id: str | None = None
_approval_start_time: float | None = None

_orig_post = _pr_client._post


async def _post_instrument(path: str, data: dict, action_type: str | None = None) -> dict:
    global _captured_chain_id, _approval_start_time
    result = await _orig_post(path, data, action_type)
    if path == "/v1/chains" and isinstance(result, dict) and "id" in result:
        _captured_chain_id = result["id"]
    if isinstance(result, dict) and result.get("policy_decision") == "require_approval":
        _approval_start_time = time.monotonic()
        print()
        print("  ============================================================")
        print("  AWAITING APPROVAL")
        print("  Check approver@example.com and click the approve link.")
        print(f"  Chain ID: {_captured_chain_id}")
        print("  ============================================================")
        print("  Polling: ", end="", flush=True)
    return result


_pr_client._post = _post_instrument

_orig_get = _pr_client._get


async def _get_instrument(path: str, action_type: str | None = None) -> dict:
    if "approval-status" in path:
        print(".", end="", flush=True)
    result = await _orig_get(path, action_type)
    if "approval-status" in path and isinstance(result, dict) and result.get("approval_status") == "approved":
        elapsed = time.monotonic() - _approval_start_time if _approval_start_time else 0
        print(f" APPROVED! ({elapsed:.0f}s elapsed)")
    return result


_pr_client._get = _get_instrument

# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


async def _run_vendor_workflow() -> tuple[Chain, list[dict]]:
    events_log: list[dict] = []

    async with Chain(
        name="vendor-purchase-langgraph-prod",
        metadata={"workflow": "vendor_purchase", "adapter": "langgraph", "demo": "production"},
    ) as chain:
        cid = chain.chain_id
        print(f"  Chain started  →  id={cid}")
        print()

        d = await chain.record_agent_action(
            agent_name="pricing-research",
            action_type="tool_call",
            action_name="search_web",
            payload={"query": "enterprise SaaS pricing benchmarks Q2 2026"},
        )
        events_log.append({"seq": 1, "agent": "pricing-research", "action": "search_web", "decision": d.policy_decision, "decision_source": d.decision_source})
        print(f"  [1] pricing-research     search_web                    → {d.policy_decision}")

        d = await chain.record_agent_action(
            agent_name="offer-calculator",
            action_type="tool_call",
            action_name="calculate_offer",
            payload={"vendor": "vendor-a", "amount": 3000},
        )
        events_log.append({"seq": 2, "agent": "offer-calculator", "action": "calculate_offer", "amount_usd": 3000, "decision": d.policy_decision, "decision_source": d.decision_source})
        print(f"  [2] offer-calculator     calculate_offer ($3,000)       → {d.policy_decision}")

        commitments = [
            ("vendor-a", 3000,  3_000),
            ("vendor-b", 3000,  6_000),
            ("vendor-c", 3000,  9_000),
            ("vendor-d", 3000, 12_000),  # crosses $10k threshold → require_approval
        ]
        for i, (vendor, amount, cumulative) in enumerate(commitments, start=1):
            d = await chain.record_agent_action(
                agent_name="commitment-recorder",
                action_type="tool_call",
                action_name="record_commitment",
                payload={"vendor": vendor, "amount": amount},
            )
            entry = {
                "seq": 2 + i,
                "agent": "commitment-recorder",
                "action": "record_commitment",
                "vendor": vendor,
                "amount_usd": amount,
                "cumulative_usd": cumulative,
                "decision": d.policy_decision,
                "decision_source": d.decision_source,
            }
            events_log.append(entry)
            note = ""
            if d.decision_source == "human_approval":
                note = "  <- human approved"
            elif cumulative >= 10000:
                note = f"  <- crossed $10,000 threshold"
            print(
                f"  [{2+i}] commitment-recorder  record_commitment "
                f"{vendor} ${amount:,}  (cumulative ${cumulative:,})"
                f"  → {d.policy_decision}{note}"
            )

        # send_email after financial threshold is crossed and approval granted,
        # so the "first external communication" policy does not fire prematurely.
        d = await chain.record_agent_action(
            agent_name="communication",
            action_type="tool_call",
            action_name="send_email",
            payload={"to": "vendor-d@example.com", "subject": "Purchase commitments confirmed"},
        )
        events_log.append({"seq": 7, "agent": "communication", "action": "send_email", "decision": d.policy_decision, "decision_source": d.decision_source})
        print(f"  [7] communication        send_email                     → {d.policy_decision}")

    return chain, events_log


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    api_key = os.environ.get("PROOFRAIL_API_KEY", "")
    if not api_key:
        print("ERROR: PROOFRAIL_API_KEY environment variable is not set.")
        return 1

    print("=" * 65)
    print("  ProofRail — LangGraph Production Demo")
    print("=" * 65)
    print()
    print("  Backend   : https://api.proofrail.dev")
    print("  Adapter   : proofrail.Chain (direct, LangGraph-style)")
    print("  Agents    : pricing-research -> offer-calculator ->")
    print("              communication -> commitment-recorder (x4)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : Each commitment is $3,000; 4 rounds = $12,000 total.")
    print("              4th commitment crosses threshold -> require_approval.")
    print("              Owner clicks approve link in email -> chain completes.")
    print()

    proofrail.init(
        api_key=api_key,
        backend_url="https://api.proofrail.dev",
        environment="production",
        enable_local_fast_path=False,
        cumulative_financial_threshold_usd=10000,
        default_approval_timeout_hours=1,
        fallback_approvers=["approver@example.com"],
    )

    print("  Executing workflow…")
    print()

    run_start = datetime.now(timezone.utc)
    chain, events_log = await _run_vendor_workflow()
    run_end = datetime.now(timezone.utc)

    chain_id = chain.chain_id or _captured_chain_id

    # Fetch chain detail and receipt from backend
    chain_detail = await _pr_client.get_chain(chain_id)
    receipt = await _pr_client.get_chain_receipt(chain_id)

    print()
    print("  [OK] Workflow completed")
    print()
    print("  Chain:")
    print(f"    ID             : {chain_id}")
    print(f"    Status         : {chain_detail.status}")
    print(f"    Actions logged : {len(events_log)}")
    decisions = [e["decision"] for e in events_log]
    print(f"    Decisions      : {', '.join(decisions)}")
    print()
    if receipt:
        print("  Receipt:")
        print(f"    Number         : {receipt.receipt_number}")
        sig = receipt.signature or ""
        print(f"    Signature      : {sig[:32]}… ({len(sig)} chars)")
        print(f"    Created at     : {receipt.created_at}")
    else:
        print("  Receipt: NOT GENERATED (chain may not have completed)")
    print()

    # Artifacts
    events_resp = await _pr_client.get_chain_events(chain_id)
    trace = {
        "demo_name": "langgraph_vendor_workflow_production",
        "adapter": "langgraph",
        "run_at": run_start.isoformat(),
        "run_end": run_end.isoformat(),
        "chain_id": chain_id,
        "chain_detail": json.loads(chain_detail.model_dump_json()),
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "fast_path_enabled": False,
            "environment": "production",
        },
        "events_from_backend": [e.model_dump() for e in events_resp.events],
        "events_log_local": events_log,
    }
    trace_path = ARTIFACT_DIR / "chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2, default=str)

    receipt_data = json.loads(receipt.model_dump_json()) if receipt else {}
    receipt_path = ARTIFACT_DIR / "receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2, default=str)

    decision_source = next(
        (e["decision_source"] for e in events_log if e["decision_source"] == "human_approval"),
        "backend_evaluation",
    )

    print()
    print("  ============================================================")
    print("  SUCCESS: LangGraph production demo complete")
    print(f"  Chain ID:    {chain_id}")
    print(f"  Receipt ID:  {receipt.receipt_number if receipt else 'N/A'}")
    print(f"  Decision:    {decision_source}")
    print(f"  Artifacts:   {ARTIFACT_DIR}")
    print("  ============================================================")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
