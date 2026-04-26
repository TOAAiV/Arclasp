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

async def _post(path: str, data: dict, action_type: str | None = None) -> dict:
    """
    POST *data* as JSON to *path* on the configured backend.

    On network failure the effective fail_mode determines behaviour:

    * ``"deny"``  — raises :exc:`BackendUnavailableError`.
    * ``"allow"`` — logs a warning and returns a synthetic allow decision so
      the chain can continue without the backend.

    Parameters
    ----------
    path : str
        API path relative to ``backend_url``.
    data : dict
        Request body (serialised as JSON).
    action_type : str | None
        The ``action_type`` of the event being posted (e.g. ``"tool_call"``).
        When provided, ``ChainConfig.resolve_fail_mode(action_type)`` is used
        instead of the global ``fail_mode`` so per-class overrides apply.

    Returns
    -------
    dict
        Parsed JSON response body.

    Raises
    ------
    BackendUnavailableError
        When the backend is unreachable and the resolved fail_mode is "deny".
    httpx.HTTPStatusError
        On 4xx / 5xx responses that are not connectivity failures.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (POST {path})"
        return _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to aag backend at {config.backend_url} (POST {path})"
        return _handle_backend_failure(msg, config, action_type)

    except httpx.HTTPStatusError:
        # 4xx / 5xx — re-raise; these are application errors, not transport
        # failures, so fail_mode does not apply.
        raise


async def _get(path: str, action_type: str | None = None) -> dict:
    """
    GET *path* on the configured backend.

    Applies the same fail_mode semantics as :func:`_post`.

    Parameters
    ----------
    path : str
        API path relative to ``backend_url``.
    action_type : str | None
        Passed to ``resolve_fail_mode`` if the request fails.
    """
    config = get_config()
    client = _get_client()

    try:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    except httpx.TimeoutException:
        msg = f"Backend request timed out after {config.backend_timeout_seconds}s (GET {path})"
        return _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to aag backend at {config.backend_url} (GET {path})"
        return _handle_backend_failure(msg, config, action_type)

    except httpx.HTTPStatusError:
        raise


def _handle_backend_failure(
    message: str,
    config: ChainConfig,
    action_type: str | None = None,
) -> dict:
    """
    Apply the resolved fail_mode to a transport-level backend failure.

    ``action_type`` is forwarded to :meth:`ChainConfig.resolve_fail_mode` so
    that per-action-class overrides in ``fail_modes`` are honoured.

    * ``"deny"``  → raise :exc:`BackendUnavailableError`
    * ``"allow"`` → log a warning and return a synthetic allow payload
    """
    effective = config.resolve_fail_mode(action_type)
    if effective == "allow":
        logger.warning(
            "%s — fail_mode=allow (action_type=%s), continuing without backend",
            message,
            action_type or "unspecified",
        )
        return {
            "policy_decision": "allow",
            "decision_reason": "Backend unavailable — fail open (fail_mode=allow)",
            "decision_source": "offline_stub",
        }

    raise BackendUnavailableError(message=message, fail_mode=effective)
