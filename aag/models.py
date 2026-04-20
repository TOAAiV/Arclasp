"""
aag.models — Shared Pydantic models and data types.
"""

from __future__ import annotations

from datetime import datetime

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
    """The governance decision returned by the backend for an agent action."""

    decision: str   # allow | allow_with_flag | require_approval | deny
    reason: str
    source: str     # backend_evaluation | local_fast_path | offline_stub


class ChainConfig(BaseModel):
    """
    Full configuration for an aag SDK session.  Created by aag.init() and
    stored as a module-level singleton.
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
    fail_mode: str = "deny"           # "deny" | "allow"
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
