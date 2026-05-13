"""
proofrail.client — SDK client initialization, configuration singleton, and
low-level HTTP transport to the ProofRail backend.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
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
_http_client: httpx.AsyncClient | None = None


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
    global _config, _http_client

    if not kwargs.get("api_key"):
        raise ValueError(
            "api_key is required. Call proofrail.init(api_key='prail_...') before using the SDK."
        )

    _config = ChainConfig(**kwargs)

    # Close the old HTTP client before replacing it so we don't leak open
    # TCP connections and file descriptors on re-init.
    _old_client = _http_client
    if _old_client is not None:
        try:
            loop = asyncio.get_running_loop()
            # Inside an async context — schedule close as a fire-and-forget task.
            loop.create_task(_old_client.aclose())
        except RuntimeError:
            # No running event loop — close synchronously via a temporary loop.
            asyncio.run(_old_client.aclose())

    # Rebuild the HTTP client so that base_url, timeout, and auth header all
    # stay in sync with the new config.
    _http_client = httpx.AsyncClient(
        base_url=_config.backend_url,
        timeout=httpx.Timeout(_config.backend_timeout_seconds),
        headers={
            "Authorization": f"Bearer {_config.api_key}",
            "Content-Type": "application/json",
        },
    )

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
    """Return the module-level HTTP client, raising if init() was skipped."""
    if _http_client is None:
        raise RuntimeError(
            "proofrail has not been initialized. Call proofrail.init(api_key='prail_...') first."
        )
    return _http_client


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _post(path: str, data: dict, action_type: str | None = None) -> dict:
    """
    POST *data* as JSON to *path* on the configured backend.

    On network failure, the resolved fail_mode determines behaviour:
    ``"deny"`` raises BackendUnavailableError; ``"allow"`` raises
    _OfflineSignal so chain.py can transition to offline mode cleanly.
    4xx/5xx responses bypass fail_mode and always re-raise.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (POST {path})"
        _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to ProofRail backend at {config.backend_url} (POST {path})"
        _handle_backend_failure(msg, config, action_type)

    except httpx.HTTPStatusError:
        # 4xx / 5xx — re-raise; these are application errors, not transport
        # failures, so fail_mode does not apply.
        raise


async def _get(path: str, action_type: str | None = None) -> dict:
    """GET *path* on the configured backend. Same fail_mode semantics as _post."""
    config = get_config()
    client = _get_client()

    try:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (GET {path})"
        _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to ProofRail backend at {config.backend_url} (GET {path})"
        _handle_backend_failure(msg, config, action_type)

    except httpx.HTTPStatusError:
        raise


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
