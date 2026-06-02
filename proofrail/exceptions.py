"""
proofrail.exceptions — SDK exception hierarchy.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Remediation lookup keyed on policy_name (v2 spec section 7)
# ---------------------------------------------------------------------------

# Values are (remediation_string, docs_url).  Used when the backend doesn't
# return these fields so the error still gives the operator actionable guidance.
_POLICY_REMEDIATION: dict[str, tuple[str, str]] = {
    "cumulative_financial_threshold": (
        "Update `financial_approval_threshold_usd` in init(), or approve via dashboard.",
        "https://docs.proofrail.ai/policies/thresholds",
    ),
    "unauthorized_domain": (
        "Add the domain to `external_domains_allowlist` in init(), or approve via dashboard.",
        "https://docs.proofrail.ai/policies/domains",
    ),
    "bulk_operation": (
        "Reduce the operation batch size, or request approval via dashboard.",
        "https://docs.proofrail.ai/policies/bulk-operations",
    ),
    "high_risk_agent": (
        "Remove the agent from `high_risk_agents` in init(), or approve via dashboard.",
        "https://docs.proofrail.ai/policies/high-risk-agents",
    ),
    "unapproved_llm_model": (
        "Add the model to the approved-models list in init(), or approve via dashboard.",
        "https://docs.proofrail.ai/policies/llm-models",
    ),
    "pii_exposure": (
        "Add sensitive field names to `sensitive_field_patterns` in init() to redact them.",
        "https://docs.proofrail.ai/policies/pii",
    ),
    "approval_timeout": (
        "Increase `default_approval_timeout_hours` in init(), or pre-approve the action.",
        "https://docs.proofrail.ai/policies/approvals",
    ),
    "human_approval_denied": (
        "The action was denied by a human approver. Review the denial reason "
        "(in the 'condition' field) and adjust the action or talk to your approver. "
        "Repeated denials of similar actions may indicate the policy needs tuning.",
        "https://docs.proofrail.ai/policies/approvals/denials",
    ),
}


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class ProofRailPolicyError(Exception):
    """
    Common base for all policy-denial exceptions raised by the SDK.

    Both :class:`ActionDeniedError` (raised when the backend returns a
    ``"deny"`` decision) and :class:`PolicyViolationError` (raised when a
    local fast-path check blocks an action) carry the same six diagnostic
    fields.  This base class holds them so callers can catch either with a
    single ``except ProofRailPolicyError`` clause while still being able to
    distinguish the two if needed.

    Attributes
    ----------
    message : str
        Human-readable summary of why the action was denied.
    policy_name : str | None
        The name of the policy rule that triggered the denial.
    condition : str | None
        The specific condition that was violated, as returned by the backend.
    chain_context : dict | None
        Snapshot of chain state at the time of denial (chain_id, sequence, …).
    remediation : str | None
        Actionable guidance for fixing the configuration or getting approval.
    docs_url : str | None
        Link to the relevant policy documentation.
    decision_source : str | None
        Where the decision originated: ``"backend_evaluation"``,
        ``"local_fast_path"``, or ``"offline_stub"``.
    """

    def __init__(
        self,
        message: str,
        policy_name: str | None = None,
        condition: str | None = None,
        chain_context: dict | None = None,
        remediation: str | None = None,
        docs_url: str | None = None,
        decision_source: str | None = None,
    ) -> None:
        self.message = message
        self.policy_name = policy_name
        self.condition = condition
        self.chain_context = chain_context
        self.remediation = remediation
        self.docs_url = docs_url
        self.decision_source = decision_source
        super().__init__(str(self))

    def __str__(self) -> str:
        name = type(self).__name__
        lines = [f"{name}: {self.message}"]
        if self.policy_name:
            lines.append(f"  Policy      : {self.policy_name}")
        if self.condition:
            lines.append(f"  Condition   : {self.condition}")
        if self.chain_context:
            lines.append(f"  Chain       : {self.chain_context}")
        if self.decision_source:
            lines.append(f"  Source      : {self.decision_source}")
        if self.remediation:
            lines.append(f"  Remediation : {self.remediation}")
        if self.docs_url:
            lines.append(f"  Docs        : {self.docs_url}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Policy-denial subclasses
# ---------------------------------------------------------------------------

class ActionDeniedError(ProofRailPolicyError):
    """
    Raised when the backend policy engine returns a ``"deny"`` decision for an
    agent action.  Carries structured context so operators can surface a clear
    error message or take remediation steps.

    Catch :class:`ProofRailPolicyError` instead when you want to handle both
    backend denials and local fast-path violations uniformly.
    """


class PolicyViolationError(ProofRailPolicyError):
    """
    Raised when an agent action is blocked by a local fast-path check before
    (or instead of) a backend round-trip, e.g. when the SDK's local policy
    evaluation rejects the action immediately.

    Catch :class:`ProofRailPolicyError` instead when you want to handle both
    backend denials and local fast-path violations uniformly.
    """


# ---------------------------------------------------------------------------
# Transport / lifecycle exceptions
# ---------------------------------------------------------------------------

class BackendUnavailableError(Exception):
    """
    Raised when the ProofRail backend cannot be reached and fail_mode is
    ``"deny"``.  Carries the original failure message and the configured
    fail_mode for context.
    """

    def __init__(self, message: str, fail_mode: str = "deny") -> None:
        self.message = message
        self.fail_mode = fail_mode
        super().__init__(f"{message} (fail_mode={fail_mode})")


class ChainTimeoutError(Exception):
    """
    Raised when a chain exceeds the configured approval timeout without a
    resolution, or when the approval polling loop times out.
    """

    def __init__(self, chain_id: str, timeout_seconds: int) -> None:
        self.chain_id = chain_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"chain '{chain_id}' timed out after {timeout_seconds}s"
        )


class ProofRailKillSwitchError(Exception):
    """
    Raised when the organisation's kill switch is active and an agent action
    is attempted.  The kill switch is evaluated at the very top of the policy
    engine — before shadow-mode or any other rule — so it always takes effect
    regardless of policy configuration.

    Attributes
    ----------
    organization_id : str | None
        The organisation for which the kill switch is active.
    reason : str | None
        The human-readable reason recorded when the kill switch was activated,
        if the backend returns one.
    """

    def __init__(
        self,
        message: str = "All agent actions are denied: organisation kill switch is active",
        organization_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.message = message
        self.organization_id = organization_id
        self.reason = reason
        super().__init__(str(self))

    def __str__(self) -> str:
        lines = [f"ProofRailKillSwitchError: {self.message}"]
        if self.organization_id:
            lines.append(f"  Organisation : {self.organization_id}")
        if self.reason:
            lines.append(f"  Reason       : {self.reason}")
        return "\n".join(lines)


class ChainAutoPausedError(ProofRailPolicyError):
    """
    Raised when the backend reports ``auto_paused=True`` on a chain event
    response.  This means the chain has been halted by the backend's runaway-
    action limiter — no further events can be recorded until an operator
    resumes or explicitly terminates the chain.

    Catching this exception lets callers stop processing gracefully and surface
    a clear error rather than receiving cascading ``409 CONFLICT`` responses
    from every subsequent ``record_agent_action`` call.

    Attributes
    ----------
    chain_id : str | None
        The chain that was auto-paused.  Use this to construct the resume URL:
        ``POST /v1/chains/{chain_id}/resume``.
    reason : str | None
        The decision reason returned by the backend for the triggering event,
        if the backend included one.
    """

    def __init__(
        self,
        message: str = (
            "Chain has been auto-paused by the backend due to a runaway-limit trigger. "
            "No further events can be recorded until the chain is resumed."
        ),
        chain_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.chain_id = chain_id
        self.reason = reason
        resume_hint = (
            f"POST /v1/chains/{chain_id}/resume to resume the chain, "
            "or contact your org admin to investigate the runaway trigger."
            if chain_id
            else (
                "POST /v1/chains/{chain_id}/resume to resume the chain, "
                "or contact your org admin to investigate the runaway trigger."
            )
        )
        super().__init__(
            message=message,
            policy_name="auto_pause",
            condition=reason,
            chain_context={"chain_id": chain_id} if chain_id else None,
            remediation=resume_hint,
            docs_url="https://docs.proofrail.ai/policies/runaway-limits",
            decision_source="backend_evaluation",
        )
