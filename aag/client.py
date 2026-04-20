"""
aag.client — SDK client initialization, configuration singleton, and
low-level HTTP transport to the aag backend.
"""

from __future__ import annotations

import logging

import httpx

from aag.exceptions import BackendUnavailableError
from aag.models import ChainConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_config: ChainConfig | None = None
_http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init(**kwargs) -> ChainConfig:
    """
    Initialize the aag SDK with the given configuration values.

    Must be called once before creating any Chain.  Calling it again with
    different values reconfigures the SDK and resets the HTTP client.

    Parameters
    ----------
    api_key : str  (required)
        The aag API key generated from the dashboard.
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
            "api_key is required. Call aag.init(api_key='aag_...') before using the SDK."
        )

    _config = ChainConfig(**kwargs)

    # Rebuild the HTTP client whenever init() is called so that base_url,
    # timeout, and auth header all stay in sync with the new config.
    if _http_client is not None:
        # Schedule the old client for cleanup; we can't await here (sync
        # function), so we replace it and let GC handle the old one.
        pass

    _http_client = httpx.AsyncClient(
        base_url=_config.backend_url,
        timeout=httpx.Timeout(_config.backend_timeout_seconds),
        headers={
            "Authorization": f"Bearer {_config.api_key}",
            "Content-Type": "application/json",
        },
    )

    logger.debug("aag SDK initialized (environment=%s)", _config.environment)
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
            "aag has not been initialized. Call aag.init(api_key='aag_...') first."
        )
    return _config


def _get_client() -> httpx.AsyncClient:
    """Return the module-level HTTP client, raising if init() was skipped."""
    if _http_client is None:
        raise RuntimeError(
            "aag has not been initialized. Call aag.init(api_key='aag_...') first."
        )
    return _http_client


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _post(path: str, data: dict) -> dict:
    """
    POST *data* as JSON to *path* on the configured backend.

    On network failure the configured ``fail_mode`` determines behaviour:

    * ``"deny"``  — raises :exc:`BackendUnavailableError`.
    * ``"allow"`` — logs a warning and returns a synthetic allow decision so
      the chain can continue without the backend.

    Returns
    -------
    dict
        Parsed JSON response body.

    Raises
    ------
    BackendUnavailableError
        When the backend is unreachable and fail_mode is "deny".
    httpx.HTTPStatusError
        On 4xx / 5xx responses that are not connectivity failures.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException as exc:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (POST {path})"
        return _handle_backend_failure(msg, config)

    except httpx.ConnectError as exc:
        msg = f"Could not connect to aag backend at {config.backend_url} (POST {path})"
        return _handle_backend_failure(msg, config)

    except httpx.HTTPStatusError as exc:
        # 4xx / 5xx — re-raise; these are application errors, not transport
        # failures, so fail_mode does not apply.
        raise


async def _get(path: str) -> dict:
    """
    GET *path* on the configured backend.

    Applies the same ``fail_mode`` semantics as :func:`_post`.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (GET {path})"
        return _handle_backend_failure(msg, config)

    except httpx.ConnectError:
        msg = f"Could not connect to aag backend at {config.backend_url} (GET {path})"
        return _handle_backend_failure(msg, config)

    except httpx.HTTPStatusError:
        raise


def _handle_backend_failure(message: str, config: ChainConfig) -> dict:
    """
    Apply ``fail_mode`` to a transport-level backend failure.

    * ``"deny"``  → raise :exc:`BackendUnavailableError`
    * ``"allow"`` → log a warning and return a synthetic allow payload
    """
    if config.fail_mode == "allow":
        logger.warning("%s — fail_mode=allow, continuing without backend", message)
        return {
            "policy_decision": "allow",
            "decision_reason": "Backend unavailable — fail open (fail_mode=allow)",
            "decision_source": "offline_stub",
        }

    raise BackendUnavailableError(message=message, fail_mode=config.fail_mode)
