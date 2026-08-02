"""
proofrail — AI Agent Governance Layer SDK.

Quick start:
    import proofrail

    proofrail.init(api_key="prail_...")

    async with proofrail.Chain("my-agent-workflow") as chain:
        await chain.record_agent_action(
            agent_name="my-agent",
            action_type="tool_call",
            action_name="send_email",
            payload={"to": "user@example.com"},
        )
"""

from proofrail.chain import Chain
from proofrail.client import (
    init,
    issue_public_verification_token,
    list_public_verification_tokens,
    revoke_public_verification_token,
    verify_approval_v2,
    verify_public_token,
    verify_receipt,
    verify_receipt_v2,
)
from proofrail.exceptions import (
    ActionDeniedError,
    BackendUnavailableError,
    ChainAutoPausedError,
    ChainTimeoutError,
    PolicyViolationError,
    ProofRailKillSwitchError,
    ProofRailVerificationError,
)

__version__ = "0.1.0a8"

__all__ = [
    "init",
    "issue_public_verification_token",
    "list_public_verification_tokens",
    "revoke_public_verification_token",
    "verify_approval_v2",
    "verify_public_token",
    "verify_receipt",
    "verify_receipt_v2",
    "Chain",
    "ActionDeniedError",
    "BackendUnavailableError",
    "ChainAutoPausedError",
    "ChainTimeoutError",
    "PolicyViolationError",
    "ProofRailKillSwitchError",
    "ProofRailVerificationError",
    "__version__",
]
