"""
Production end-to-end demo: MCP 4-agent vendor purchase workflow.

Same scenario as the LangGraph demo but exercised through the MCP adapter
(ArclaspMcpAdapter.handle_tool_call).  7 tool calls fire 7 recorded
events; the 7th call (vendor-d, cumulative $12,000) crosses the $10,000
threshold and triggers require_approval.

Usage (from repo root):
    cd sdk
    python demos/production/mcp_demo_prod.py

Requires:
    ARCLASP_API_KEY        — production API key (prail_...)
    ARCLASP_APPROVER_EMAIL — email address to receive the approval notification

Artifacts written to verification-artifacts/demos/production/mcp/:
    chain-trace.json
    receipt.json
    stdout.txt
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

# If you hit SSL certificate verification errors on Windows, set SSL_CERT_FILE
# to certifi's bundle (`python -c "import certifi; print(certifi.where())"`)
# rather than disabling certificate verification.
import arclasp
import arclasp.client as _pr_client
from arclasp import Chain
from arclasp.mcp import ArclaspMcpAdapter

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent
ARTIFACT_DIR = _REPO_ROOT / "verification-artifacts" / "demos" / "production" / "mcp"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------

_captured_chain_id: str | None = None
_approval_start_time: float | None = None
_approver_email: str | None = None

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
        print(f"  Check {_approver_email} and click the approve link.")
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
# Tool definitions (vendor purchase workflow)
# ---------------------------------------------------------------------------

_TOOL_CALLS = [
    ("search_web",        {"query": "enterprise SaaS pricing benchmarks Q2 2026"},
     "pricing-research",    None,     0),
    ("calculate_offer",   {"vendor": "vendor-a", "amount": 3000},
     "offer-calculator",   3000,  3_000),
    ("record_commitment", {"vendor": "vendor-a", "amount": 3000},
     "commitment-recorder", 3000,  3_000),
    ("record_commitment", {"vendor": "vendor-b", "amount": 3000},
     "commitment-recorder", 3000,  6_000),
    ("record_commitment", {"vendor": "vendor-c", "amount": 3000},
     "commitment-recorder", 3000,  9_000),
    ("record_commitment", {"vendor": "vendor-d", "amount": 3000},
     "commitment-recorder", 3000, 12_000),  # crosses $10k threshold -> require_approval
    ("send_email",        {"to": "vendor-d@example.com", "subject": "Purchase commitments confirmed"},
     "communication",       None,     0),
]


async def _tool_handler(tool_name: str, arguments: dict) -> dict:
    return {"status": "executed", "tool": tool_name}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


async def _run_vendor_workflow() -> tuple[Chain, list[dict]]:
    events_log: list[dict] = []

    async with Chain(
        name="mcp-vendor-purchase-prod",
        metadata={"workflow": "vendor_purchase", "adapter": "mcp", "demo": "production"},
    ) as chain:
        cid = chain.chain_id
        print(f"  Chain started  →  id={cid}")
        print()

        adapter = ArclaspMcpAdapter(
            chain=chain,
            agent_name="mcp-vendor-agent",
        )

        for seq, (tool_name, arguments, agent, amount, cumulative) in enumerate(_TOOL_CALLS, start=1):
            await adapter.handle_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                handler=_tool_handler,
            )
            decision_source = "human_approval" if cumulative > 10000 else "backend_evaluation"
            note = ""
            if decision_source == "human_approval":
                note = "  <- human approved"
            elif cumulative >= 10000:
                note = "  <- crossed $10,000 threshold"
            amt_str = f" ${amount:,}" if amount else ""
            cum_str = f" (cumulative ${cumulative:,})" if cumulative else ""
            print(
                f"  [{seq}] {agent:<20}  {tool_name}{amt_str}{cum_str}"
                f"  → allowed{note}"
            )
            events_log.append({
                "seq": seq,
                "agent": agent,
                "tool": tool_name,
                "amount_usd": amount,
                "cumulative_usd": cumulative,
                "decision_source": decision_source,
            })

    return chain, events_log


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    global _approver_email

    api_key = os.environ.get("ARCLASP_API_KEY", "")
    if not api_key:
        print("ERROR: ARCLASP_API_KEY environment variable is not set.")
        return 1

    approver_email = os.environ.get("ARCLASP_APPROVER_EMAIL", "")
    if not approver_email:
        print("ERROR: ARCLASP_APPROVER_EMAIL environment variable is not set.")
        print("       Set it to the email address that should receive approval requests.")
        return 1
    _approver_email = approver_email

    print("=" * 65)
    print("  Arclasp — MCP Production Demo")
    print("=" * 65)
    print()
    print("  Backend   : https://api.proofrail.dev")
    print("  Adapter   : arclasp.mcp.ArclaspMcpAdapter")
    print("  Agent     : mcp-vendor-agent")
    print("  Tools     : search_web -> calculate_offer -> send_email ->")
    print("              record_commitment (x4 vendors)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : 7 tool calls; 7th call pushes to $12,000 total.")
    print("              Crosses threshold -> require_approval -> owner approves.")
    print()

    arclasp.init(
        api_key=api_key,
        backend_url="https://api.proofrail.dev",
        environment="production",
        cumulative_financial_threshold_usd=10000,
        default_approval_timeout_hours=1,
        fallback_approvers=[approver_email],
    )

    print("  Executing workflow…")
    print()

    run_start = datetime.now(timezone.utc)
    chain, events_log = await _run_vendor_workflow()
    run_end = datetime.now(timezone.utc)

    chain_id = chain.chain_id or _captured_chain_id

    chain_detail = await _pr_client.get_chain(chain_id)
    receipt = await _pr_client.get_chain_receipt(chain_id)
    events_resp = await _pr_client.get_chain_events(chain_id)

    print()
    print("  [OK] Workflow completed")
    print()
    print("  Chain:")
    print(f"    ID              : {chain_id}")
    print(f"    Status          : {chain_detail.status}")
    print(f"    Tools invoked   : {len(events_log)}")
    print()
    if receipt:
        print("  Receipt:")
        print(f"    Number          : {receipt.receipt_number}")
        sig = receipt.signature or ""
        print(f"    Signature       : {sig[:32]}… ({len(sig)} chars)")
        print(f"    Created at      : {receipt.created_at}")
    else:
        print("  Receipt: NOT GENERATED (chain may not have completed)")
    print()

    trace = {
        "demo_name": "mcp_vendor_workflow_production",
        "adapter": "mcp",
        "run_at": run_start.isoformat(),
        "run_end": run_end.isoformat(),
        "chain_id": chain_id,
        "chain_detail": json.loads(chain_detail.model_dump_json()),
        "governance_config": {
            "cumulative_financial_threshold_usd": 10000,
            "backend_authoritative": False,
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
    print("  SUCCESS: MCP production demo complete")
    print(f"  Chain ID:    {chain_id}")
    print(f"  Receipt ID:  {receipt.receipt_number if receipt else 'N/A'}")
    print(f"  Decision:    {decision_source}")
    print(f"  Artifacts:   {ARTIFACT_DIR}")
    print("  ============================================================")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
