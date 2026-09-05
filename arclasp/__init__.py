"""
arclasp — AI Agent Governance Layer SDK.

Quick start:
    import arclasp

    arclasp.init(api_key="prail_...")

    async with arclasp.Chain("my-agent-workflow") as chain:
        await chain.record_agent_action(
            agent_name="my-agent",
            action_type="tool_call",
            action_name="send_email",
            payload={"to": "user@example.com"},
        )
"""

from arclasp.chain import Chain
from arclasp.client import (
    init,
    verify_approval_v2,
    verify_public_token,
    verify_receipt_v2,
)
from arclasp.exceptions import (
    ActionDeniedError,
    ArclaspPolicyError,
    BackendUnavailableError,
    ChainCompletionError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ArclaspKillSwitchError,
    ArclaspVerificationError,
)

__version__ = "0.1.0b1"

__all__ = [
    "init",
    "verify_approval_v2",
    "verify_public_token",
    "verify_receipt_v2",
    "Chain",
    "ArclaspPolicyError",
    "ActionDeniedError",
    "BackendUnavailableError",
    "ChainCompletionError",
    "ChainAutoPausedError",
    "ChainTimeoutError",
    "ArclaspKillSwitchError",
    "ArclaspVerificationError",
    "__version__",
]
