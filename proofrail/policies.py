"""
proofrail.policies — Reference implementation of the ProofRail policy engine.

This module is the open-source reference implementation of the algorithm
currently running in the closed-source ProofRail backend, extracted for
transparency and local testing.  It is intended to be read alongside
``backend/app/services/policy_engine.py`` — the two must stay in sync.

Pinning policy
--------------
The SDK implementation pins to the backend's *actual* behaviour, not to the
v2 specification's higher-level description.  Where the spec and the backend
diverge on specifics (e.g. the exact numeric thresholds that separate a flag
decision from an approval gate), this file follows the backend.  If you spot
a divergence that looks like a genuine bug rather than an intentional design
choice, open an issue at https://github.com/proofrail/proofrail-sdk before
sending a patch — we decide together which side is authoritative.

Audience
--------
External developers evaluating ProofRail can audit this file to understand
exactly what rules their agents are subject to.  Operators building custom
tooling (e.g. local CI policy checks, dashboard integrations) can call these
functions directly without spinning up the full backend.

Usage
-----
    from proofrail.policies import (
        classify_risk,
        update_chain_metrics_local,
        evaluate_policy,
        process_action_local,
    )

    risk = classify_risk(
        "tool_call", "send_email", {"to": "alice@example.com"}, "mailer-agent", {}
    )
    metrics = update_chain_metrics_local({}, "tool_call", "send_email", {}, risk)
    decision = evaluate_policy(
        "tool_call", "send_email", {}, "mailer-agent", risk, metrics, org_config
    )

Pipeline (matches backend policy_engine.py)
--------------------------------------------
    Stage 0  kill-switch check      → immediate deny if flag set
    Stage 1  classify_risk()        → risk_score (0-100) + category tags
    Stage 2  update_chain_metrics_local() → in-memory running totals
    Stage 3  evaluate_policy()      → final decision

See also
--------
``process_action_local`` — orchestrates all four stages, mirrors
``backend.app.services.policy_engine.process_action`` without DB calls.
"""

from __future__ import annotations

import logging

from proofrail._constants import DEFAULT_SENSITIVE_FIELD_PATTERNS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensitive field patterns — derived from the shared constant (v2 spec § 12)
# Sourced from _constants.py so sanitization.py and policies.py stay in sync.
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: frozenset[str] = frozenset(DEFAULT_SENSITIVE_FIELD_PATTERNS)


# ---------------------------------------------------------------------------
# Internal helpers — verbatim from backend policy_engine.py
# ---------------------------------------------------------------------------

def _extract_numeric(payload: dict, keys: list[str]) -> float | None:
    """Return the first numeric value found under any of *keys*, or None."""
    for key in keys:
        val = payload.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _has_sensitive_fields(payload: dict) -> bool:
    """Return True if any payload key contains a sensitive-field pattern."""
    for key in payload:
        key_lower = key.lower()
        if any(pat in key_lower for pat in _SENSITIVE_PATTERNS):
            return True
    return False


def _add_category(categories: list[str], category: str) -> None:
    """Append *category* to *categories* if not already present."""
    if category not in categories:
        categories.append(category)


def _decision(decision: str, reason: str) -> dict:
    """Return a canonical policy-decision dict with source="backend_evaluation"."""
    return {
        "decision": decision,
        "reason": reason,
        "source": "backend_evaluation",
    }


def _full_result(
    decision: str,
    reason: str,
    risk_classification: dict,
    evaluation_mode: str,
    shadow_decision: str | None = None,
    kill_switch_active: bool = False,
) -> dict:
    """Assemble the complete dict returned by :func:`process_action_local`.

    ``source`` is ``"sdk_reference_implementation"`` to distinguish these
    results from live backend evaluations in logs and dashboards.
    """
    return {
        "decision": decision,
        "reason": reason,
        "source": "sdk_reference_implementation",
        "risk_classification": risk_classification,
        "evaluation_mode": evaluation_mode,
        "shadow_decision": shadow_decision,
        "kill_switch_active": kill_switch_active,
    }


# ---------------------------------------------------------------------------
# Stage 1 — Risk classification
# ---------------------------------------------------------------------------

