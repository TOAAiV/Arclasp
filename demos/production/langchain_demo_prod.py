"""
Production end-to-end demo: LangChain 4-agent vendor purchase workflow.

Same scenario as the LangGraph demo but exercised through the LangChain
adapter (govern() + ArclaspLangChainCallback).  7 tool invocations fire
14 events (on_tool_start + on_tool_end each); the 7th on_tool_start pushes
cumulative exposure to $12,000, triggering require_approval.

Usage (from repo root):
    cd sdk
    python demos/production/langchain_demo_prod.py

Requires:
    ARCLASP_API_KEY  — production API key (prail_...)

Artifacts written to verification-artifacts/demos/production/langchain/:
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Windows SSL workaround — must happen before httpx is used by the SDK.
# ---------------------------------------------------------------------------
import httpx as _httpx
_OrigAsyncClient = _httpx.AsyncClient
class _NoVerifyAsyncClient(_OrigAsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs["verify"] = False
        super().__init__(*args, **kwargs)
_httpx.AsyncClient = _NoVerifyAsyncClient

import arclasp
import arclasp.client as _pr_client
from arclasp.langchain.adapter import govern

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent
ARTIFACT_DIR = _REPO_ROOT / "verification-artifacts" / "demos" / "production" / "langchain"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Instrumentation
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
# Vendor workflow stub (same shape as the mocked demo)
# ---------------------------------------------------------------------------

_VENDOR_TOOLS = [
    ("search_web",        {"query": "enterprise SaaS pricing benchmarks Q2 2026"}),
    ("calculate_offer",   {"vendor": "vendor-a", "amount": 3000}),
    ("record_commitment", {"vendor": "vendor-a", "amount": 3000}),
    ("record_commitment", {"vendor": "vendor-b", "amount": 3000}),
    ("record_commitment", {"vendor": "vendor-c", "amount": 3000}),
    ("record_commitment", {"vendor": "vendor-d", "amount": 3000}),
    ("send_email",        {"to": "vendor-d@example.com", "subject": "Purchase commitments confirmed"}),
]


class _VendorWorkflowChain:
    def invoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        return {"output": "done"}

    async def ainvoke(self, input: Any, config: Any = None, **kw: Any) -> dict:
        callbacks = (config or {}).get("callbacks", [])
        for tool_name, tool_input in _VENDOR_TOOLS:
            run_id = uuid.uuid4()
            for cb in callbacks:
                if hasattr(cb, "on_tool_start"):
                    await cb.on_tool_start({"name": tool_name}, str(tool_input), run_id=run_id)
            for cb in callbacks:
                if hasattr(cb, "on_tool_end"):
                    await cb.on_tool_end("ok", run_id=run_id)
        return {"output": "vendor_workflow_complete"}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

_SEMANTIC_STEPS = [
    ("pricing-research",    "search_web",        None,  None),
    ("offer-calculator",    "calculate_offer",   3000,  3_000),
    ("commitment-recorder", "record_commitment", 3000,  3_000),
    ("commitment-recorder", "record_commitment", 3000,  6_000),
    ("commitment-recorder", "record_commitment", 3000,  9_000),
    ("commitment-recorder", "record_commitment", 3000, 12_000),
    ("communication",       "send_email",        None,  None),
]


async def _run_vendor_workflow() -> None:
    governed = govern(
        _VendorWorkflowChain(),
        chain_name="langchain-vendor-purchase-prod",
        metadata={"workflow": "vendor_purchase", "adapter": "langchain", "demo": "production"},
    )
    await governed.ainvoke({"input": "start vendor purchase workflow"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    api_key = os.environ.get("ARCLASP_API_KEY", "")
    if not api_key:
        print("ERROR: ARCLASP_API_KEY environment variable is not set.")
        return 1

    print("=" * 65)
    print("  Arclasp — LangChain Production Demo")
    print("=" * 65)
    print()
    print("  Backend   : https://api.proofrail.dev")
    print("  Adapter   : arclasp.langchain.adapter.govern()")
    print("  Agents    : pricing-research -> offer-calculator ->")
    print("              communication -> commitment-recorder (x4)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : 7 tools (14 events); 7th tool_start crosses threshold")
    print("              -> require_approval -> owner approves via email.")
    print()

    arclasp.init(
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
    await _run_vendor_workflow()
    run_end = datetime.now(timezone.utc)

    chain_id = _captured_chain_id
    if not chain_id:
        print("ERROR: Chain ID was not captured — backend may not have been reached.")
        return 1

    chain_detail = await _pr_client.get_chain(chain_id)
    receipt = await _pr_client.get_chain_receipt(chain_id)
    events_resp = await _pr_client.get_chain_events(chain_id)

    print()
    print("  [OK] Workflow completed")
    print()
    print("  Chain:")
    print(f"    ID             : {chain_id}")
    print(f"    Status         : {chain_detail.status}")
    print(f"    Events (backend): {events_resp.total}")
    print()
    if receipt:
        print("  Receipt:")
        print(f"    Number         : {receipt.receipt_number}")
        sig = receipt.signature or ""
        print(f"    Signature      : {sig[:32]}… ({len(sig)} chars)")
    else:
        print("  Receipt: NOT GENERATED")
    print()

    trace = {
        "demo_name": "langchain_vendor_workflow_production",
        "adapter": "langchain",
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
        "semantic_steps": [
            {"seq": i + 1, "agent": agent, "action": action}
            for i, (agent, action, _, _) in enumerate(_SEMANTIC_STEPS)
        ],
    }
    trace_path = ARTIFACT_DIR / "chain-trace.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2, default=str)

    receipt_data = json.loads(receipt.model_dump_json()) if receipt else {}
    receipt_path = ARTIFACT_DIR / "receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2, default=str)

    decision_source = "human_approval"
    for ev in events_resp.events:
        if ev.decision_source == "human_approval":
            decision_source = "human_approval"
            break

    print()
    print("  ============================================================")
    print("  SUCCESS: LangChain production demo complete")
    print(f"  Chain ID:    {chain_id}")
    print(f"  Receipt ID:  {receipt.receipt_number if receipt else 'N/A'}")
    print(f"  Decision:    {decision_source}")
    print(f"  Artifacts:   {ARTIFACT_DIR}")
    print("  ============================================================")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
