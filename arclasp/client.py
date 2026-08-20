"""
arclasp.client — SDK client initialization, configuration singleton, and
low-level HTTP transport to the Arclasp backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import warnings
import weakref
from contextvars import ContextVar
from datetime import datetime
from typing import NoReturn
from urllib.parse import urlencode

import httpx

from arclasp.exceptions import BackendUnavailableError, ArclaspVerificationError
from arclasp.models import (
    AuthenticatedVerificationResponse,
    ChainConfig,
    ChainDetail,
    ChainEventsResponse,
    ChainListResponse,
    ChainReceiptResponse,
    PublicVerificationResponse,
    PublicVerificationTokenCreateResponse,
    PublicVerificationTokenListResponse,
    PublicVerificationTokenMetadata,
    ReceiptVerifyResponse,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Localhost URL detection — exempt from the HTTP plaintext warning.
# startswith() covers port variants automatically (e.g. http://localhost:9999).
# ---------------------------------------------------------------------------

_LOCALHOST_HTTP_PREFIXES: tuple[str, ...] = (
    "http://localhost",
    "http://127.0.0.1",
    "http://0.0.0.0",
    "http://[::1]",
)


def _is_localhost_url(url: str) -> bool:
    """Return True when *url* points at a local loopback address."""
    return any(url.startswith(p) for p in _LOCALHOST_HTTP_PREFIXES)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_config: ChainConfig | None = None
# Loop-aware client cache: one httpx.AsyncClient per event loop.  WeakKeyDictionary
# ensures entries are removed automatically when a loop is garbage-collected, so
# short-lived loops (e.g. asyncio.Runner()) never accumulate stale clients.
_clients_by_loop: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# K0B internal benchmark hook (explicit opt-in only — not public SDK surface)
#
# Zero overhead for ordinary callers: the contextvar defaults to None, and
# every recording site is a cheap `is not None` check. Only a caller that
# explicitly enters `_benchmark_sample()` (e.g. the K0B verification script)
# ever populates a sample. Records only low-cardinality transport metadata —
# retry count, client created/reused, the Server-Timing header value if
# present, and response status/exception category — never chain IDs,
# payloads, or secrets.
# ---------------------------------------------------------------------------

_benchmark_ctx: ContextVar["_BenchmarkSample | None"] = ContextVar(
    "_arclasp_benchmark_ctx", default=None
)


class _BenchmarkSample:
    """Internal opt-in transport-metadata sample for one authoritative request.

    Not part of the public SDK surface — never imported or documented outside
    internal benchmark tooling.
    """

    __slots__ = (
        "retries",
        "client_created",
        "server_timing_header",
        "status_code",
        "exception_category",
    )

    def __init__(self) -> None:
        self.retries: int = 0
        self.client_created: bool | None = None
        self.server_timing_header: str | None = None
        self.status_code: int | None = None
        self.exception_category: str | None = None


@contextlib.contextmanager
def _benchmark_sample():
    """Collect a `_BenchmarkSample` for the single authoritative request(s)
    made while this context is active. Internal opt-in only."""
    sample = _BenchmarkSample()
    token = _benchmark_ctx.set(sample)
    try:
        yield sample
    finally:
        _benchmark_ctx.reset(token)


# ---------------------------------------------------------------------------
# Internal signal
# ---------------------------------------------------------------------------


class _OfflineSignal(Exception):
    """Legacy internal signal retained for older internal imports.

    Governed SDK transport no longer emits this signal; backend unavailability
    raises BackendUnavailableError.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init(**kwargs) -> ChainConfig:
    """
    Initialize the Arclasp SDK with the given configuration values.

    Must be called once before creating any Chain.  Calling it again with
    different values reconfigures the SDK and resets the HTTP client.

    Parameters
    ----------
    api_key : str  (required)
        The Arclasp API key generated from the dashboard.
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
            "api_key is required. Call arclasp.init(api_key='prail_...') before using the SDK."
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

    # Warn when plaintext HTTP is used against a non-localhost backend.
    # Localhost URLs are exempt (development/testing). All other HTTP backends
    # transmit governance audit data unencrypted regardless of environment.
    if _config.backend_url.startswith("http://") and not _is_localhost_url(
        _config.backend_url
    ):
        logger.warning(
            "Arclasp SDK: backend_url uses plaintext HTTP (%r). "
            "All audit data will be transmitted unencrypted. "
            "Switch to an https:// URL for non-local deployments.",
            _config.backend_url,
        )

    logger.debug("Arclasp SDK initialized (environment=%s)", _config.environment)
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
            "arclasp has not been initialized. Call arclasp.init(api_key='prail_...') first."
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
            "arclasp has not been initialized. Call arclasp.init(api_key='prail_...') first."
        )
    loop = asyncio.get_running_loop()  # raises RuntimeError if no running loop
    sample = _benchmark_ctx.get()
    if sample is not None and sample.client_created is None:
        sample.client_created = loop not in _clients_by_loop
    client = _clients_by_loop.get(loop)
    if client is None:
        client = httpx.AsyncClient(
            base_url=_config.backend_url,
            timeout=httpx.Timeout(_config.backend_timeout_seconds),
            headers={
                "Authorization": f"Bearer {_config.api_key.get_secret_value()}",
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
    * **Retryable:** ``httpx.TimeoutException``, ``httpx.NetworkError``
      (which covers ``ConnectError``, ``ReadError``, and ``WriteError``),
      5xx responses (500/502/503/504), 429 with optional Retry-After.
    * **Non-retryable — propagates immediately:** ``httpx.RemoteProtocolError``
      and ``httpx.DecodingError`` indicate backend health issues (malformed
      HTTP, bad response body) that should surface to the caller for
      the caller rather than silently retrying forever.
    * **Non-retryable (immediate return):** 2xx success or 4xx client error.

    After all retries are exhausted on a network exception, re-raises the
    last exception so the caller can call ``_handle_backend_failure``.

    After all retries are exhausted on a 5xx/429 response, returns the last
    response so the caller can call ``_handle_backend_failure``.

    Each retry attempt is logged at INFO level so developers can observe retry
    behaviour in their logs.
    """
    sample = _benchmark_ctx.get()

    for attempt in range(max_retries + 1):
        try:
            response: httpx.Response = await coro_factory()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt < max_retries:
                backoff_ms = backoff_base_ms * (2**attempt)
                logger.info(
                    "Arclasp SDK retry attempt %d/%d after %dms: %s",
                    attempt + 1,
                    max_retries,
                    backoff_ms,
                    exc,
                )
                if sample is not None:
                    sample.retries += 1
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            raise  # exhausted — let caller call _handle_backend_failure

        if response.status_code in (500, 502, 503, 504):
            if attempt < max_retries:
                backoff_ms = backoff_base_ms * (2**attempt)
                logger.info(
                    "Arclasp SDK retry attempt %d/%d after %dms: HTTP %d",
                    attempt + 1,
                    max_retries,
                    backoff_ms,
                    response.status_code,
                )
                if sample is not None:
                    sample.retries += 1
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            # Exhausted retries on 5xx — return for caller to fail closed.

        elif response.status_code == 429:
            if attempt < max_retries:
                backoff_ms = _get_retry_after_ms(
                    response, backoff_base_ms * (2**attempt)
                )
                logger.info(
                    "Arclasp SDK retry attempt %d/%d after %dms: HTTP 429",
                    attempt + 1,
                    max_retries,
                    backoff_ms,
                )
                if sample is not None:
                    sample.retries += 1
                await asyncio.sleep(backoff_ms / 1000.0)
                continue
            # Exhausted retries on 429 — return for caller to fail closed.

        return response

    raise RuntimeError("unreachable")  # loop always returns or raises above


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


