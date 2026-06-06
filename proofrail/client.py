"""
proofrail.client — SDK client initialization, configuration singleton, and
low-level HTTP transport to the ProofRail backend.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
import weakref
from urllib.parse import urlencode

import httpx

from proofrail.exceptions import BackendUnavailableError
from proofrail.models import (
    ChainConfig,
    ChainDetail,
    ChainEventsResponse,
    ChainListResponse,
    ChainReceiptResponse,
    ReceiptVerifyResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_config: ChainConfig | None = None
# Loop-aware client cache: one httpx.AsyncClient per event loop.  WeakKeyDictionary
# ensures entries are removed automatically when a loop is garbage-collected, so
# short-lived loops (e.g. asyncio.Runner()) never accumulate stale clients.
_clients_by_loop: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# Internal signal
# ---------------------------------------------------------------------------

class _OfflineSignal(Exception):
    """
    Raised by _handle_backend_failure when the backend is unreachable and the
    resolved fail_mode is "allow".  chain.py catches this in _start() and in
    record_agent_action() to transition the chain to offline mode rather than
    returning a synthetic dict that is missing fields callers expect (e.g.
    the "id" key on a chain-create response).
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init(**kwargs) -> ChainConfig:
    """
    Initialize the ProofRail SDK with the given configuration values.

    Must be called once before creating any Chain.  Calling it again with
    different values reconfigures the SDK and resets the HTTP client.

    Parameters
    ----------
    api_key : str  (required)
        The ProofRail API key generated from the dashboard.
    **kwargs :
        Any field accepted by ``ChainConfig`` (see models.py).

    Returns
    -------
    ChainConfig
        The validated config object that was stored.

    Raises
    ------
    ValueError
        If ``api_key`` is not provided or is empty.
    """
    global _config

    if not kwargs.get("api_key"):
        raise ValueError(
            "api_key is required. Call proofrail.init(api_key='prail_...') before using the SDK."
        )

    _config = ChainConfig(**kwargs)

    # On re-init: gracefully close the current loop's client (if we're inside
    # an async context) so that TCP connections and file descriptors are
    # released.  Then clear every cached client so the next _get_client() call
    # creates a fresh instance with the updated config.
    #
    # Clients bound to *other* loops are dropped from the WeakKeyDictionary
    # here.  Their resources are reclaimed by httpx + GC when those loops are
    # eventually garbage-collected.  This is the standard httpx lifecycle for
    # short-lived loops (e.g. asyncio.Runner()) and is safe for our usage.
    try:
        loop = asyncio.get_running_loop()
        old_client = _clients_by_loop.get(loop)
        if old_client is not None:
            # Fire-and-forget: schedule aclose on the current loop.
            loop.create_task(old_client.aclose())
    except RuntimeError:
        # No running event loop — nothing to close explicitly.
        pass
    _clients_by_loop.clear()

    # Warn loudly when plaintext HTTP is used against a production environment.
    # Governance audit data sent over HTTP is vulnerable to interception.
    if (
        _config.environment == "production"
        and _config.backend_url.startswith("http://")
    ):
        warnings.warn(
            f"ProofRail SDK: backend_url uses plaintext HTTP ({_config.backend_url!r}) "
            "in environment='production'. All audit data will be transmitted unencrypted. "
            "Switch to an https:// URL for production deployments.",
            UserWarning,
            stacklevel=2,
        )

    logger.debug("ProofRail SDK initialized (environment=%s)", _config.environment)
    return _config


def get_config() -> ChainConfig:
    """
    Return the current ``ChainConfig`` singleton.

    Raises
    ------
    RuntimeError
        If ``init()`` has not been called yet.
    """
    if _config is None:
        raise RuntimeError(
            "proofrail has not been initialized. Call proofrail.init(api_key='prail_...') first."
        )
    return _config


def _get_client() -> httpx.AsyncClient:
    """Return the httpx.AsyncClient bound to the currently running event loop.

    Each asyncio event loop gets its own client instance, stored in
    ``_clients_by_loop``.  The first call for a given loop creates and caches
    the client; subsequent calls return the cached instance.

    This prevents "asyncio.Event bound to a different event loop" errors that
    occurred when a single global client was shared across loops — the root
    cause of BUG-LC-03.

    Raises
    ------
    RuntimeError
        If ``init()`` has not been called, or if there is no running event loop
        (i.e. called from a non-async context without an active loop).
    """
    if _config is None:
        raise RuntimeError(
            "proofrail has not been initialized. Call proofrail.init(api_key='prail_...') first."
        )
    loop = asyncio.get_running_loop()  # raises RuntimeError if no running loop
    client = _clients_by_loop.get(loop)
    if client is None:
        client = httpx.AsyncClient(
            base_url=_config.backend_url,
            timeout=httpx.Timeout(_config.backend_timeout_seconds),
            headers={
                "Authorization": f"Bearer {_config.api_key}",
                "Content-Type": "application/json",
            },
        )
        _clients_by_loop[loop] = client
    return client


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _get_retry_after_ms(response: httpx.Response, default_ms: int) -> int:
    """
    Extract the Retry-After header value in milliseconds.

    Accepts integer seconds (``Retry-After: 2``) or floating-point seconds.
    Returns *default_ms* when the header is absent or unparseable.
    """
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return int(float(retry_after) * 1000)
        except (ValueError, TypeError):
            pass
    return default_ms