def classify_risk(
    action_type: str,
    action_name: str,
    payload: dict,
    agent_name: str,
    chain_context: dict,
) -> dict:
    """
    Score the risk of a single agent action.

    Synchronous port of ``backend.app.services.policy_engine.classify_risk``
    — the backend version is ``async`` only by signature; it has no awaits.

    Parameters
    ----------
    action_type : str
        Category of the action, e.g. ``"tool_call"``, ``"delete"``.
    action_name : str
        Specific action identifier, e.g. ``"send_email"``, ``"delete_record"``.
    payload : dict
        Arbitrary action data.  Keys are inspected for sensitive patterns and
        numeric financial values; values are not validated.
    agent_name : str
        Identifier of the agent performing the action.
    chain_context : dict
        Chain-level context.  Currently used for:
        ``high_risk_agents`` — list of agent names designated as high-risk.

    Returns
    -------
    dict
        ``{"risk_score": int, "categories": list[str], "reasons": list[str]}``

        ``risk_score`` is capped at 100.  ``categories`` contains zero or more
        of: ``"destructive"``, ``"write"``, ``"financial"``, ``"financial_high"``,
        ``"communication"``, ``"exfiltration"``, ``"privilege_escalation"``,
        ``"schema_change"``, ``"credential_exposure"``, ``"unregistered_agent"``.
    """
    risk_score = 0
    categories: list[str] = []
    reasons: list[str] = []

    atype = action_type.lower()
    aname = action_name.lower()

    # Delete operations (+40, "destructive")
    if atype == "delete" or "delete" in aname:
        risk_score += 40
        _add_category(categories, "destructive")
        reasons.append("Delete operation detected")

    # Write / modify operations (+20, "write")
    if atype == "write" or "write" in aname or "modify" in aname:
        risk_score += 20
        _add_category(categories, "write")
        reasons.append("Write/modify operation detected")

    # Financial value in payload
    amount = _extract_numeric(payload, ["amount", "value"])
    if amount is not None:
        if amount > 10_000:
            risk_score += 50
            _add_category(categories, "financial_high")
            reasons.append(f"High financial value detected: {amount:,.2f}")
        elif amount > 1_000:
            risk_score += 30
            _add_category(categories, "financial")
            reasons.append(f"Financial value detected: {amount:,.2f}")

    # Communication (+25, "communication")
    if any(k in aname for k in ("email", "send", "message")):
        risk_score += 25
        _add_category(categories, "communication")
        reasons.append("Communication action detected")

    # External data transfer / exfiltration risk (+20, "exfiltration")
    if "external" in aname or "url" in payload or "domain" in payload:
        risk_score += 20
        _add_category(categories, "exfiltration")
        reasons.append("External data transfer risk detected")

    # IAM / privilege operations (+40, "privilege_escalation")
    if any(k in aname for k in ("iam", "permission", "role", "access")):
        risk_score += 40
        _add_category(categories, "privilege_escalation")
        reasons.append("IAM/permission operation detected")

    # Schema / migration operations (+35, "schema_change")
    if "schema" in aname or "migration" in aname:
        risk_score += 35
        _add_category(categories, "schema_change")
        reasons.append("Schema/migration operation detected")

    # Credential exposure in payload (+50, "credential_exposure")
    if _has_sensitive_fields(payload):
        risk_score += 50
        _add_category(categories, "credential_exposure")
        reasons.append("Sensitive fields detected in payload")

    # Agent registry check (+10, "unregistered_agent")
    # Mirrors backend policy_engine.py: registered_agents flows in via chain_context.
    # When key is absent (no registry context provided), check is skipped — backward compatible.
    _registered: dict | None = chain_context.get("registered_agents")
    if _registered is not None:
        _agent_info = _registered.get(agent_name.lower().strip())
        if _agent_info is None:
            risk_score += 10
            _add_category(categories, "unregistered_agent")
            reasons.append(
                f"Agent '{agent_name}' is not registered for this organisation"
            )

    return {
        "risk_score": min(risk_score, 100),
        "categories": categories,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Stage 2 — Cumulative metrics updater (in-memory)
# ---------------------------------------------------------------------------

def update_chain_metrics_local(
    cumulative_metrics: dict,
    action_type: str,
    action_name: str,
    payload: dict,
    risk_classification: dict,
) -> dict:
    """
    Apply the same metric-accumulation logic as
    ``backend.app.services.policy_engine.update_chain_metrics``, but in-memory
    instead of writing to a database row.

    Parameters
    ----------
    cumulative_metrics : dict
        The current running totals for this chain.  **Never mutated** — a copy
        is taken at the start and the updated copy is returned.
    action_type, action_name, payload :
        The action being recorded.
    risk_classification : dict
        Output of :func:`classify_risk` for this action.

    Returns
    -------
    dict
        A new dict with updated totals.  The caller's ``cumulative_metrics``
        is unchanged.

    Notes
    -----
    Metrics are accumulated regardless of policy mode — this mirrors the
    backend's behaviour where ``update_chain_metrics`` is always called even
    in shadow mode.

    Tracked fields
    ~~~~~~~~~~~~~~
    - ``financial_exposure_usd``       — running sum of ``amount``/``value`` keys.
    - ``external_communications_count``— count of actions with "communication" category.
    - ``records_modified_count``       — count of actions with "write" category.
    - ``privileged_actions_count``     — count of actions with "privilege_escalation".
    - ``external_domains_contacted``   — deduplicated list of ``domain``/``url`` values.
    """
    metrics: dict = dict(cumulative_metrics)  # never mutate the caller's dict
    categories: list[str] = risk_classification.get("categories", [])

    amount = _extract_numeric(payload, ["amount", "value"])
    if amount is not None:
        metrics["financial_exposure_usd"] = (
            metrics.get("financial_exposure_usd", 0.0) + amount
        )

    if "communication" in categories:
        metrics["external_communications_count"] = (
            metrics.get("external_communications_count", 0) + 1
        )

    if "write" in categories:
        metrics["records_modified_count"] = (
            metrics.get("records_modified_count", 0) + 1
        )

    if "privilege_escalation" in categories:
        metrics["privileged_actions_count"] = (
            metrics.get("privileged_actions_count", 0) + 1
        )

    domain_or_url: str | None = payload.get("domain") or payload.get("url")
    if domain_or_url:
        existing_domains: list[str] = list(metrics.get("external_domains_contacted", []))
        domain_str = str(domain_or_url)
        if domain_str not in existing_domains:
            existing_domains.append(domain_str)
        metrics["external_domains_contacted"] = existing_domains

    return metrics


# ---------------------------------------------------------------------------
# Stage 3 — Policy evaluator
# ---------------------------------------------------------------------------

def evaluate_policy(
    action_type: str,
    action_name: str,
    payload: dict,
    agent_name: str,
    risk_classification: dict,
    cumulative_metrics: dict,
    org_config: dict,
) -> dict:
    """
    Apply policy rules in strict priority order — first match wins.

    Synchronous port of ``backend.app.services.policy_engine.evaluate_policy``
    — the backend version is ``async`` only by signature; it has no awaits.

    Priority order:

    1. Hard denies         → ``"deny"``
    2. Approval gates      → ``"require_approval"``
    3. Audit flags         → ``"allow_with_flag"``
    4. Default             → ``"allow"``

    Parameters
    ----------
    action_type, action_name, payload, agent_name :
        The action being evaluated.
    risk_classification : dict
        Output of :func:`classify_risk`.
    cumulative_metrics : dict
        Running chain totals — must include ``financial_exposure_usd`` when
        financial thresholds are relevant.  Pass the dict returned by
        :func:`update_chain_metrics_local` (i.e. *after* the current action
        has been added) so cumulative checks reflect the true exposure.
    org_config : dict
        Organisation-level configuration.  Recognised keys:

        ``environment``
            ``"production"`` blocks all delete operations unconditionally.
        ``financial_approval_threshold_usd``
            Single-transaction limit (default 5 000).
        ``cumulative_threshold_usd``
            Chain cumulative financial limit (default 10 000).
        ``high_risk_agents``
            List of agent names that always require approval.

    Returns
    -------
    dict
        ``{"decision": str, "reason": str, "source": "backend_evaluation"}``
        where ``decision`` is one of: ``allow``, ``allow_with_flag``,
        ``require_approval``, ``deny``.
    """
    aname = action_name.lower()
    categories: list[str] = risk_classification.get("categories", [])
    risk_score: int = risk_classification.get("risk_score", 0)
    cumulative_usd: float = cumulative_metrics.get("financial_exposure_usd", 0.0)

    # ---------------------------------------------------------------------------
    # Hard denies
    # ---------------------------------------------------------------------------

    if "delete" in aname and org_config.get("environment") == "production":
        return _decision("deny", "Production delete blocked")

    if cumulative_usd > 50_000:
        return _decision(
            "deny",
            f"Cumulative financial exposure exceeds $50,000 hard limit "
            f"(current: ${cumulative_usd:,.2f})",
        )

    if "credential_exposure" in categories:
        return _decision("deny", "Credential exposure detected in payload")

    if "iam" in aname or "permission" in aname:
        return _decision("deny", "IAM/permission modification blocked")

    # ---------------------------------------------------------------------------
    # Approval triggers
    # ---------------------------------------------------------------------------

    single_amount: float = _extract_numeric(payload, ["amount", "value"]) or 0.0
    financial_threshold: float = float(
        org_config.get("financial_approval_threshold_usd", 5_000)
    )
    if single_amount > financial_threshold:
        return _decision(
            "require_approval",
            f"Single transaction (${single_amount:,.2f}) exceeds approval "
            f"threshold (${financial_threshold:,.2f})",
        )

    cumulative_threshold: float = float(
        org_config.get("cumulative_threshold_usd", 10_000)
    )
    if cumulative_usd > cumulative_threshold:
        return _decision(
            "require_approval",
            f"Cumulative financial exposure (${cumulative_usd:,.2f}) exceeds "
            f"threshold (${cumulative_threshold:,.2f})",
        )

    if (
        "communication" in categories
        and cumulative_metrics.get("external_communications_count", 0) == 1
    ):
        return _decision("require_approval", "First external communication requires approval")

    if risk_score >= 70:
        return _decision(
            "require_approval",
            f"High risk score ({risk_score}/100) requires approval",
        )

    # NOTE: the static high_risk_agents gate has been removed.
    # Agent risk tier is now driven by the registry (registered_agents / risk_tier field).

    # ---------------------------------------------------------------------------
    # Audit flags
    # ---------------------------------------------------------------------------

    if "write" in categories:
        return _decision("allow_with_flag", "Write operation flagged for audit")

    if risk_score >= 40:
        return _decision(
            "allow_with_flag",
            f"Medium risk score ({risk_score}/100) flagged for review",
        )

    # ---------------------------------------------------------------------------
    # Default allow
    # ---------------------------------------------------------------------------

    return _decision("allow", "Action permitted by policy")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def process_action_local(
    agent_name: str,
    action_type: str,
    action_name: str,
    payload: dict,
    cumulative_metrics: dict,
    org_config: dict,
    *,
    kill_switch_active: bool = False,
    kill_switch_reason: str | None = None,
    monthly_budget_exceeded: bool = False,
    active_exception_id: str | None = None,
    policy_mode: str = "enforce",
) -> dict:
    """
    Run the full four-stage policy pipeline locally.

    Mirrors ``backend.app.services.policy_engine.process_action``, replacing
    every DB-bound operation with a caller-supplied input:

    - ``check_kill_switch``       → ``kill_switch_active``
    - ``check_monthly_budget``    → ``monthly_budget_exceeded``
    - ``check_active_exception``  → ``active_exception_id``
    - ``update_chain_metrics``    → done in-memory internally

    The caller is responsible for supplying accurate values and for persisting
    ``updated_cumulative_metrics`` from the returned dict if needed.

    Parameters
    ----------
    agent_name, action_type, action_name, payload :
        The action to evaluate.
    cumulative_metrics : dict
        Current running chain totals passed to the evaluator.  Not mutated.
    org_config : dict
        Organisation-level configuration.  Same keys as :func:`evaluate_policy`.
        Also reads ``policy_mode`` from here if not supplied as a keyword arg.
    kill_switch_active : bool
        Set True when the organisation kill switch is active.
    kill_switch_reason : str | None
        Human-readable reason for the kill switch, if any.
    monthly_budget_exceeded : bool
        Set True when the monthly LLM cost budget would be exceeded.  Upgrades
        an ``allow`` or ``allow_with_flag`` decision to ``require_approval``.
    active_exception_id : str | None
        ID of a matching active policy exception.  Downgrades
        ``require_approval`` to ``allow`` (time-boxed exception bypass).
    policy_mode : str
        ``"enforce"`` (default), ``"shadow"``, or ``"disabled"``.

    Returns
    -------
    dict
        Keys: ``decision``, ``reason``, ``source``, ``risk_classification``,
        ``evaluation_mode``, ``shadow_decision``, ``kill_switch_active``,
        ``updated_cumulative_metrics``.

        ``source`` is ``"sdk_reference_implementation"`` — distinct from
        ``"backend_evaluation"`` so dashboard tooling can differentiate.

        ``updated_cumulative_metrics`` reflects the current action; the caller
        must persist it if multi-action chain state is needed.
    """
    # ------------------------------------------------------------------
    # Stage 0 — Kill-switch (always runs before mode logic)
    # ------------------------------------------------------------------
    if kill_switch_active:
        reason_suffix = f": {kill_switch_reason}" if kill_switch_reason else ""
        logger.warning(
            "Kill switch active for agent=%s action=%s", agent_name, action_name
        )
        result = _full_result(
            decision="deny",
            reason=f"Kill switch active{reason_suffix}. Contact your organization admin.",
            risk_classification={},
            evaluation_mode="enforce",
            kill_switch_active=True,
        )
        result["updated_cumulative_metrics"] = dict(cumulative_metrics)
        return result

    # ------------------------------------------------------------------
    # Disabled mode — skip everything
    # ------------------------------------------------------------------
    if policy_mode == "disabled":
        logger.info(
            "Policy disabled for agent=%s action=%s — auto-allow",
            agent_name, action_name,
        )
        result = _full_result(
            decision="allow",
            reason="Policy evaluation disabled",
            risk_classification={},
            evaluation_mode="disabled",
        )
        result["updated_cumulative_metrics"] = dict(cumulative_metrics)
        return result

    # ------------------------------------------------------------------
    # Build chain context
    # ------------------------------------------------------------------
    chain_context: dict = {
        "high_risk_agents": org_config.get("high_risk_agents", []),
        "environment": org_config.get("environment", "development"),
    }
    _reg = org_config.get("registered_agents")
    if _reg is not None:
        chain_context["registered_agents"] = (
            {name.lower().strip(): {"risk_tier": "standard"} for name in _reg}
            if isinstance(_reg, list)
            else _reg
        )

    # ------------------------------------------------------------------
    # Stage 1 — Classify risk
    # ------------------------------------------------------------------
    risk_classification = classify_risk(
        action_type=action_type,
        action_name=action_name,
        payload=payload,
        agent_name=agent_name,
        chain_context=chain_context,
    )

    logger.debug(
        "Risk classified for agent=%s action=%s: score=%d categories=%s",
        agent_name,
        action_name,
        risk_classification["risk_score"],
        risk_classification["categories"],
    )

    # ------------------------------------------------------------------
    # Stage 2 — Update cumulative metrics (in-memory, non-mutating)
    # ------------------------------------------------------------------
    updated_metrics = update_chain_metrics_local(
        cumulative_metrics=cumulative_metrics,
        action_type=action_type,
        action_name=action_name,
        payload=payload,
        risk_classification=risk_classification,
    )

    # ------------------------------------------------------------------
    # Stage 3 — Evaluate policy
    # ------------------------------------------------------------------
    raw_decision = evaluate_policy(
        action_type=action_type,
        action_name=action_name,
        payload=payload,
        agent_name=agent_name,
        risk_classification=risk_classification,
        cumulative_metrics=updated_metrics,
        org_config=org_config,
    )

    true_decision: str = raw_decision["decision"]
    true_reason: str = raw_decision["reason"]

    logger.debug(
        "Policy decision for agent=%s action=%s: %s — %s [mode=%s]",
        agent_name,
        action_name,
        true_decision,
        true_reason,
        policy_mode,
    )

    # ------------------------------------------------------------------
    # Monthly budget gate (enforce mode only)
    # Mirrors backend logic: only escalate if decision would otherwise
    # allow the action through.
    # ------------------------------------------------------------------
    if policy_mode == "enforce" and true_decision in ("allow", "allow_with_flag"):
        if monthly_budget_exceeded:
            true_decision = "require_approval"
            true_reason = "Monthly LLM cost budget would be exceeded"
            logger.warning(
                "Monthly budget gate triggered for agent=%s action=%s",
                agent_name, action_name,
            )

    # ------------------------------------------------------------------
    # Time-boxed exception gate (enforce mode, require_approval only)
    # A matching active exception converts require_approval → allow.
    # ------------------------------------------------------------------
    if policy_mode == "enforce" and true_decision == "require_approval":
        if active_exception_id is not None:
            logger.info(
                "Time-boxed exception bypass: agent=%s exception=%s",
                agent_name, active_exception_id,
            )
            result = _full_result(
                decision="allow",
                reason=(
                    f"Time-boxed exception active (id={active_exception_id}): "
                    "approval bypassed"
                ),
                risk_classification=risk_classification,
                evaluation_mode="enforce",
            )
            result["updated_cumulative_metrics"] = updated_metrics
            return result

    # ------------------------------------------------------------------
    # Shadow mode — return "allow" to caller but record true decision
    # ------------------------------------------------------------------
    if policy_mode == "shadow":
        logger.info(
            "Shadow mode: would-have-been decision=%s for agent=%s",
            true_decision, agent_name,
        )
        result = _full_result(
            decision="allow",
            reason=f"Shadow mode active — action permitted (real decision: {true_decision})",
            risk_classification=risk_classification,
            evaluation_mode="shadow",
            shadow_decision=true_decision,
        )
        result["updated_cumulative_metrics"] = updated_metrics
        return result

    # ------------------------------------------------------------------
    # Enforce mode — return the real decision
    # ------------------------------------------------------------------
    result = _full_result(
        decision=true_decision,
        reason=true_reason,
        risk_classification=risk_classification,
        evaluation_mode="enforce",
    )
    result["updated_cumulative_metrics"] = updated_metrics
    return result
