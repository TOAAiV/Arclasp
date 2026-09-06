"""
arclasp.models — Shared Pydantic models and data types.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from arclasp._constants import (
    DEFAULT_SENSITIVE_FIELD_PATTERNS,
    DEFAULT_SENSITIVE_VALUE_PATTERNS,
)


class AgentAction(BaseModel):
    """Represents a single action taken by an agent within a chain."""

    agent_name: str
    parent_agent_name: str | None = None
    action_type: str
    action_name: str
    action_payload: dict = Field(default_factory=dict)
    executed_at: datetime | None = None


class RemediationAction(BaseModel):
    """One bounded, customer-safe next step within a RemediationV1 snapshot."""

    code: str
    summary: str


class RemediationV1(BaseModel):
    """
    Structured, versioned remediation guidance snapshot (REM1 schema v1).

    Mirrors the backend's ``app.schemas.remediation.RemediationV1``. Guidance
    only — describes what compliant next step is available after a policy
    decision; never alters the decision itself and grants no authority.

    Populated only when the backend sends the ``remediation_v1`` field on a
    ChainEvent response (added by the REM1 backend batch). Older backends
    that don't send this field leave ``PolicyDecision.remediation_v1`` as
    ``None`` — see PolicyDecision.remediation for this SDK's pre-existing,
    unrelated plain-string remediation fallback.
    """

    schema_version: int
    outcome: str
    retryable: bool
    requires_human: bool
    actions: list[RemediationAction] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)


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

    # Where the decision was made. Public governed paths use backend_evaluation or human_approval; legacy compatibility values may still parse.
    decision_source: str = "backend_evaluation"

    # Present when a specific named policy triggered the decision
    policy_name: str | None = None

    # Kill-switch fields — set when the organisation kill switch halted the action
    kill_switch_active: bool = False
    pause_reason: str | None = None

    # Optional remediation guidance returned by the backend
    remediation: str | None = None
    docs_url: str | None = None

    # Structured, versioned remediation guidance (REM1 schema v1). Additive
    # and independent of the plain-string `remediation`/`docs_url` fields
    # above — None for backends that don't send it yet and for decisions
    # where remediation is trivially absent (e.g. a plain "allow").
    remediation_v1: RemediationV1 | None = None

    # Shadow-mode fields — populated when the org is running in shadow mode.
    # evaluation_mode: "enforce" | "shadow" | "disabled" | None (pre-feature events)
    # shadow_decision: the would-have-been decision when evaluation_mode == "shadow"
    evaluation_mode: str | None = None
    shadow_decision: str | None = None

    # Cost tracking — populated for events that include LLM token data; None otherwise.
    estimated_cost_usd: float | None = None

    # Auto-pause flag — True when this event caused the backend to halt the chain
    # due to a runaway-limit trigger.  The SDK raises ChainAutoPausedError when True.
    auto_paused: bool = False

    # Running chain totals after this event: financial_exposure_usd,
    # records_modified_count, external_communications_count, etc.
    # Empty dict on early-exit paths (kill switch, disabled mode).
    cumulative_metrics: dict = Field(default_factory=dict)


class ChainConfig(BaseModel):
    """
    Full configuration for a Arclasp SDK session.  Created by arclasp.init()
    and stored as a module-level singleton.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr

    environment: str = "production"
    backend_url: str = "https://api.arclasp.com"

    financial_approval_threshold_usd: float = 5000.0
    external_domains_allowlist: list[str] = Field(default_factory=list)
    high_risk_agents: list[str] = Field(default_factory=list)
    # Optional list of agent names considered registered in this org.
    # If provided, classify_risk applies +10 / "unregistered_agent" to any
    # agent NOT in this list. None = skip the check (default, backward-compatible).
    # Real-time registry sync from backend deferred to v2.1.
    registered_agents: list[str] | None = None
    default_approval_timeout_hours: int = 24
    fallback_approvers: list[str] = Field(default_factory=list)

    backend_timeout_seconds: int = 5
    # Drain timeout override.  None (default) = use the buffer-size formula:
    #   max(10.0, buffer_size * backend_timeout_seconds * 1.5).
    # Set to a positive integer to enforce a hard ceiling regardless of
    # buffer size — useful when the agent run has a known wall-clock budget.
    drain_timeout_seconds: int | None = None
    offline_buffer_max_events: int = 100

    # HTTP retry configuration.
    # max_retries: total number of retry attempts after the initial call fails.
    #   3 retries → up to 4 total attempts (1 initial + 3 retries).
    # retry_backoff_base_ms: base delay for exponential backoff.
    #   attempt 1 = base ms, attempt 2 = 2× base, attempt 3 = 4× base.
    #   Defaults give a 100 ms / 500 ms / 2 s cadence at base=100.
    max_retries: int = 3
    retry_backoff_base_ms: int = 100

    # Field-name patterns — any key matching (case-insensitive substring) is redacted.
    # Defaults are the complete v2 spec section 12 set.  Extend without replacing:
    #   arclasp.init(sensitive_field_patterns=[*DEFAULT_SENSITIVE_FIELD_PATTERNS, "my_secret"])
    sensitive_field_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SENSITIVE_FIELD_PATTERNS)
    )
    # Value-prefix patterns — any string value starting with one of these is
    # redacted regardless of its key name (catches embedded API keys).
    sensitive_value_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SENSITIVE_VALUE_PATTERNS)
    )
    max_payload_string_length: int = 1000

    cumulative_financial_threshold_usd: float = 10000.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------



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

    ``id`` is the receipt's backend UUID.  The current chain-receipt endpoint
    does not expose it; the field is ``None`` until the backend includes it.
    When present it can be passed to ``client.verify_receipt(receipt_id)`` to
    cryptographically confirm the receipt is untampered.
    """

    id: str | None = None  # backend UUID; included when available
    receipt_number: str
    summary: str | None = None
    structured_data: dict = Field(default_factory=dict)
    signature: str | None = None
    previous_receipt_hash: str | None = None
    created_at: datetime


class ReceiptVerifyResponse(BaseModel):
    """
    Response from the public GET /v1/receipts/{receipt_id}/verify endpoint.

    Deprecated compatibility response for the legacy public receipt verifier.

    ``valid`` means the backend re-derived the server-side HMAC successfully.
    It is not independent or offline verification.
    """

    valid: bool
    receipt_number: str
    chain_id: str
    generated_at: str


class VerificationLayerStatus(BaseModel):
    """Status for a single verification layer in v2 verification responses."""

    status: str
    reason_code: str | None = None


class AsymmetricSignatureStatus(BaseModel):
    """Approval signature status without raw signature or key material."""

    status: str
    key_status: str = "not_evaluated"
    reason_code: str | None = None


class TimestampAnchorStatuses(BaseModel):
    """Timestamp-anchor status summaries without raw proof blobs."""

    freetsa: VerificationLayerStatus
    opentimestamps: VerificationLayerStatus


class EvidenceChainStatus(BaseModel):
    """Receipt-chain continuity status for v2 verification."""

    status: str
    reason_code: str | None = None


class HistoricalStateStatus(BaseModel):
    """Historical-recovery/adjudication context for composed verification."""

    status: str
    reason_code: str | None = None


class VerificationLayers(BaseModel):
    """Detailed v2 verification layer breakdown when the backend exposes it."""

    compatibility_integrity: VerificationLayerStatus
    compatibility_chain: VerificationLayerStatus
    compatibility_binding: VerificationLayerStatus
    v2_payload_shape: VerificationLayerStatus
    v2_artifact_hash: VerificationLayerStatus
    asymmetric_signature: AsymmetricSignatureStatus
    v2_evidence_chain: VerificationLayerStatus
    issuance_provenance: VerificationLayerStatus
    cutover_manifest: VerificationLayerStatus
    recovery_authorization: VerificationLayerStatus
    receipt_ordering: VerificationLayerStatus
    historical_state: HistoricalStateStatus


class LegacyHmacDiagnostic(BaseModel):
    """Non-authoritative HMAC compatibility diagnostic for approval evidence."""

    status: str
    authoritative: bool = False
    reason_code: str | None = None


class EvidenceStatusFact(BaseModel):
    """Public-safe post-issuance governance fact."""

    action: str
    reason_code: str
    public_summary: str | None = None
    issued_at: datetime | str
    status_sequence: int
    artifact_hash_sha256: str
    signing_key_id: str


class EvidenceStatusCurrentStatus(BaseModel):
    """Current governance state independent of cryptographic verification."""

    reliance_status: str
    dispute_status: str
    correction_status: str
    correction_count: int


class EvidenceStatusCurrentFacts(BaseModel):
    """Current bounded governance facts exposed by verification responses."""

    revocation: EvidenceStatusFact | None = None
    open_dispute: EvidenceStatusFact | None = None
    latest_dispute_resolution: EvidenceStatusFact | None = None
    latest_correction: EvidenceStatusFact | None = None


class PublicEvidenceStatus(BaseModel):
    """Public-token post-issuance governance status."""

    status_proof: str
    reason_code: str
    history_count: int
    current_status: EvidenceStatusCurrentStatus | None = None
    current_facts: EvidenceStatusCurrentFacts


class AuthenticatedEvidenceStatus(PublicEvidenceStatus):
    """Authenticated governance status with projection health diagnostics."""

    projection_status: str
    projection_reason_code: str | None = None


class ChainMetadata(BaseModel):
    """Safe chain metadata returned by authenticated v2 verification."""

    id: str
    external_id: str | None = None
    name: str | None = None


class VerificationArtifact(BaseModel):
    """Safe artifact metadata returned by authenticated v2 verification."""

    type: str
    version: str
    id: str
    created_at: datetime | None = None
    chain: ChainMetadata
    receipt_id: str | None = None
    receipt_number: str | None = None
    generated_at: datetime | str | None = None
    issuance_mode: str | None = None


class VerificationStatus(BaseModel):
    """Role-aware v2 verification result without raw evidence fields."""

    overall_status: str
    reason_code: str | None = None
    integrity: VerificationLayerStatus
    asymmetric_signature: AsymmetricSignatureStatus
    timestamp_anchors: TimestampAnchorStatuses
    evidence_chain: EvidenceChainStatus
    verified_at: datetime
    layers: VerificationLayers | None = None
    legacy_hmac_diagnostic: LegacyHmacDiagnostic | None = None


class ApprovalDecisionContext(BaseModel):
    """Member-visible approval decision summary."""

    outcome: str | None = None
    decided_at: datetime | str | None = None
    reason_code: str | None = None


class SafeActionContext(BaseModel):
    """Member-visible action labels, excluding payloads."""

    agent_name: str | None = None
    action_type: str | None = None
    action_name: str | None = None


class MemberVerificationContext(BaseModel):
    """Member-visible v2 verification context."""

    approval_decision: ApprovalDecisionContext | None = None
    action: SafeActionContext | None = None
    receipt_integrity_semantics: str | None = None


class AdminApproverContext(BaseModel):
    """Admin-visible actor context returned by authenticated v2 verification."""

    user_id: str | None = None
    membership_id: str | None = None
    email: str | None = None
    name: str | None = None
    role: str | None = None


class AdminPolicyContext(BaseModel):
    """Admin-visible policy summary returned by authenticated v2 verification."""

    policy_id: str | None = None
    policy_name: str | None = None
    policy_version: int | str | None = None
    rule_id: str | None = None
    reason: str | None = None


class AdminReviewContext(BaseModel):
    """Sanitized admin review context returned by authenticated v2 verification."""

    source: str
    reason: str | None = None
    expires_at: datetime | str | None = None
    action: SafeActionContext | None = None
    money: dict = Field(default_factory=dict)


class AdminVerificationContext(BaseModel):
    """Admin-only allowlisted v2 verification context."""

    approver: AdminApproverContext | None = None
    decision_notes: str | None = None
    review_context: AdminReviewContext | None = None
    policy: AdminPolicyContext | None = None
    audit_actor: AdminApproverContext | None = None


class VerificationCapabilities(BaseModel):
    """Capabilities attached to authenticated v2 verification responses."""

    can_view_identity_context: bool
    can_download_admin_evidence: bool
    admin_downloads: dict = Field(default_factory=dict)


class AuthenticatedVerificationResponse(BaseModel):
    """Authenticated, organization-scoped v2 verification response."""

    verification_version: str = "2"
    artifact: VerificationArtifact
    verification: VerificationStatus
    context: MemberVerificationContext
    capabilities: VerificationCapabilities
    admin_context: AdminVerificationContext | None = None
    evidence_status: AuthenticatedEvidenceStatus | None = None


class PublicVerificationStatus(BaseModel):
    """Minimized public v2 verification status."""

    status: str
    reason_code: str | None = None
    key_status: str | None = None


class PublicVerificationResponse(BaseModel):
    """Minimized tokenized public verification response."""

    verification_version: str
    overall_status: str | None = None
    artifact_type: str | None = None
    artifact_version: str | None = None
    issuance_mode: str | None = None
    integrity: PublicVerificationStatus | None = None
    asymmetric_signature: PublicVerificationStatus | None = None
    layers: dict[str, PublicVerificationStatus] | None = None
    timestamp_anchors: dict | None = None
    verified_at: datetime | str | None = None
    reason_code: str | None = None
    legacy_hmac_diagnostic: LegacyHmacDiagnostic | None = None
    evidence_status: PublicEvidenceStatus | None = None


class PublicVerificationTokenMetadata(BaseModel):
    """Public verification token metadata; excludes plaintext token and hash."""

    id: str
    organization_id: str
    artifact_type: str
    artifact_id: str
    status: str
    created_at: datetime | str
    created_by_user_id: str | None = None
    created_by_membership_id: str | None = None
    expires_at: datetime | str | None = None
    revoked_at: datetime | str | None = None
    revoked_by_user_id: str | None = None
    revoked_by_membership_id: str | None = None
    revocation_reason: str | None = None


class PublicVerificationTokenCreateResponse(PublicVerificationTokenMetadata):
    """Token issue response; plaintext token is returned exactly once."""

    token: str
    public_path: str


class PublicVerificationTokenListResponse(BaseModel):
    """Paginated public verification token list response."""

    organization_id: str
    tokens: list[PublicVerificationTokenMetadata]
    total: int
    limit: int
    offset: int

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
