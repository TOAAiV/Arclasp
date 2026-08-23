"""
arclasp.chain — Agent chain tracking and execution context.

Usage (async — preferred):
    async with arclasp.Chain("order-processing", metadata={"order_id": "123"}) as chain:
        await chain.record_agent_action(
            agent_name="pricing-agent",
            action_type="calculation",
            action_name="apply_discount",
            payload={"discount_pct": 15},
        )

Usage (sync — for non-async scripts only):
    with arclasp.Chain("order-processing") as chain:
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

from arclasp import client as _client
from arclasp.exceptions import (
    ActionDeniedError,
    ChainAutoPausedError,
    ChainCompletionError,
    ChainTimeoutError,
    ArclaspKillSwitchError,
    _POLICY_REMEDIATION,
)
from arclasp.models import (
    ChainDetail,
    ChainEventsResponse,
    ChainReceiptResponse,
    PolicyDecision,
)
from arclasp.sanitization import sanitize_payload

logger = logging.getLogger(__name__)

# Sentinel UUID sent in the ChainCreate body.  The backend derives the real
# organization_id from the API key; this field is validated but ignored.
_ORG_ID_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Legacy buffer helper
# ---------------------------------------------------------------------------


def _buffer_event(buffer: list, event_body: dict, max_events: int) -> bool:
    """
    Append *event_body* to *buffer*, evicting the oldest entry when full.

    When the buffer is at capacity, the oldest (index 0) event is dropped and
    a warning is logged before the new event is appended.  Per spec section 12,
    oldest events are dropped first so the audit trail stays as current as
    possible under pressure.

    Always returns True (the event is always accepted).

    This helper is retained for compatibility with legacy internal tests and
    transitional drain code. Public governed execution no longer uses it to
    convert backend unavailability into an allow.
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
    Context manager that wraps a Arclasp chain lifecycle.

    Supports both ``async with`` (preferred) and ``with`` (sync scripts only).
    The sync interface uses ``asyncio.run()`` internally and will raise a
    ``RuntimeError`` if called from inside a running event loop — use
    ``async with`` in that case.
    """

    def __init__(
        self,
        name: str,
        metadata: dict | None = None,
        policy_config: dict | None = None,
    ) -> None:
        self.name = name
        self.metadata: dict = metadata or {}
        # Per-chain policy override, merged by the backend key-by-key over
        # the org-wide config (chain value wins where set). None/{} = no
        # override — behaves identically to the org-wide config alone.
        # Build with the add_financial_threshold() facade, or pass a raw
        # dict directly for the recognised keys (see add_financial_threshold
        # docstring for the schema).
        self.policy_config: dict = dict(policy_config) if policy_config else {}

        self._chain_id: str | None = None
        self._sequence_number: int = 1
        # Legacy internal state retained for transitional drain helpers only.
        # Public governed execution must be backed by an authoritative backend chain.
        self._offline: bool = False
        self._offline_buffer: list[dict] = []
        self._cumulative_metrics: dict = {}
        # Single-flight drain task - at most one drain coroutine runs at a time.
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
        await self._complete_for_async_exit(raise_on_failure=exc_type is None)
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
        asyncio.run(self._complete(raise_on_failure=exc_type is None))
        return False

    # ------------------------------------------------------------------
    # Policy configuration facade
    # ------------------------------------------------------------------

    def add_financial_threshold(
        self,
        usd: float,
        notify: list[str] | None = None,
        deny: bool = False,
    ) -> None:
        """
        Configure a per-chain cumulative financial threshold, overriding the
        org-wide default for this chain only.

        Must be called before the chain is started (i.e. before entering the
        ``async with`` / ``with`` block) — the resulting ``policy_config`` is
        sent once, in the ``POST /v1/chains`` body.

        Parameters
        ----------
        usd : float
            The cumulative spend threshold for this chain, in USD. Overrides
            the org-wide ``cumulative_financial_threshold_usd`` (or its
            backend default) for this chain only.
        notify : list[str], optional
            Additional approver emails to notify when this chain's threshold
            crosses. Unioned with this chain's ``fallback_approvers`` and
            deduplicated — does not replace them.
        deny : bool, default False
            If True, crossing this threshold denies the action outright
            instead of pausing for human approval.

        This is equivalent to constructing the chain with:

            Chain("name", policy_config={
                "cumulative_financial_threshold_usd": usd,
                "notify": notify,  # only if notify is truthy
                "cumulative_financial_threshold_action": "deny",  # only if deny
            })
        """
        self.policy_config["cumulative_financial_threshold_usd"] = usd

        if notify:
            existing: list[str] = list(self.policy_config.get("notify", []))
            for email in notify:
                if email not in existing:
                    existing.append(email)
            self.policy_config["notify"] = existing

        if deny:
            self.policy_config["cumulative_financial_threshold_action"] = "deny"

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
            The resolved policy decision from the backend. Common allow outcomes:

            * ``policy_decision="allow"`` - action permitted by backend policy.
            * ``policy_decision="allow", decision_source="human_approval"`` -
              action was initially gated for human review and a reviewer approved
              it. ``decision_reason`` will contain the approver's notes when
              provided.

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
            raise _client.BackendUnavailableError(
                message=(
                    "Chain is not backed by an authoritative backend session; "
                    "governed actions cannot execute offline."
                ),
            )

        response = await _client._post(
            f"/v1/chains/{self._chain_id}/events",
            event_body,
            action_type=action_type,
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
                "Arclasp shadow mode: action '%s' would have been '%s' under "
                "enforce mode; returning allow per shadow mode (chain=%s)",
                decision_obj.policy_decision,
                decision_obj.shadow_decision,
                self._chain_id,
            )

        if decision_obj.policy_decision == "deny":
            # Kill-switch denials carry a distinct flag so callers can
            # differentiate them from ordinary policy violations.
            if decision_obj.kill_switch_active:
                raise ArclaspKillSwitchError(
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
            "fallback_approvers": config.fallback_approvers,
            # Per-chain override (Chain(policy_config={...}) or
            # add_financial_threshold()). {} = no override, org-wide config
            # from arclasp.init() applies unchanged.
            "policy_config": self.policy_config,
        }

        creation_idempotency_key = uuid.uuid4().hex
        response = await _client._post(
            "/v1/chains",
            body,
            action_type="chain_create",
            headers={"Idempotency-Key": creation_idempotency_key},
        )
        self._chain_id = response["id"]

        logger.debug(
            "Chain started (id=%s name=%s offline=%s)",
            self._chain_id,
            self.name,
            self._offline,
        )

    async def _complete(self, *, raise_on_failure: bool = True) -> None:
        """Mark the chain as completed on the backend."""
        if self._chain_id is None:
            return  # Never started (offline or error on entry) — nothing to close.

        if self._offline:
            # Transitional guard for pre-K1 internal state only. Public governed
            # execution no longer creates offline chains.
            if self._drain_task is not None and not self._drain_task.done():
                self._drain_task.cancel()
            logger.warning(
                "Skipping completion for non-authoritative offline chain %s",
                self._chain_id,
            )
            return

        # Flush any transitional pending drain before marking the chain complete.
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
            response = await _client._post(f"/v1/chains/{self._chain_id}/complete", {})
            if not isinstance(response, dict) or response.get("status") != "completed":
                raise ValueError("completion response did not confirm completed status")
            logger.debug("Chain completed (id=%s)", self._chain_id)
        except Exception as exc:
            logger.warning(
                "Failed to mark chain %s as completed: %s", self._chain_id, exc
            )
            if raise_on_failure:
                raise ChainCompletionError(
                    self._chain_id,
                    "Authoritative chain completion could not be confirmed",
                ) from exc

    async def _complete_for_async_exit(self, *, raise_on_failure: bool = True) -> None:
        """
        Run async context-manager completion without orphaning it on cancellation.

        ``asyncio.shield`` alone keeps the inner completion alive, but still
        raises ``CancelledError`` to the outer task immediately.  We keep a
        strong reference to the single completion task, continue awaiting that
        same task until it settles, observe its result/exception, and only then
        re-raise cancellation.
        """
        completion_task = asyncio.create_task(
            self._complete(raise_on_failure=raise_on_failure)
        )
        cancellation: asyncio.CancelledError | None = None

        while not completion_task.done():
            try:
                await asyncio.shield(completion_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                continue
            except Exception:
                if cancellation is not None:
                    break
                raise

        if cancellation is not None:
            try:
                completion_task.result()
            except asyncio.CancelledError:
                logger.warning(
                    "Chain completion task was cancelled while closing chain %s",
                    self._chain_id,
                )
            except Exception as exc:
                logger.warning(
                    "Chain completion failed after cancellation for chain %s: %s",
                    self._chain_id,
                    exc,
                )
            raise cancellation

        completion_task.result()

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
                approver_notes = approvals_list[0].get("decision_notes") or None

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
# Transitional async drain helper
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
# Transitional offline-mode buffer drain helper
# ---------------------------------------------------------------------------


async def _drain_offline_buffer(chain: Chain) -> None:
    """
    Drain a legacy offline buffer to the backend, retrying every 30 s until
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
                "Arclasp SDK: backend recovered, offline buffer drained "
                "(chain=%s sent=%d)",
                chain._chain_id,
                sent_count,
            )
            break
