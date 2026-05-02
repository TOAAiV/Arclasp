"""
proofrail.client — SDK client initialization, configuration singleton, and
low-level HTTP transport to the ProofRail backend.
"""

from __future__ import annotations

import logging

import httpx

from proofrail.exceptions import BackendUnavailableError
from proofrail.models import ChainConfig

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

    # Rebuild the HTTP client whenever init() is called so that base_url,
    # timeout, and auth header all stay in sync with the new config.
    # Can't await the old client's aclose() here (sync function), so we
    # replace it and let GC handle cleanup.
    _http_client = httpx.AsyncClient(
        base_url=_config.backend_url,
        timeout=httpx.Timeout(_config.backend_timeout_seconds),
        headers={
            "Authorization": f"Bearer {_config.api_key}",
            "Content-Type": "application/json",
        },
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
    ``"deny"`` raises BackendUnavailableError; ``"allow"`` logs a warning and
    returns a synthetic allow payload so the chain continues offline.
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
        return _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to ProofRail backend at {config.backend_url} (POST {path})"
        return _handle_backend_failure(msg, config, action_type)

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
        return _handle_backend_failure(msg, config, action_type)

    except httpx.ConnectError:
        msg = f"Could not connect to ProofRail backend at {config.backend_url} (GET {path})"
        return _handle_backend_failure(msg, config, action_type)

    except httpx.HTTPStatusError:
        raise


def _handle_backend_failure(
    message: str,
    config: ChainConfig,
    action_type: str | None = None,
) -> dict:
    """Apply the resolved fail_mode to a transport-level backend failure."""
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
