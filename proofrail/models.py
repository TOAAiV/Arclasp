"""
proofrail.models — Shared Pydantic models and data types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentAction(BaseModel):
    """Represents a single action taken by an agent within a chain."""

    agent_name: str
    parent_agent_name: str | None = None
    action_type: str
    action_name: str
    action_payload: dict = Field(default_factory=dict)
    executed_at: datetime | None = None


class PolicyDecision(BaseModel):
    """
    The governance decision returned by the backend for an agent action.

    Field names match the backend wire format.  Instantiate via
    ``PolicyDecision.model_validate(response_dict)`` at the point where a
    backend response dict is first consumed.
    """

    # Core decision — one of: allow | allow_with_flag | require_approval | deny
    policy_decision: str

    # Human-readable reason for the decision (empty string when not provided)
    decision_reason: str = ""

    # Where the decision was made: backend_evaluation | local_fast_path | offline_stub
    decision_source: str = "backend_evaluation"

    # Present when a specific named policy triggered the decision
    policy_name: str | None = None

    # Kill-switch fields — set when the organisation kill switch halted the action
    kill_switch_active: bool = False
    pause_reason: str | None = None

    # Optional remediation guidance returned by the backend
    remediation: str | None = None
    docs_url: str | None = None


class ChainConfig(BaseModel):
    """
    Full configuration for a ProofRail SDK session.  Created by proofrail.init()
    and stored as a module-level singleton.
    """

    # --- Required ---
    api_key: str

    # --- Environment ---
    environment: str = "production"
    backend_url: str = "http://localhost:8000"

    # --- Policy thresholds ---
    financial_approval_threshold_usd: float = 5000.0
    external_domains_allowlist: list[str] = Field(default_factory=list)
    high_risk_agents: list[str] = Field(default_factory=list)
    default_approval_timeout_hours: int = 24
    fallback_approvers: list[str] = Field(default_factory=list)

    # --- Failure behaviour ---
    fail_mode: str = "deny"           # "deny" | "allow" — global default
    # Per-action-class overrides.  Keys are action_type strings (e.g.
    # "tool_call", "llm_inference"); values are "deny" or "allow".
    # When an action_type is present here it takes precedence over fail_mode.
    fail_modes: dict[str, str] = Field(default_factory=dict)
    backend_timeout_seconds: int = 5

    # --- Offline / buffering ---
    offline_buffer_max_events: int = 100

    # --- Sanitization ---
    sensitive_field_patterns: list[str] = Field(
        default_factory=lambda: ["api_key", "password", "secret", "token"]
    )
    max_payload_string_length: int = 1000

    # --- Local optimisations ---
    enable_local_fast_path: bool = True
    cumulative_financial_threshold_usd: float = 10000.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def resolve_fail_mode(self, action_type: str | None = None) -> str:
        """
        Return the effective fail_mode for a given *action_type*.

        Lookup order:

        1. ``fail_modes[action_type]`` — per-class override (if key exists).
        2. ``fail_mode``              — global default.

        Parameters
        ----------
        action_type : str | None
            The ``action_type`` of the event being recorded (e.g.
            ``"tool_call"``, ``"llm_inference"``).  Pass ``None`` to get the
            global default without any per-class lookup.

        Returns
        -------
        str
            ``"deny"`` or ``"allow"``.
        """
        if action_type and action_type in self.fail_modes:
            return self.fail_modes[action_type]
        return self.fail_mode


# ---------------------------------------------------------------------------
# SDK read-endpoint response mirror types
#
# These mirror the backend's response schemas.  Defined here (not imported
# from the backend package) so the SDK remains independently installable.
# ---------------------------------------------------------------------------

class ChainDetail(BaseModel):
    """
    Full chain detail returned by the GET /v1/chains/{chain_id} endpoint.
    Accessed via ``client.get_chain()`` or ``chain.detail()``.
    """
    id: str
    organization_id: str
    external_chain_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    agents_involved: list = Field(default_factory=list)
    cumulative_metrics: dict = Field(default_factory=dict)
    environment: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ChainEventDetail(BaseModel):
    """Single event record from GET /v1/chains/{chain_id}/events."""
    id: str
    sequence_number: int
    agent_name: str
    parent_agent_name: str | None = None
    action_type: str
    action_name: str
    action_payload: dict = Field(default_factory=dict)
    action_result: dict | None = None
    risk_classification: dict = Field(default_factory=dict)
    policy_decision: str
    decision_reason: str | None = None
    decision_source: str
    evaluation_mode: str | None = None
    executed_at: datetime | None = None
    created_at: datetime


class ChainEventsResponse(BaseModel):
    """Paginated event list from GET /v1/chains/{chain_id}/events."""
    events: list[ChainEventDetail]
    total: int
    limit: int
    offset: int


class ChainReceiptResponse(BaseModel):
    """
    Audit receipt from GET /v1/chains/{chain_id}/receipt.
    Accessed via ``client.get_chain_receipt()`` or ``chain.receipt()``.
    """
    receipt_number: str
    summary: str | None = None
    structured_data: dict = Field(default_factory=dict)
    signature: str | None = None
    previous_receipt_hash: str | None = None
    created_at: datetime


class ChainSummary(BaseModel):
    """Lightweight chain entry in the list returned by GET /v1/chains."""
    id: str
    external_chain_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    environment: str
    agents_involved: list = Field(default_factory=list)
    event_count: int = 0


class ChainListResponse(BaseModel):
    """Paginated chain list from GET /v1/chains."""
    chains: list[ChainSummary]
    total: int
    limit: int
    offset: int
