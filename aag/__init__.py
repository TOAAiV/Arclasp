"""
aag — AI Agent Governance Layer SDK.

Quick start:
    import aag

    aag.init(api_key="aag_...")

    async with aag.Chain("my-agent-workflow") as chain:
        await chain.record_agent_action(
            agent_name="my-agent",
            action_type="tool_call",
            action_name="send_email",
            payload={"to": "user@example.com"},
        )
"""

from aag.chain import Chain
from aag.client import init
from aag.exceptions import (
    ActionDeniedError,
    BackendUnavailableError,
    ChainTimeoutError,
    PolicyViolationError,
    ProofRailKillSwitchError,
)

__version__ = "0.1.0"

__all__ = [
    "init",
    "Chain",
    "ActionDeniedError",
    "BackendUnavailableError",
    "ChainTimeoutError",
    "PolicyViolationError",
    "ProofRailKillSwitchError",
    "__version__",
]
