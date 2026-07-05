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
from proofrail.client import init
from proofrail.exceptions import (
    ActionDeniedError,
    BackendUnavailableError,
    ChainAutoPausedError,
    ChainTimeoutError,
    PolicyViolationError,
    ProofRailKillSwitchError,
)

__version__ = "0.1.0a7"

__all__ = [
    "init",
    "Chain",
    "ActionDeniedError",
    "BackendUnavailableError",
    "ChainAutoPausedError",
    "ChainTimeoutError",
    "PolicyViolationError",
    "ProofRailKillSwitchError",
    "__version__",
]