async def _retry_with_backoff(
    coro_factory,
    max_retries: int,
    backoff_base_ms: int,
) -> httpx.Response:
    """
    Execute ``coro_factory()`` with exponential-backoff retries for transient
    errors.

    Retry policy
    ------------
    * **Retryable:** ``httpx.TimeoutException``, ``httpx.ConnectError``,
      5xx responses (500/502/503/504), 429 with optional Retry-After.
    * **Non-retryable (immediate return):** 2xx success or 4xx client error.

    After all retries are exhausted on a network exception, re-raises the
    last exception so the caller can call ``_handle_backend_failure``.

    After all retries are exhausted on a 5xx/429 response, returns the last
    response so the caller can call ``_handle_backend_failure``.

    Each retry attempt is logged at INFO level so developers can observe retry
    behaviour in their logs.
    """
    for attempt in range(max_retries + 1):
        try:
            response: httpx.Response = await coro_factory()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < max_retries:
                backoff_ms = backoff_base_ms * (2 ** attempt)
                logger.info(
                    "ProofRail SDK retry attempt %d/%d after %dms: %s",
                    attempt + 1, max_retries, backoff_ms, exc,
                )
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            raise  # exhausted — let caller call _handle_backend_failure

        if response.status_code in (500, 502, 503, 504):
            if attempt < max_retries:
                backoff_ms = backoff_base_ms * (2 ** attempt)
                logger.info(
                    "ProofRail SDK retry attempt %d/%d after %dms: HTTP %d",
                    attempt + 1, max_retries, backoff_ms, response.status_code,
                )
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            # Exhausted retries on 5xx — return for caller to handle via fail_mode.

        elif response.status_code == 429:
            if attempt < max_retries:
                backoff_ms = _get_retry_after_ms(
                    response, backoff_base_ms * (2 ** attempt)
                )
                logger.info(
                    "ProofRail SDK retry attempt %d/%d after %dms: HTTP 429",
                    attempt + 1, max_retries, backoff_ms,
                )
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            # Exhausted retries on 429 — return for caller to handle via fail_mode.

        return response

    raise RuntimeError("unreachable")  # loop always returns or raises above


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _post(path: str, data: dict, action_type: str | None = None) -> dict:
    """
    POST *data* as JSON to *path* on the configured backend.

    Retries up to ``config.max_retries`` times (exponential backoff, base
    ``config.retry_backoff_base_ms`` ms) on transient errors: network
    timeouts, connection failures, 5xx responses, and 429 rate limits.

    After all retries are exhausted:
    * Network/5xx/429 → ``fail_mode`` determines behaviour: ``"deny"`` raises
      BackendUnavailableError; ``"allow"`` raises _OfflineSignal so chain.py
      can transition to offline mode cleanly.

    4xx responses are non-retryable and always re-raise ``HTTPStatusError``
    immediately — these are deterministic client errors where retry won't help.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await _retry_with_backoff(
            lambda: client.post(path, json=data),
            config.max_retries,
            config.retry_backoff_base_ms,
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        _handle_backend_failure(
            f"Backend request failed after {config.max_retries + 1} attempts "
            f"(POST {path}): {exc}",
            config,
            action_type,
        )

    if response.status_code in (500, 502, 503, 504, 429):
        _handle_backend_failure(
            f"Backend returned HTTP {response.status_code} after "
            f"{config.max_retries + 1} attempts (POST {path})",
            config,
            action_type,
        )

    response.raise_for_status()  # 4xx → HTTPStatusError; non-retryable, no fail_mode
    return response.json()


async def _get(path: str, action_type: str | None = None) -> dict:
    """
    GET *path* on the configured backend.

    Same retry and fail_mode semantics as ``_post``.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await _retry_with_backoff(
            lambda: client.get(path),
            config.max_retries,
            config.retry_backoff_base_ms,
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        _handle_backend_failure(
            f"Backend request failed after {config.max_retries + 1} attempts "
            f"(GET {path}): {exc}",
            config,
            action_type,
        )

    if response.status_code in (500, 502, 503, 504, 429):
        _handle_backend_failure(
            f"Backend returned HTTP {response.status_code} after "
            f"{config.max_retries + 1} attempts (GET {path})",
            config,
            action_type,
        )

    response.raise_for_status()  # 4xx → HTTPStatusError; non-retryable
    return response.json()


# ---------------------------------------------------------------------------
# Public read API — chain detail, events, receipt, list
# ---------------------------------------------------------------------------

async def get_chain(chain_id: str) -> ChainDetail:
    """
    Fetch full detail for a single chain.

    Parameters
    ----------
    chain_id : str
        The backend-assigned chain UUID string.

    Returns
    -------
    ChainDetail
        Full chain detail including status, metrics, and metadata.

    Raises
    ------
    httpx.HTTPStatusError
        On 404 (chain not found) or 403 (forbidden).
    """
    data = await _get(f"/v1/chains/{chain_id}")
    return ChainDetail.model_validate(data)


async def get_chain_events(
    chain_id: str,
    limit: int = 100,
    offset: int = 0,
    sequence_after: int | None = None,
) -> ChainEventsResponse:
    """
    Fetch a page of events for a chain.

    Parameters
    ----------
    chain_id : str
        The backend-assigned chain UUID string.
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
    """
    params: dict = {"limit": limit, "offset": offset}
    if sequence_after is not None:
        params["sequence_after"] = sequence_after
    path = f"/v1/chains/{chain_id}/events?{urlencode(params)}"
    data = await _get(path)
    return ChainEventsResponse.model_validate(data)


async def get_chain_receipt(chain_id: str) -> ChainReceiptResponse:
    """
    Fetch the audit receipt for a completed chain.

    Parameters
    ----------
    chain_id : str
        The backend-assigned chain UUID string.

    Returns
    -------
    ChainReceiptResponse
        Receipt fields including ``receipt_number``, ``signature``, and
        ``previous_receipt_hash`` for hash-chain verification.

    Raises
    ------
    httpx.HTTPStatusError
        On 404 if the chain has no receipt yet (check ``chain.status``),
        or 403 if the caller's org does not own this chain.
    """
    data = await _get(f"/v1/chains/{chain_id}/receipt")
    return ChainReceiptResponse.model_validate(data)


async def verify_receipt(receipt_id: str) -> ReceiptVerifyResponse:
    """
    Verify a receipt's signature against the backend's signing key.

    Calls the public (no auth required) ``GET /v1/receipts/{id}/verify``
    endpoint.  HMAC validation is performed server-side; the signing secret is
    never shared with the SDK.

    Parameters
    ----------
    receipt_id : str
        The receipt's backend UUID.  Obtain it from the receipts list endpoint
        (``GET /v1/receipts``) or from ``ChainReceiptResponse.id`` when the
        backend chain-receipt endpoint exposes it.

    Returns
    -------
    ReceiptVerifyResponse
        ``valid=True`` means the receipt's structured_data matches the
        server-side HMAC.  ``valid=False`` means tampering was detected.

    Raises
    ------
    httpx.HTTPStatusError
        On 404 (receipt not found) or other HTTP errors.
    """
    data = await _get(f"/v1/receipts/{receipt_id}/verify")
    return ReceiptVerifyResponse.model_validate(data)


async def list_chains(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    environment: str | None = None,
) -> ChainListResponse:
    """
    Fetch a paginated list of chains for the caller's organisation.

    Parameters
    ----------
    limit : int
        Max chains per page (1–200).  Default 50.
    offset : int
        Pagination offset.  Default 0.
    status : str, optional
        Filter by chain status, e.g. ``"completed"``, ``"active"``.
    environment : str, optional
        Filter by environment, e.g. ``"production"``, ``"development"``.

    Returns
    -------
    ChainListResponse
        ``chains`` list of :class:`ChainSummary` plus ``total``, ``limit``, ``offset``.
    """
    params: dict = {"limit": limit, "offset": offset}
    if status is not None:
        params["status"] = status
    if environment is not None:
        params["environment"] = environment
    path = f"/v1/chains?{urlencode(params)}"
    data = await _get(path)
    return ChainListResponse.model_validate(data)


def _handle_backend_failure(
    message: str,
    config: ChainConfig,
    action_type: str | None = None,
) -> None:
    """
    Apply the resolved fail_mode to a transport-level backend failure.

    ``"allow"`` raises _OfflineSignal so the caller (chain.py) can transition
    to offline mode without receiving a synthetic dict that is missing fields
    (e.g. "id" on a chain-create response).
    ``"deny"`` raises BackendUnavailableError.
    """
    effective = config.resolve_fail_mode(action_type)
    if effective == "allow":
        logger.warning(
            "%s — fail_mode=allow (action_type=%s), transitioning to offline mode",
            message,
            action_type or "unspecified",
        )
        raise _OfflineSignal(message)

    raise BackendUnavailableError(message=message, fail_mode=effective)
