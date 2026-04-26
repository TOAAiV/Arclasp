"""
aag.chain — Agent chain tracking and execution context.

Usage (async — preferred):
    async with aag.Chain("order-processing", metadata={"order_id": "123"}) as chain:
        await chain.record_agent_action(
            agent_name="pricing-agent",
            action_type="calculation",
            action_name="apply_discount",
            payload={"discount_pct": 15},
        )

Usage (sync — for non-async scripts only):
    with aag.Chain("order-processing") as chain:
        # record_agent_action is always async; wrap it for sync contexts:
        import asyncio
        asyncio.run(chain.record_agent_action(...))
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import TracebackType
from typing import Type

from aag import client as _client
from aag.exceptions import ActionDeniedError, ChainTimeoutError, ProofRailKillSwitchError
from aag.sanitization import sanitize_payload

logger = logging.getLogger(__name__)

# Sentinel UUID sent in the ChainCreate body.  The backend derives the real
# organization_id from the API key; this field is validated but ignored.
_ORG_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"


class Chain:
    """
    Context manager that wraps an aag chain lifecycle.

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
        exc_type: Type[BaseException] | None,
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
        exc_type: Type[BaseException] | None,
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
    ) -> dict:
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
        dict
            The raw policy decision response from the backend.

        Raises
        ------
        ActionDeniedError
            When the policy decision is ``"deny"`` or an approval is denied /
            times out.
        ChainTimeoutError
            When an ``"approve"`` gate is not resolved within the configured
            timeout window.
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
            # Backend unavailable and fail_mode=allow — skip remote call.
            logger.debug(
                "Offline mode: skipping event submission for action '%s'", action_name
            )
            self._sequence_number += 1
            return {
                "policy_decision": "allow",
                "decision_reason": "Offline — fail_mode=allow",
                "decision_source": "offline_stub",
            }

        response = await _client._post(
            f"/v1/chains/{self._chain_id}/events", event_body,
            action_type=action_type,
        )

        decision = response.get("policy_decision", "allow")
        reason = response.get("decision_reason", "")
        source = response.get("decision_source", "backend_evaluation")

        logger.debug(
            "Event recorded (chain=%s seq=%d decision=%s)",
            self._chain_id,
            self._sequence_number,
            decision,
        )

        self._sequence_number += 1

        if decision == "deny":
            # Kill-switch denials carry a distinct flag so callers can
            # differentiate them from ordinary policy violations.
            if response.get("kill_switch_active"):
                raise ProofRailKillSwitchError(
                    message=reason or "All agent actions are denied: organisation kill switch is active",
                    reason=response.get("pause_reason"),
                )
            raise ActionDeniedError(
                message=reason or "Action denied by policy",
                decision_source=source,
            )

        if decision == "require_approval":
            await self._poll_for_approval()

        return response

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
            logger.debug("Chain started (id=%s name=%s)", self._chain_id, self.name)
        except Exception:
            # _post already applied fail_mode.  If we reach here the error
            # was re-raised (fail_mode=deny) — propagate it.
            raise

    async def _complete(self) -> None:
        """Mark the chain as completed on the backend."""
        if self._chain_id is None:
            return  # Never started (offline or error on entry) — nothing to close.

        try:
            await _client._post(f"/v1/chains/{self._chain_id}/complete", {})
            logger.debug("Chain completed (id=%s)", self._chain_id)
        except Exception as exc:
            # Completing a chain is best-effort — log but do not mask any
            # exception that is already propagating from the with-block body.
            logger.warning("Failed to mark chain %s as completed: %s", self._chain_id, exc)

    async def _poll_for_approval(self) -> None:
        """
        Poll GET /v1/chains/{chain_id}/approval-status every 5 seconds until
        the approval is resolved or the timeout expires.

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

            if approval_status == "approved":
                logger.info("Approval granted for chain %s", self._chain_id)
                return

            if approval_status in ("denied", "timed_out"):
                raise ActionDeniedError(
                    message=f"Approval {approval_status} for chain {self._chain_id}",
                    chain_context=self._chain_id,
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
