"""First-release top-level import contract."""

import importlib.metadata

import arclasp


def test_canonical_top_level_all():
    assert arclasp.__all__ == [
        "init",
        "verify_approval_v2",
        "verify_public_token",
        "verify_receipt_v2",
        "Chain",
        "ArclaspPolicyError",
        "ActionDeniedError",
        "BackendUnavailableError",
        "ChainCompletionError",
        "ChainAutoPausedError",
        "ChainTimeoutError",
        "ArclaspKillSwitchError",
        "ArclaspVerificationError",
        "__version__",
    ]


def test_canonical_top_level_imports():
    from arclasp import (  # noqa: F401
        ActionDeniedError,
        ArclaspKillSwitchError,
        ArclaspPolicyError,
        ArclaspVerificationError,
        BackendUnavailableError,
        Chain,
        ChainAutoPausedError,
        ChainCompletionError,
        ChainTimeoutError,
        init,
        verify_approval_v2,
        verify_public_token,
        verify_receipt_v2,
    )


def test_removed_top_level_names_are_absent():
    for name in (
        "PolicyViolationError",
        "verify_receipt",
        "issue_public_verification_token",
        "list_public_verification_tokens",
        "revoke_public_verification_token",
    ):
        assert not hasattr(arclasp, name)
        assert name not in arclasp.__all__


def test_version_contract():
    assert arclasp.__version__ == "0.1.0b2"
    assert importlib.metadata.version("arclasp") == "0.1.0b2"
