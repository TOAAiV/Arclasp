"""
Legacy local fast-path compatibility helpers.

Public governed Chain execution no longer calls this module for allow
authority. The functions remain importable for older callers and focused
compatibility tests, but backend evaluation is authoritative for SDK-governed
actions.
"""

from __future__ import annotations

import logging

from proofrail.models import ChainConfig
from proofrail.policies import classify_risk, process_action_local

logger = logging.getLogger(__name__)

# Categories whose presence blocks fast-path evaluation — too high-risk to
# bypass the backend, even if the risk score happens to be below 40.
_FAST_PATH_BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    [
        "destructive",
        "credential_exposure",
        "exfiltration",
        "privilege_escalation",
        "financial",
        "financial_high",
    ]
)


def is_fast_path_eligible(
    action_type: str,
    action_name: str,
    payload: dict,
    agent_name: str,
    cumulative_metrics: dict,
    config: ChainConfig,
) -> tuple[bool, str]:
    """
    Decide whether this action can be evaluated locally without a backend call.

    All five eligibility criteria must pass; the first failure short-circuits.

    Parameters
    ----------
    action_type, action_name, payload, agent_name :
        The action to test.
    cumulative_metrics : dict
        Current running chain totals — used for the threshold proximity check.
    config : ChainConfig
        The current SDK configuration.

    Returns
    -------
    tuple[bool, str]
        ``(eligible, reason)`` where ``reason`` is a short human-readable
        explanation of the outcome — useful for debugging fast-path skip
        decisions (logged at DEBUG level by :func:`evaluate_fast_path`).
    """
    # Criterion 1 — fast-path must be enabled
    if not config.enable_local_fast_path:
        return False, "enable_local_fast_path is False"

    # Criterion 2 — never bypass the backend in production
    if config.environment == "production":
        return False, "production environment always uses backend"

    # Criterion 3 — risk score and category gate
    chain_context: dict = {"high_risk_agents": config.high_risk_agents}
    if config.registered_agents is not None:
        chain_context["registered_agents"] = {
            name.lower().strip(): {"risk_tier": "standard"}
            for name in config.registered_agents
        }
    risk = classify_risk(action_type, action_name, payload, agent_name, chain_context)
    risk_score: int = risk["risk_score"]
    categories: list[str] = risk["categories"]

    if risk_score >= 40:
        return False, f"risk score {risk_score} >= 40"

    blocking = _FAST_PATH_BLOCKING_CATEGORIES.intersection(categories)
    if blocking:
        return False, f"blocking categories present: {sorted(blocking)}"

    # Criterion 4 — financial exposure must not be within 80% of the threshold
    current_exposure: float = cumulative_metrics.get("financial_exposure_usd", 0.0)
    threshold: float = config.cumulative_financial_threshold_usd
    if current_exposure >= threshold * 0.8:
        return (
            False,
            f"financial exposure ${current_exposure:,.2f} >= 80% of "
            f"cumulative threshold ${threshold:,.2f}",
        )

    # Criterion 5 — agent must not be designated high-risk
    if agent_name in config.high_risk_agents:
        return False, f"agent '{agent_name}' is in high_risk_agents"

    return True, "all fast-path criteria passed"


def evaluate_fast_path(
    action_type: str,
    action_name: str,
    payload: dict,
    agent_name: str,
    cumulative_metrics: dict,
    config: ChainConfig,
) -> dict | None:
    """
    Attempt a local fast-path evaluation for an agent action.

    Returns a decision dict (same wire shape as backend responses) when the
    action is eligible AND the local reference policy returns ``"allow"``.
    Returns ``None`` in all other cases — the caller should fall back to a
    synchronous backend round-trip.

    Parameters
    ----------
    action_type, action_name, payload, agent_name :
        The action to evaluate.
    cumulative_metrics : dict
        Running chain totals (used for threshold proximity checks).
    config : ChainConfig
        The current SDK configuration.

    Returns
    -------
    dict | None
        Decision dict with keys ``policy_decision``, ``decision_reason``,
        ``decision_source="local_fast_path"`` — ready for
        ``PolicyDecision.model_validate()`` — when eligible and allowed;
        ``None`` otherwise.
    """
    eligible, reason = is_fast_path_eligible(
        action_type, action_name, payload, agent_name, cumulative_metrics, config
    )
    if not eligible:
        logger.debug("Fast-path skipped for '%s': %s", action_name, reason)
        return None

    org_config = {
        "environment": config.environment,
        "financial_approval_threshold_usd": config.financial_approval_threshold_usd,
        "cumulative_threshold_usd": config.cumulative_financial_threshold_usd,
        "high_risk_agents": config.high_risk_agents,
        "registered_agents": config.registered_agents,
    }

    local_result = process_action_local(
        agent_name=agent_name,
        action_type=action_type,
        action_name=action_name,
        payload=payload,
        cumulative_metrics=cumulative_metrics,
        org_config=org_config,
        kill_switch_active=False,
        monthly_budget_exceeded=False,
        policy_mode="enforce",
    )

    if local_result["decision"] != "allow":
        # Action looked safe by heuristics but local policy says otherwise.
        # Fall back to backend for an authoritative decision.
        logger.debug(
            "Fast-path rejected '%s' after local eval: decision=%s — falling back to backend",
            action_name,
            local_result["decision"],
        )
        return None

    logger.debug(
        "Fast-path allow for '%s' (risk_score=%d)",
        action_name,
        local_result.get("risk_classification", {}).get("risk_score", 0),
    )

    return {
        "policy_decision": "allow",
        "decision_reason": local_result["reason"],
        "decision_source": "local_fast_path",
        # "cumulative_metrics" populates the PolicyDecision returned to the
        # caller; "updated_cumulative_metrics" is a separate key consumed
        # (and popped) by chain.py to update its own running-totals snapshot.
        # Without both keys present, PolicyDecision.model_validate() silently
        # defaults cumulative_metrics to {} for every fast-path-allowed
        # decision once chain.py pops "updated_cumulative_metrics" out of
        # this same dict before validating it.
        "cumulative_metrics": local_result["updated_cumulative_metrics"],
        "updated_cumulative_metrics": local_result["updated_cumulative_metrics"],
    }
