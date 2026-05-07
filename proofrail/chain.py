"""
proofrail.chain — Agent chain tracking and execution context.

Usage (async — preferred):
    async with proofrail.Chain("order-processing", metadata={"order_id": "123"}) as chain:
        await chain.record_agent_action(
            agent_name="pricing-agent",
            action_type="calculation",
            action_name="apply_discount",
            payload={"discount_pct": 15},
        )

Usage (sync — for non-async scripts only):
    with proofrail.Chain("order-processing") as chain:
        # record_agent_action is always async; wrap it for sync contexts:
        import asyncio
        asyncio.run(chain.record_agent_action(...))
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from types import TracebackType

from proofrail import client as _client
from proofrail import fast_path as _fast_path
from proofrail.exceptions import (
    ActionDeniedError,
    ChainTimeoutError,
    ProofRailKillSwitchError,
    _POLICY_REMEDIATION,
)
from proofrail.models import PolicyDecision
from proofrail.sanitization import sanitize_payload

logger = logging.getLogger(__name__)

# Sentinel UUID sent in the ChainCreate body.  The backend derives the real
# organization_id from the API key; this field is validated but ignored.
_ORG_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Offline buffer helper
# ---------------------------------------------------------------------------

def _buffer_event(buffer: list, event_body: dict, max_events: int) -> bool:
    """
    Append *event_body* to *buffer* up to *max_events* capacity.

    Returns True if the event was buffered, False if the buffer is full.

    This is a standalone helper (not a Chain method) so the Phase-4 async
    fast-path event sender can call it directly without a Chain reference.
    """
    if len(buffer) >= max_events:
        logger.warning(
            "Offline buffer is full (%d events) — dropping event", max_events
        )
        return False
    buffer.append(event_body)
    return True


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

class Chain:
    """
    Context manager that wraps a ProofRail chain lifecycle.

    Supports both ``async with`` (preferred) and ``with`` (sync scripts only).
    The sync interface uses ``asyncio.run()`` internally and will raise a
    ``RuntimeError`` if called from inside a running event loop — use
    ``async with`` in that case.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata: dict = metadata or {}

        self._chain_id: str | None = None
        self._sequence_number: int = 1
        self._offline: bool = False  # True when backend unreachable + fail_mode=allow
        self._offline_buffer: list[dict] = []
        self._cumulative_metrics: dict = {}  # in-process snapshot for fast-path checks

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chain_id(self) -> str | None:
        """The backend-assigned chain UUID, available after __enter__."""
        return self._chain_id

    # ------------------------------------------------------------------
    # Async context manager (preferred)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "Chain":
        await self._start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        await self._complete()
        return False  # do not suppress exceptions

    # ------------------------------------------------------------------
    # Sync context manager (non-async scripts only)
    # ------------------------------------------------------------------

    def __enter__(self) -> "Chain":
        try:
            asyncio.get_running_loop()
            # A loop is already running — cannot use blocking asyncio.run().
            raise RuntimeError(
                "Cannot use synchronous 'with Chain(...)' inside a running async "
                "event loop.  Use 'async with Chain(...)' instead."
            )
        except RuntimeError as exc:
            if "no running event loop" not in str(exc) and "no current event loop" not in str(exc):
                raise
        asyncio.run(self._start())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        asyncio.run(self._complete())
        return False

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def record_agent_action(
        self,
        agent_name: str,
        action_type: str,
        action_name: str,
        payload: dict | None = None,
        parent_agent_name: str | None = None,
    ) -> PolicyDecision:
        """
        Record an agent action on this chain and evaluate it against policy.

        Parameters
        ----------
        agent_name : str
            Identifier of the agent taking the action.
        action_type : str
            Category of the action, e.g. ``"tool_call"``, ``"llm_inference"``.
        action_name : str
            Specific action within the category, e.g. ``"send_email"``.
        payload : dict, optional
            Arbitrary data describing the action.  Sanitized before sending.
        parent_agent_name : str, optional
            The agent that invoked this one, if any.

        Returns
        -------
        PolicyDecision
            The resolved policy decision.  Three possible outcomes:

            * ``policy_decision="allow"`` — action permitted by policy.
            * ``policy_decision="allow", decision_source="human_approval"`` —
              action was initially gated for human review and a reviewer approved
              it.  ``decision_reason`` will contain the approver's notes when
              provided.
            * ``policy_decision="allow", decision_source="offline_stub"`` —
              backend was unreachable and ``fail_mode="allow"`` is configured.

        Raises
        ------
        ActionDeniedError
            When the policy decision is ``"deny"`` or a human approval is
            denied / times out.
        ChainTimeoutError
            When an ``"approve"`` gate is not resolved within the configured
            timeout window.

        Examples
        --------
        >>> async with Chain("checkout") as chain:
        ...     decision = await chain.record_agent_action(
        ...         agent_name="payment-agent",
        ...         action_type="tool_call",
        ...         action_name="charge_card",
        ...         payload={"amount_usd": 6000},
        ...     )
        ...     # decision.policy_decision is always "allow" here —
        ...     # if a human approval gate was required, it has already
        ...     # been resolved before this line is reached.
        ...     print(decision.decision_source)
        """
        if self._chain_id is None and not self._offline:
            raise RuntimeError("Chain has not been started. Use it as a context manager.")

        config = _client.get_config()
        sanitized = sanitize_payload(payload or {}, config)

        event_body = {
            "agent_name": agent_name,
            "parent_agent_name": parent_agent_name,
            "action_type": action_type,
            "action_name": action_name,
            "action_payload": sanitized,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._offline:
            # Backend unavailable and fail_mode=allow — buffer the event locally.
            _buffer_event(self._offline_buffer, event_body, config.offline_buffer_max_events)
            self._sequence_number += 1
            logger.debug(
                "Offline mode: buffered event for action '%s' (buffered=%d)",
                action_name,
                len(self._offline_buffer),
            )
            return PolicyDecision(
                policy_decision="allow",
                decision_reason="Offline — fail_mode=allow",
                decision_source="offline_stub",
            )

        # --- Fast-path evaluation (non-production + low-risk actions) ---
        fast_decision = _fast_path.evaluate_fast_path(
            action_type=action_type,
            action_name=action_name,
            payload=sanitized,
            agent_name=agent_name,
            cumulative_metrics=self._cumulative_metrics,
            config=config,
        )
        if fast_decision is not None:
            # Buffer the event and drain asynchronously — agent is not blocked.
            _buffer_event(self._offline_buffer, event_body, config.offline_buffer_max_events)
            asyncio.create_task(_drain_buffer_to_backend(self))
            self._sequence_number += 1
            logger.debug(
                "Fast-path allow for '%s' (chain=%s)", action_name, self._chain_id
            )
            return PolicyDecision.model_validate(fast_decision)

        try:
            response = await _client._post(
                f"/v1/chains/{self._chain_id}/events", event_body,
                action_type=action_type,
            )
        except _client._OfflineSignal:
            # Backend went offline mid-chain — transition to offline and buffer.
            self._offline = True
            _buffer_event(self._offline_buffer, event_body, config.offline_buffer_max_events)
            self._sequence_number += 1
            logger.warning(
                "Backend went offline mid-chain (id=%s) — switching to offline mode",
                self._chain_id,
            )
            return PolicyDecision(
                policy_decision="allow",
                decision_reason="Offline — fail_mode=allow",
                decision_source="offline_stub",
            )

        decision_obj = PolicyDecision.model_validate(response)

        logger.debug(
            "Event recorded (chain=%s seq=%d decision=%s)",
            self._chain_id,
            self._sequence_number,
            decision_obj.policy_decision,
        )

        self._sequence_number += 1

        if decision_obj.policy_decision == "deny":
            # Kill-switch denials carry a distinct flag so callers can
            # differentiate them from ordinary policy violations.
            if decision_obj.kill_switch_active:
                raise ProofRailKillSwitchError(
                    message=decision_obj.decision_reason
                    or "All agent actions are denied: organisation kill switch is active",
                    reason=decision_obj.pause_reason,
                )
            raise _build_action_denied(decision_obj, self._chain_id, self._sequence_number)

        if decision_obj.policy_decision == "require_approval":
            approver_notes = await self._poll_for_approval()  # raises if denied or timed out
            return PolicyDecision(
                policy_decision="allow",
                decision_reason=(
                    f"Approved by human reviewer: {approver_notes}"
                    if approver_notes
                    else "Approved by human reviewer"
                ),
                decision_source="human_approval",
                policy_name=decision_obj.policy_name,
                kill_switch_active=decision_obj.kill_switch_active,
                pause_reason=decision_obj.pause_reason,
                remediation=decision_obj.remediation,
                docs_url=decision_obj.docs_url,
            )

        return decision_obj

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        """Create the chain on the backend and store the assigned chain_id."""
        config = _client.get_config()

        body = {
            "organization_id": _ORG_ID_PLACEHOLDER,
            "external_chain_id": self.name,
            "environment": config.environment,
            "metadata": self.metadata,
        }

        try:
            response = await _client._post("/v1/chains", body)
            self._chain_id = response["id"]
        except _client._OfflineSignal:
            # Backend unreachable and fail_mode=allow — generate a local UUID
            # so the chain can continue offline.  Events are buffered until
            # connectivity is restored (replay is a future-phase feature).
            self._chain_id = str(uuid.uuid4())
            self._offline = True
            logger.warning(
                "Backend unreachable at chain start — running offline (id=%s name=%s)",
                self._chain_id,
                self.name,
            )

        logger.debug(
            "Chain started (id=%s name=%s offline=%s)",
            self._chain_id, self.name, self._offline,
        )

    async def _complete(self) -> None:
        """Mark the chain as completed on the backend."""
        if self._chain_id is None:
            return  # Never started (offline or error on entry) — nothing to close.

        if self._offline:
            logger.debug(
                "Offline mode: skipping chain completion for chain %s", self._chain_id
            )
            return

        try:
            await _client._post(f"/v1/chains/{self._chain_id}/complete", {})
            logger.debug("Chain completed (id=%s)", self._chain_id)
        except Exception as exc:
            # Completing a chain is best-effort — log but do not mask any
            # exception that is already propagating from the with-block body.
            logger.warning("Failed to mark chain %s as completed: %s", self._chain_id, exc)

    async def _poll_for_approval(self) -> str | None:
        """
        Poll GET /v1/chains/{chain_id}/approval-status every 5 seconds until
        the approval is resolved or the timeout expires.

        Returns
        -------
        str | None
            The approver's notes/reason if the action was approved, or ``None``
            if the approver did not leave a note.

        Raises
        ------
        ActionDeniedError
            If the approval is denied or times out on the backend side.
        ChainTimeoutError
            If the local polling window expires before a decision is made.
        """
        config = _client.get_config()
        poll_interval_seconds = 5
        timeout_seconds = config.default_approval_timeout_hours * 3600
        elapsed = 0

        logger.info(
            "Waiting for approval on chain %s (timeout=%dh)",
            self._chain_id,
            config.default_approval_timeout_hours,
        )

        while elapsed < timeout_seconds:
            await asyncio.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

            try:
                status_response = await _client._get(
                    f"/v1/chains/{self._chain_id}/approval-status"
                )
            except Exception as exc:
                logger.warning("Approval poll failed: %s — retrying", exc)
                continue

            approval_status = status_response.get("approval_status")
            approver_notes: str | None = None
            approvals_list = status_response.get("approvals", [])
            if approvals_list:
                approver_notes = approvals_list[0].get("reason") or None

            if approval_status == "approved":
                logger.info("Approval granted for chain %s", self._chain_id)
                return approver_notes

            if approval_status in ("denied", "timed_out"):
                policy_name = (
                    "human_approval_denied" if approval_status == "denied" else "approval_timeout"
                )
                default_rem, default_docs = _POLICY_REMEDIATION.get(policy_name, (None, None))
                condition = (
                    approver_notes
                    if approval_status == "denied"
                    else "Approval was not resolved within the configured timeout window."
                )
                raise ActionDeniedError(
                    message=f"Approval {approval_status} for chain {self._chain_id}",
                    policy_name=policy_name,
                    condition=condition,
                    chain_context={"chain_id": self._chain_id},
                    decision_source="human_approval",
                    remediation=default_rem,
                    docs_url=default_docs,
                )

            # "pending" or None — keep polling
            logger.debug(
                "Approval still pending for chain %s (%ds elapsed)",
                self._chain_id,
                elapsed,
            )

        raise ChainTimeoutError(
            chain_id=self._chain_id,
            timeout_seconds=timeout_seconds,
        )


# ---------------------------------------------------------------------------
# Error construction helper
# ---------------------------------------------------------------------------

def _build_action_denied(
    decision: PolicyDecision,
    chain_id: str | None,
    sequence_number: int,
) -> ActionDeniedError:
    """
    Construct a fully-populated ActionDeniedError from a PolicyDecision.

    Falls back to the SDK's built-in remediation lookup when the backend does
    not supply remediation / docs_url fields.
    """
    policy_name = decision.policy_name
    default_remediation: str | None = None
    default_docs_url: str | None = None
    if policy_name and policy_name in _POLICY_REMEDIATION:
        default_remediation, default_docs_url = _POLICY_REMEDIATION[policy_name]

    message = (
        f"Action denied by policy '{policy_name}'."
        if policy_name
        else (decision.decision_reason or "Action denied by policy")
    )

    return ActionDeniedError(
        message=message,
        policy_name=policy_name,
        condition=decision.decision_reason or None,
        chain_context={"chain_id": chain_id, "sequence": sequence_number} if chain_id else None,
        remediation=decision.remediation or default_remediation,
        docs_url=decision.docs_url or default_docs_url,
        decision_source=decision.decision_source,
    )


# ---------------------------------------------------------------------------
# Fast-path async drain helper
# ---------------------------------------------------------------------------

async def _drain_buffer_to_backend(chain: Chain) -> None:
    """
    Send buffered events to the backend asynchronously (fire-and-forget).

    Called via ``asyncio.create_task`` after a fast-path decision so that
    events are logged without blocking the agent.  Failures are logged at
    DEBUG level and the event remains in the buffer — it is not retried
    automatically, but will be visible in the offline buffer for future use.

    Never raises — swallows all exceptions.
    """
    if not chain._offline_buffer or chain._chain_id is None or chain._offline:
        return

    # Drain events one by one; stop at the first failure so we don't
    # interleave partial sends with later events.
    while chain._offline_buffer:
        event_body = chain._offline_buffer[0]
        try:
            await _client._post(
                f"/v1/chains/{chain._chain_id}/events",
                event_body,
                action_type=event_body.get("action_type"),
            )
            chain._offline_buffer.pop(0)
        except Exception as exc:
            logger.debug(
                "Fast-path async log failed (chain=%s): %s — event stays in buffer",
                chain._chain_id,
                exc,
            )
            break
