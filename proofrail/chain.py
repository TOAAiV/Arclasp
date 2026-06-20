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

import httpx

from proofrail import client as _client
from proofrail import fast_path as _fast_path
from proofrail.exceptions import (
    ActionDeniedError,
    ChainAutoPausedError,
    ChainTimeoutError,
    ProofRailKillSwitchError,
    _POLICY_REMEDIATION,
)
from proofrail.models import (
    ChainDetail,
    ChainEventsResponse,
    ChainReceiptResponse,
    PolicyDecision,
    ReceiptVerifyResponse,
)
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
    Append *event_body* to *buffer*, evicting the oldest entry when full.

    When the buffer is at capacity, the oldest (index 0) event is dropped and
    a warning is logged before the new event is appended.  Per spec section 12,
    oldest events are dropped first so the audit trail stays as current as
    possible under pressure.

    Always returns True (the event is always accepted).

    This is a standalone helper (not a Chain method) so the Phase-4 async
    fast-path event sender can call it directly without a Chain reference.
    """
    if len(buffer) >= max_events:
        dropped = buffer.pop(0)
        logger.warning(
            "Offline buffer full (%d events) — dropped oldest event (action=%s)",
            max_events,
            dropped.get("action_name", "unknown"),
        )
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
        # Single-flight drain task — at most one drain coroutine runs at a time.
        self._drain_task: asyncio.Task | None = None

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
            if "no running event loop" not in str(
                exc
            ) and "no current event loop" not in str(exc):
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
            raise RuntimeError(
                "Chain has not been started. Use it as a context manager."
            )

        config = _client.get_config()
        sanitized = sanitize_payload(payload or {}, config)

        # Generated once here; travels unchanged through retries, offline
        # buffer, and drain — backend deduplicates on (chain_id, idempotency_key).
        idempotency_key = uuid.uuid4().hex

        event_body = {
            "idempotency_key": idempotency_key,
            "agent_name": agent_name,
            "parent_agent_name": parent_agent_name,
            "action_type": action_type,
            "action_name": action_name,
            "action_payload": sanitized,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._offline:
            # Backend unavailable and fail_mode=allow — buffer the event locally.
            _buffer_event(
                self._offline_buffer, event_body, config.offline_buffer_max_events
            )
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
            # Update local cumulative metrics so the next fast-path eligibility
            # check (criterion 4) uses accurate totals instead of the initial
            # empty dict. Safe without a lock: no await exists between the
            # read (passed to evaluate_fast_path above) and this write, so no
            # other coroutine can preempt at this point. (SDK-S-2)
            self._cumulative_metrics = fast_decision.pop(
                "updated_cumulative_metrics", self._cumulative_metrics
            )
            # Buffer the event and drain asynchronously — agent is not blocked.
            # Single-flight: only spawn a new drain task when no task is running.
            # This prevents concurrent drain tasks from reading the same buffer
            # entry and sending duplicate events (audit finding C-1).
            _buffer_event(
                self._offline_buffer, event_body, config.offline_buffer_max_events
            )
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(_drain_buffer_to_backend(self))
            self._sequence_number += 1
            logger.debug(
                "Fast-path allow for '%s' (chain=%s)", action_name, self._chain_id
            )
            return PolicyDecision.model_validate(fast_decision)

        try:
            response = await _client._post(
                f"/v1/chains/{self._chain_id}/events",
                event_body,
                action_type=action_type,
            )
        except _client._OfflineSignal:
            # Backend went offline mid-chain — transition to offline and buffer.
            self._offline = True
            _buffer_event(
                self._offline_buffer, event_body, config.offline_buffer_max_events
            )
            self._sequence_number += 1
            logger.warning(
                "Backend went offline mid-chain (id=%s) — switching to offline mode",
                self._chain_id,
            )
            # Start offline drain task (single-flight).  The drain task will
            # attempt to flush buffered events immediately, then retry every
            # 30 s until the buffer is empty or the chain exits.
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(_drain_offline_buffer(self))
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

        # Auto-pause: backend halted this chain due to a runaway-limit trigger.
        # Raise immediately so callers get a clear error rather than cascading
        # 409 responses on every subsequent record_agent_action call.
        if decision_obj.auto_paused:
            raise ChainAutoPausedError(
                message=(
                    f"Chain {self._chain_id} has been auto-paused by the backend. "
                    "No further events can be recorded until the chain is resumed."
                ),
                chain_id=self._chain_id,
                reason=decision_obj.decision_reason or None,
            )

        # Shadow-mode visibility: log when the backend is evaluating in shadow
        # mode so developers can observe would-have-been decisions without
        # needing to inspect every PolicyDecision object manually.
        if decision_obj.evaluation_mode == "shadow" and decision_obj.shadow_decision:
            logger.info(
                "ProofRail shadow mode: action '%s' would have been '%s' under "
                "enforce mode; returning allow per shadow mode (chain=%s)",
                decision_obj.policy_decision,
                decision_obj.shadow_decision,
                self._chain_id,
            )

        if decision_obj.policy_decision == "deny":
            # Kill-switch denials carry a distinct flag so callers can
            # differentiate them from ordinary policy violations.
            if decision_obj.kill_switch_active:
                raise ProofRailKillSwitchError(
                    message=decision_obj.decision_reason
                    or "All agent actions are denied: organisation kill switch is active",
                    reason=decision_obj.pause_reason,
                )
            raise _build_action_denied(
                decision_obj, self._chain_id, self._sequence_number
            )

        if decision_obj.policy_decision == "require_approval":
            approver_notes = (
                await self._poll_for_approval()
            )  # raises if denied or timed out
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
    # Read helpers — fetch chain state from the backend
    # ------------------------------------------------------------------

    async def detail(self) -> ChainDetail:
        """
        Fetch full detail for this chain from the backend.

        Returns
        -------
        ChainDetail
            Full chain record including status, agents_involved, metrics, etc.

        Raises
        ------
        RuntimeError
            If the chain has not been started yet.
        httpx.HTTPStatusError
            On unexpected backend errors.
        """
        if self._chain_id is None:
            raise RuntimeError(
                "Chain has not been started. Use it as a context manager."
            )
        return await _client.get_chain(self._chain_id)

    async def events(
        self,
        limit: int = 100,
        offset: int = 0,
        sequence_after: int | None = None,
    ) -> ChainEventsResponse:
        """
        Fetch a paginated list of events recorded on this chain.

        Parameters
        ----------
        limit : int
            Max events per page (1–500).  Default 100.
        offset : int
            Pagination offset.  Default 0.
        sequence_after : int, optional
            Cursor — return only events with sequence_number > this value.

        Returns
        -------
        ChainEventsResponse
            ``events`` list plus ``total``, ``limit``, ``offset``.

        Raises
        ------
        RuntimeError
            If the chain has not been started yet.
        """
        if self._chain_id is None:
            raise RuntimeError(
                "Chain has not been started. Use it as a context manager."
            )
        return await _client.get_chain_events(
            self._chain_id,
            limit=limit,
            offset=offset,
            sequence_after=sequence_after,
        )

    async def receipt(self) -> ChainReceiptResponse | None:
        """
        Fetch the audit receipt for this chain, if one has been generated.

        Receipts are generated automatically when a chain is completed.  If the
        chain is still active, this returns ``None`` rather than raising.

        Returns
        -------
        ChainReceiptResponse
            The audit receipt, or ``None`` if none exists yet.

        Raises
        ------
        RuntimeError
            If the chain has not been started yet.
        httpx.HTTPStatusError
            On unexpected backend errors (not 404).
        """
        if self._chain_id is None:
            raise RuntimeError(
                "Chain has not been started. Use it as a context manager."
            )
        try:
            return await _client.get_chain_receipt(self._chain_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def verify_receipt(self, receipt_id: str) -> ReceiptVerifyResponse:
        """
        Verify this chain's audit receipt is genuine and untampered.

        Calls the backend's public ``GET /v1/receipts/{id}/verify`` endpoint,
        which re-derives the HMAC server-side and reports whether the receipt's
        ``structured_data`` is intact.

        Parameters
        ----------
        receipt_id : str
            The UUID of the receipt to verify.  Obtain it from the receipts
            list (``GET /v1/receipts``) or from ``ChainReceiptResponse.id``
            once the chain-receipt endpoint exposes that field.

        Returns
        -------
        ReceiptVerifyResponse
            ``valid=True`` means the receipt is untampered.
            ``valid=False`` means tampering was detected.

        Raises
        ------
        RuntimeError
            If the chain has not been started yet.
        httpx.HTTPStatusError
            On 404 (receipt not found) or other HTTP errors.

        Notes
        -----
        The current ``GET /v1/chains/{chain_id}/receipt`` endpoint does not
        return the receipt UUID.  You can retrieve it from
        ``GET /v1/receipts`` filtered by this chain's ID, or from the
        ProofRail dashboard.
        """
        if self._chain_id is None:
            raise RuntimeError(
                "Chain has not been started. Use it as a context manager."
            )
        return await _client.verify_receipt(receipt_id)

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
            "metadata": sanitize_payload(self.metadata, config),
        }

        try:
            response = await _client._post(
                "/v1/chains", body, action_type="chain_create"
            )
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
            self._chain_id,
            self.name,
            self._offline,
        )

    async def _complete(self) -> None:
        """Mark the chain as completed on the backend."""
        if self._chain_id is None:
            return  # Never started (offline or error on entry) — nothing to close.

        if self._offline:
            # Cancel any pending offline drain task to avoid dangling references.
            if self._drain_task is not None and not self._drain_task.done():
                self._drain_task.cancel()
            logger.debug(
                "Offline mode: skipping chain completion for chain %s", self._chain_id
            )
            return

        # Flush any pending fast-path drain before marking the chain complete.
        # Without this, fast-path events buffered near the end of the chain
        # can be lost if the event loop shuts down before the drain task runs
        # (audit finding I-6).
        if self._drain_task is not None and not self._drain_task.done():
            config = _client.get_config()
            buffer_size = len(self._offline_buffer)
            if config.drain_timeout_seconds is not None:
                drain_timeout = float(config.drain_timeout_seconds)
            else:
                # Scale with work to do: 50% headroom over expected drain time,
                # floor of 10 s for empty/small buffers.  BUG-LR-01.
                drain_timeout = max(
                    10.0, buffer_size * config.backend_timeout_seconds * 1.5
                )
            try:
                await asyncio.wait_for(self._drain_task, timeout=drain_timeout)
            except (asyncio.TimeoutError, Exception) as exc:
                actual_dropped = len(self._offline_buffer)
                logger.warning(
                    "Drain task did not finish before chain %s completed: "
                    "%d event(s) dropped (timeout=%.1fs): %s",
                    self._chain_id,
                    actual_dropped,
                    drain_timeout,
                    exc,
                )

        try:
            await _client._post(f"/v1/chains/{self._chain_id}/complete", {})
            logger.debug("Chain completed (id=%s)", self._chain_id)
        except Exception as exc:
            # Completing a chain is best-effort — log but do not mask any
            # exception that is already propagating from the with-block body.
            logger.warning(
                "Failed to mark chain %s as completed: %s", self._chain_id, exc
            )

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
                    "human_approval_denied"
                    if approval_status == "denied"
                    else "approval_timeout"
                )
                default_rem, default_docs = _POLICY_REMEDIATION.get(
                    policy_name, (None, None)
                )
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

        assert self._chain_id is not None  # always set before _poll_for_approval is called
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
        chain_context={"chain_id": chain_id, "sequence": sequence_number}
        if chain_id
        else None,
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


# ---------------------------------------------------------------------------
# Offline-mode buffer drain helper
# ---------------------------------------------------------------------------


async def _drain_offline_buffer(chain: Chain) -> None:
    """
    Drain the offline buffer to the backend, retrying every 30 s until
    the buffer is empty or the chain is closed.

    The first drain attempt is made **immediately** (no initial sleep) so that
    a brief transient error (e.g. a 2-second blip) recovers without a 30-second
    delay.  Subsequent attempts sleep 30 s between tries.

    Recovery: when all buffered events are successfully sent, ``chain._offline``
    is set to ``False`` and the task exits.  Future ``record_agent_action``
    calls will resume normal synchronous backend contact.

    Not started for chains that went offline at ``_start()`` — those have a
    locally-generated UUID that doesn't exist on the backend; draining them
    would require first re-creating the chain (replay, out of scope for BUG-01).

    Never raises — swallows all exceptions.
    """
    first_attempt = True

    while chain._offline and chain._offline_buffer and chain._chain_id is not None:
        if not first_attempt:
            await asyncio.sleep(30)

        first_attempt = False

        if not chain._offline_buffer or chain._chain_id is None:
            break

        # Attempt to drain the full buffer in one pass.
        sent_count = 0
        while chain._offline_buffer:
            event_body = chain._offline_buffer[0]
            try:
                await _client._post(
                    f"/v1/chains/{chain._chain_id}/events",
                    event_body,
                    action_type=event_body.get("action_type"),
                )
                chain._offline_buffer.pop(0)
                sent_count += 1
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (400, 401, 403, 404, 422):
                    # Permanent client error — this event can never succeed.
                    # 404: chain should exist by the time events fire — backend
                    # confirms creation before returning. A 404 here means the
                    # chain was deleted mid-flight or DB inconsistency. Discard
                    # rather than spin forever.
                    try:
                        body_preview = exc.response.text[:200]
                    except Exception:
                        body_preview = "<unreadable>"
                    logger.warning(
                        "Offline drain: discarding event with permanent error "
                        "(chain=%s action=%r status=%d body=%s)",
                        chain._chain_id,
                        event_body.get("action_name"),
                        status,
                        body_preview,
                    )
                    chain._offline_buffer.pop(0)
                    sent_count += 1
                    continue
                else:
                    # Transient (429, 5xx) — stop this pass, retry in 30s.
                    logger.debug(
                        "Offline drain: transient error %d (chain=%s): %s — retrying in 30s",
                        status,
                        chain._chain_id,
                        exc,
                    )
                    break
            except Exception as exc:
                logger.debug(
                    "Offline drain: send failed (chain=%s): %s — retrying in 30s",
                    chain._chain_id,
                    exc,
                )
                break

        if not chain._offline_buffer:
            # Buffer fully drained — backend is back, mark chain as recovered.
            chain._offline = False
            logger.info(
                "ProofRail SDK: backend recovered, offline buffer drained "
                "(chain=%s sent=%d)",
                chain._chain_id,
                sent_count,
            )
            break
