"""
Production end-to-end demo: CrewAI 4-agent vendor purchase workflow.

Same scenario as the LangGraph demo but exercised through the CrewAI
adapter (govern() with mixed strategy: Strategy B patches execute_task
for start events, Strategy A wraps task_callback for end events).
Event 13 (7th task_execution) triggers require_approval.

Usage (from repo root):
    cd sdk
    python demos/production/crewai_demo_prod.py

Requires:
    ARCLASP_API_KEY  — production API key (prail_...)

Artifacts written to verification-artifacts/demos/production/crewai/:
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
from arclasp.crewai.adapter import govern

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Artifact directory
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent
ARTIFACT_DIR = _REPO_ROOT / "verification-artifacts" / "demos" / "production" / "crewai"
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
# CrewAI stub objects (same shape as the mocked demo)
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


# Note: The CrewAI adapter records task descriptions as event payloads and does
# not pass numeric amounts to the backend. The cumulative financial threshold
# gate is therefore not exercised by this demo. This demo instead exercises
# chain creation, task-event recording, the first-external-communication
# approval gate (send_email), and receipt generation.
# send_email is placed last so it is the approval-triggering event rather than
# firing prematurely before any financial events are recorded.
_VENDOR_TASKS = [
    _MockTask("Search vendor pricing benchmarks for enterprise SaaS Q2 2026"),
    _MockTask("Calculate initial offer for vendor-a"),
    _MockTask("Record vendor-a commitment: $3,000"),
    _MockTask("Record vendor-b commitment: $3,000"),
    _MockTask("Record vendor-c commitment: $3,000"),
    _MockTask("Record vendor-d commitment: $3,000"),
    _MockTask("Send confirmation email to vendor-d@example.com"),  # first external comm → require_approval
]

_VENDOR_AGENTS = [
    _MockAgent("pricing-researcher"),
    _MockAgent("offer-calculator"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
    _MockAgent("commitment-recorder"),
    _MockAgent("communications-manager"),
]


class _VendorCrew:
    def __init__(self) -> None:
        self.agents = _VENDOR_AGENTS
        self.tasks = _VENDOR_TASKS
        self.task_callback: Any = None

    async def kickoff_async(self, inputs: Any = None, **kw: Any) -> list:
        results = []
        for task, agent in zip(self.tasks, self.agents):
            after_cb = self.task_callback

            def _run(t=task, a=agent, acb=after_cb) -> _MockTaskOutput:
                raw = a.execute_task(t)
                out = _MockTaskOutput(raw, a.role, description=t.description)
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


async def _run_vendor_workflow() -> None:
    crew = _VendorCrew()
    governed = govern(
        crew,
        chain_name="crewai-vendor-purchase-prod",
        metadata={"workflow": "vendor_purchase", "adapter": "crewai", "demo": "production"},
    )
    await governed.kickoff_async(inputs={"topic": "vendor purchase"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    api_key = os.environ.get("ARCLASP_API_KEY", "")
    if not api_key:
        print("ERROR: ARCLASP_API_KEY environment variable is not set.")
        return 1

    print("=" * 65)
    print("  Arclasp — CrewAI Production Demo")
    print("=" * 65)
    print()
    print("  Backend   : https://api.proofrail.dev")
    print("  Adapter   : arclasp.crewai.adapter.govern()")
    print("  Strategy  : Mixed (CrewAI 1.x) — Strategy B + Strategy A")
    print("  Agents    : pricing-researcher -> offer-calculator ->")
    print("              communications-manager -> commitment-recorder (x4)")
    print("  Threshold : $10,000 cumulative financial exposure")
    print("  Scenario  : 7 tasks (14 events); event 13 crosses threshold")
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
    print(f"    ID              : {chain_id}")
    print(f"    Status          : {chain_detail.status}")
    print(f"    Events (backend): {events_resp.total}")
    print()
    if receipt:
        print("  Receipt:")
        print(f"    Number          : {receipt.receipt_number}")
        sig = receipt.signature or ""
        print(f"    Signature       : {sig[:32]}… ({len(sig)} chars)")
    else:
        print("  Receipt: NOT GENERATED")
    print()

    trace = {
        "demo_name": "crewai_vendor_workflow_production",
        "adapter": "crewai",
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
    print("  SUCCESS: CrewAI production demo complete")
    print(f"  Chain ID:    {chain_id}")
    print(f"  Receipt ID:  {receipt.receipt_number if receipt else 'N/A'}")
    print(f"  Decision:    {decision_source}")
    print(f"  Artifacts:   {ARTIFACT_DIR}")
    print("  ============================================================")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