async def _post(path: str, data: dict, action_type: str | None = None) -> dict:
    """
    POST *data* as JSON to *path* on the configured backend.

    Retries up to ``config.max_retries`` times (exponential backoff, base
    ``config.retry_backoff_base_ms`` ms) on transient errors: timeouts,
    ``NetworkError`` (connect / read / write failures), 5xx, and 429.

    After all retries are exhausted, network errors, 5xx responses, and 429
    responses raise BackendUnavailableError. Governed execution always fails
    closed when backend authority is unavailable.

    4xx responses are non-retryable and always re-raise ``HTTPStatusError``
    immediately — these are deterministic client errors where retry won't help.
    """
    config = get_config()
    client = _get_client()
    sample = _benchmark_ctx.get()

    try:
        response = await _retry_with_backoff(
            lambda: client.post(path, json=data),
            config.max_retries,
            config.retry_backoff_base_ms,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        if sample is not None:
            sample.exception_category = type(exc).__name__
        _handle_backend_failure(
            f"Backend request failed after {config.max_retries + 1} attempts "
            f"(POST {path}): {exc}",
            config,
            action_type,
        )

    if sample is not None:
        sample.status_code = response.status_code
        sample.server_timing_header = response.headers.get("Server-Timing")

    if response.status_code in (500, 502, 503, 504, 429):
        _handle_backend_failure(
            f"Backend returned HTTP {response.status_code} after "
            f"{config.max_retries + 1} attempts (POST {path})",
            config,
            action_type,
        )

    response.raise_for_status()  # 4xx -> HTTPStatusError; non-retryable
    return response.json()


async def _get(path: str, action_type: str | None = None) -> dict:
    """
    GET *path* on the configured backend.

    Same retry and fail-closed semantics as ``_post``.
    """
    config = get_config()
    client = _get_client()
    sample = _benchmark_ctx.get()

    try:
        response = await _retry_with_backoff(
            lambda: client.get(path),
            config.max_retries,
            config.retry_backoff_base_ms,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        if sample is not None:
            sample.exception_category = type(exc).__name__
        _handle_backend_failure(
            f"Backend request failed after {config.max_retries + 1} attempts "
            f"(GET {path}): {exc}",
            config,
            action_type,
        )

    if sample is not None:
        sample.status_code = response.status_code
        sample.server_timing_header = response.headers.get("Server-Timing")

    if response.status_code in (500, 502, 503, 504, 429):
        _handle_backend_failure(
            f"Backend returned HTTP {response.status_code} after "
            f"{config.max_retries + 1} attempts (GET {path})",
            config,
            action_type,
        )

    response.raise_for_status()  # 4xx → HTTPStatusError; non-retryable
    return response.json()


async def _post_once(path: str, data: dict) -> dict:
    """
    POST *data* without automatic retries.

    Used for mutation endpoints where replaying a successful-but-interrupted
    request could create duplicate state, such as one-time public token issue.
    """
    client = _get_client()
    response = await client.post(path, json=data)
    response.raise_for_status()
    return response.json()


async def _get_unauthenticated_json(path: str) -> dict:
    """GET public JSON using the configured base URL without auth headers."""
    config = get_config()
    async with httpx.AsyncClient(
        base_url=config.backend_url,
        timeout=httpx.Timeout(config.backend_timeout_seconds),
        headers={"Content-Type": "application/json"},
    ) as client:
        response = await client.get(path)
    response.raise_for_status()
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
    Deprecated compatibility helper for legacy public receipt verification.

    Calls the legacy public ``GET /v1/receipts/{id}/verify`` endpoint. HMAC
    validation is performed server-side; the signing secret is never shared
    with the SDK. Prefer ``verify_receipt_v2(receipt_id)`` for authenticated,
    organization-scoped receipt verification.
    """
    warnings.warn(
        "arclasp.client.verify_receipt() uses the deprecated legacy receipt verifier; "
        "use verify_receipt_v2() for authenticated v2 verification.",
        DeprecationWarning,
        stacklevel=2,
    )
    data = await _get(f"/v1/receipts/{receipt_id}/verify")
    return ReceiptVerifyResponse.model_validate(data)


async def verify_approval_v2(approval_id: str) -> AuthenticatedVerificationResponse:
    """Verify an approval through the authenticated role-aware v2 contract."""
    data = await _get(f"/v1/verification/v2/approvals/{approval_id}")
    return AuthenticatedVerificationResponse.model_validate(data)


async def verify_receipt_v2(receipt_id: str) -> AuthenticatedVerificationResponse:
    """Verify a receipt through the authenticated role-aware v2 contract."""
    data = await _get(f"/v1/verification/v2/receipts/{receipt_id}")
    return AuthenticatedVerificationResponse.model_validate(data)


async def list_public_verification_tokens(
    limit: int = 50,
    offset: int = 0,
) -> PublicVerificationTokenListResponse:
    """List public verification token metadata for the authenticated organization."""
    params = {"limit": limit, "offset": offset}
    data = await _get(f"/v1/public-verification-tokens?{urlencode(params)}")
    return PublicVerificationTokenListResponse.model_validate(data)


async def issue_public_verification_token(
    artifact_type: str,
    artifact_id: str,
    expires_at: datetime | str | None = None,
) -> PublicVerificationTokenCreateResponse:
    """
    Issue a tokenized public verification link.

    The plaintext token is returned by the server once. This helper does not
    retry automatically and does not log or persist the plaintext token.
    """
    body: dict = {"artifact_type": artifact_type, "artifact_id": artifact_id}
    if expires_at is not None:
        body["expires_at"] = expires_at.isoformat() if hasattr(expires_at, "isoformat") else expires_at
    data = await _post_once("/v1/public-verification-tokens", body)
    return PublicVerificationTokenCreateResponse.model_validate(data)


async def revoke_public_verification_token(
    token_id: str,
    reason: str,
) -> PublicVerificationTokenMetadata:
    """Revoke a public verification token by metadata ID."""
    data = await _post(f"/v1/public-verification-tokens/{token_id}/revoke", {"reason": reason})
    return PublicVerificationTokenMetadata.model_validate(data)


async def verify_public_token(token: str) -> PublicVerificationResponse:
    """
    Verify an opaque public token through the minimized public v2 contract.

    Failure exceptions are sanitized so the opaque token is not included in
    exception text.
    """
    try:
        data = await _get_unauthenticated_json(f"/public/v2/verify/{token}")
    except httpx.HTTPStatusError as exc:
        reason_code = None
        try:
            payload = exc.response.json()
            reason_code = payload.get("reason_code") or payload.get("detail")
        except Exception:
            reason_code = None
        raise ArclaspVerificationError(
            "public token verification failed",
            status_code=exc.response.status_code,
            reason_code=reason_code,
        ) from None
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ArclaspVerificationError("public token verification transport failed") from exc
    return PublicVerificationResponse.model_validate(data)

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
) -> NoReturn:
    """
    Apply fail-closed handling to a transport-level backend failure.

    Governed execution requires backend authority and never converts backend
    unavailability into an allow or an offline chain.
    """
    logger.warning(
        "%s - governed execution requires backend authority (action_type=%s)",
        message,
        action_type or "unspecified",
    )
    raise BackendUnavailableError(message=message)
