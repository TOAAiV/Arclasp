from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import arclasp
from arclasp import client as _client
from arclasp.exceptions import ArclaspVerificationError
from arclasp.models import (
    AuthenticatedVerificationResponse,
    PublicVerificationResponse,
    PublicVerificationTokenCreateResponse,
    PublicVerificationTokenListResponse,
    PublicVerificationTokenMetadata,
    ReceiptVerifyResponse,
)

_NOW = datetime.now(timezone.utc).isoformat()
_APPROVAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_RECEIPT_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
_TOKEN_ID = "cccccccc-dddd-eeee-ffff-000000000000"
_PUBLIC_TOKEN = "arv_abcdefghijklmnopqrstuvwxyz123456789ABCDEFG"


def _verification_payload(artifact_type: str, artifact_id: str) -> dict:
    return {
        "verification_version": "2",
        "artifact": {
            "type": artifact_type,
            "version": "2",
            "id": artifact_id,
            "created_at": _NOW,
            "chain": {"id": str(uuid.uuid4()), "external_id": "workflow-1", "name": None},
        },
        "verification": {
            "overall_status": "verified",
            "integrity": {"status": "valid", "reason_code": None},
            "asymmetric_signature": {
                "status": "valid",
                "key_status": "not_evaluated",
                "reason_code": None,
            },
            "timestamp_anchors": {
                "freetsa": {"status": "missing", "reason_code": "not_submitted"},
                "opentimestamps": {"status": "indeterminate", "reason_code": "pending"},
            },
            "evidence_chain": {"status": "not_applicable", "reason_code": None},
            "verified_at": _NOW,
        },
        "context": {
            "approval_decision": {"outcome": "approved", "decided_at": _NOW, "reason_code": None},
            "action": {"agent_name": "agent", "action_type": "tool_call", "action_name": "send"},
            "receipt_integrity_semantics": "server-attested integrity verification",
        },
        "capabilities": {
            "can_view_identity_context": False,
            "can_download_admin_evidence": False,
            "admin_downloads": {},
        },
        "admin_context": None,
    }


def _layers() -> dict:
    valid = {"status": "valid", "reason_code": None}
    na = {"status": "not_applicable", "reason_code": None}
    return {
        "compatibility_integrity": valid,
        "compatibility_chain": valid,
        "compatibility_binding": na,
        "v2_payload_shape": valid,
        "v2_artifact_hash": valid,
        "asymmetric_signature": {"status": "valid", "key_status": "trusted", "reason_code": None},
        "v2_evidence_chain": valid,
        "issuance_provenance": valid,
        "cutover_manifest": na,
        "recovery_authorization": na,
        "receipt_ordering": valid,
        "historical_state": {"status": "none", "reason_code": None},
    }


def _evidence_status(
    *,
    status_proof: str,
    reason_code: str,
    reliance_status: str = "active",
    dispute_status: str = "none",
    correction_status: str = "none",
) -> dict:
    return {
        "status_proof": status_proof,
        "reason_code": reason_code,
        "projection_status": "current",
        "projection_reason_code": None,
        "history_count": 1 if reason_code != "no_status_history" else 0,
        "current_status": None
        if reason_code == "no_status_history"
        else {
            "reliance_status": reliance_status,
            "dispute_status": dispute_status,
            "correction_status": correction_status,
            "correction_count": 1 if correction_status == "corrected" else 0,
        },
        "current_facts": {
            "revocation": {
                "action": "revoke",
                "reason_code": "superseded",
                "public_summary": "Revoked by policy",
                "issued_at": _NOW,
                "status_sequence": 1,
                "artifact_hash_sha256": "a" * 64,
                "signing_key_id": "evidence-key-1",
            }
            if reliance_status == "revoked"
            else None,
            "open_dispute": None,
            "latest_dispute_resolution": None,
            "latest_correction": None,
        },
    }


_TOKEN_METADATA = {
    "id": _TOKEN_ID,
    "organization_id": "dddddddd-eeee-ffff-0000-111111111111",
    "artifact_type": "approval_certificate",
    "artifact_id": _APPROVAL_ID,
    "status": "active",
    "created_at": _NOW,
    "created_by_user_id": None,
    "created_by_membership_id": None,
    "expires_at": None,
    "revoked_at": None,
    "revoked_by_user_id": None,
    "revoked_by_membership_id": None,
    "revocation_reason": None,
}


@pytest.fixture(autouse=True)
def init_sdk():
    arclasp.init(
        api_key="prail_testkey123",
        backend_url="https://api.example.test",
        environment="development",
        fail_mode="deny",
    )
    yield
    _client._config = None
    _client._clients_by_loop.clear()


def _mock_get(payload: dict):
    return patch.object(_client, "_get", new=AsyncMock(return_value=payload))


def _assert_no_raw_v2_fields(model: AuthenticatedVerificationResponse) -> None:
    dumped = model.model_dump()
    forbidden = {
        "context_snapshot",
        "hmac_signature",
        "ed25519_signature",
        "ed25519_public_key_id",
        "public_key",
        "token_hash",
        "token_hash_sha256",
        "proof_blob",
        "certificate",
        "structured_data",
    }
    assert not forbidden.intersection(str(dumped))


@pytest.mark.asyncio
async def test_verify_approval_v2_url_and_typed_response():
    payload = _verification_payload("approval_certificate", _APPROVAL_ID)
    with _mock_get(payload) as mock:
        result = await _client.verify_approval_v2(_APPROVAL_ID)
    mock.assert_called_once_with(f"/v1/verification/v2/approvals/{_APPROVAL_ID}")
    assert isinstance(result, AuthenticatedVerificationResponse)
    assert result.verification.asymmetric_signature.key_status == "not_evaluated"
    _assert_no_raw_v2_fields(result)


@pytest.mark.asyncio
async def test_verify_receipt_v2_url_and_typed_response():
    payload = _verification_payload("chain_record", _RECEIPT_ID)
    with _mock_get(payload) as mock:
        result = await _client.verify_receipt_v2(_RECEIPT_ID)
    mock.assert_called_once_with(f"/v1/verification/v2/receipts/{_RECEIPT_ID}")
    assert isinstance(result, AuthenticatedVerificationResponse)
    assert result.context.receipt_integrity_semantics == "server-attested integrity verification"
    _assert_no_raw_v2_fields(result)


def test_authenticated_verification_preserves_governance_and_layers():
    payload = _verification_payload("chain_record", _RECEIPT_ID)
    payload["artifact"]["issuance_mode"] = "historical_recovery"
    payload["verification"]["reason_code"] = None
    payload["verification"]["layers"] = _layers()
    payload["verification"]["legacy_hmac_diagnostic"] = {
        "status": "match",
        "authoritative": False,
        "reason_code": None,
    }
    payload["evidence_status"] = _evidence_status(
        status_proof="valid",
        reason_code="revoked",
        reliance_status="revoked",
    )
    payload["future_non_critical_field"] = {"still": "ignored"}

    result = AuthenticatedVerificationResponse.model_validate(payload)

    assert result.artifact.issuance_mode == "historical_recovery"
    assert result.verification.layers is not None
    assert result.verification.layers.asymmetric_signature.key_status == "trusted"
    assert result.verification.legacy_hmac_diagnostic is not None
    assert result.verification.legacy_hmac_diagnostic.authoritative is False
    assert result.evidence_status is not None
    assert result.evidence_status.status_proof == "valid"
    assert result.evidence_status.current_status is not None
    assert result.evidence_status.current_status.reliance_status == "revoked"
    assert result.verification.overall_status == "verified"


@pytest.mark.parametrize(
    ("reason_code", "status_proof", "current_status"),
    [
        ("no_status_history", "not_applicable", None),
        ("open_dispute", "valid", {"reliance_status": "active", "dispute_status": "open", "correction_status": "none", "correction_count": 0}),
        ("corrected", "valid", {"reliance_status": "active", "dispute_status": "none", "correction_status": "corrected", "correction_count": 1}),
        ("signing_key_untrusted", "indeterminate", None),
    ],
)
def test_authenticated_evidence_status_governance_states(reason_code, status_proof, current_status):
    payload = _verification_payload("approval_certificate", _APPROVAL_ID)
    payload["evidence_status"] = {
        "status_proof": status_proof,
        "reason_code": reason_code,
        "projection_status": "current",
        "projection_reason_code": None,
        "history_count": 0 if current_status is None else 1,
        "current_status": current_status,
        "current_facts": {
            "revocation": None,
            "open_dispute": None,
            "latest_dispute_resolution": None,
            "latest_correction": None,
        },
    }

    result = AuthenticatedVerificationResponse.model_validate(payload)

    assert result.verification.overall_status == "verified"
    assert result.evidence_status is not None
    assert result.evidence_status.reason_code == reason_code
    if current_status is None:
        assert result.evidence_status.current_status is None
    else:
        assert result.evidence_status.current_status.dispute_status == current_status["dispute_status"]


@pytest.mark.asyncio
async def test_token_list_issue_revoke_urls_and_types():
    with _mock_get({"organization_id": _TOKEN_METADATA["organization_id"], "tokens": [_TOKEN_METADATA], "total": 1, "limit": 10, "offset": 5}) as mock_get:
        listed = await _client.list_public_verification_tokens(limit=10, offset=5)
    mock_get.assert_called_once_with("/v1/public-verification-tokens?limit=10&offset=5")
    assert isinstance(listed, PublicVerificationTokenListResponse)
    assert "token" not in listed.model_dump()["tokens"][0]
    assert "token_hash_sha256" not in str(listed.model_dump())

    create_payload = dict(_TOKEN_METADATA, token=_PUBLIC_TOKEN, public_path=f"/public/v2/verify/{_PUBLIC_TOKEN}")
    with patch.object(_client, "_post_once", new=AsyncMock(return_value=create_payload)) as mock_post_once:
        issued = await _client.issue_public_verification_token("approval_certificate", _APPROVAL_ID)
    mock_post_once.assert_called_once_with(
        "/v1/public-verification-tokens",
        {"artifact_type": "approval_certificate", "artifact_id": _APPROVAL_ID},
    )
    assert isinstance(issued, PublicVerificationTokenCreateResponse)
    assert issued.token == _PUBLIC_TOKEN
    assert "token_hash" not in str(issued.model_dump())

    revoked_payload = dict(_TOKEN_METADATA, status="revoked", revoked_at=_NOW, revocation_reason="rotation")
    with patch.object(_client, "_post", new=AsyncMock(return_value=revoked_payload)) as mock_post:
        revoked = await _client.revoke_public_verification_token(_TOKEN_ID, "rotation")
    mock_post.assert_called_once_with(f"/v1/public-verification-tokens/{_TOKEN_ID}/revoke", {"reason": "rotation"})
    assert isinstance(revoked, PublicVerificationTokenMetadata)
    assert revoked.status == "revoked"


@pytest.mark.asyncio
async def test_token_issue_does_not_use_retry_helper():
    client = MagicMock()
    response = MagicMock(status_code=201)
    response.raise_for_status.return_value = None
    response.json.return_value = dict(_TOKEN_METADATA, token=_PUBLIC_TOKEN, public_path=f"/public/v2/verify/{_PUBLIC_TOKEN}")
    client.post = AsyncMock(return_value=response)

    with patch.object(_client, "_get_client", return_value=client), patch.object(_client, "_retry_with_backoff", new=AsyncMock()) as retry:
        await _client.issue_public_verification_token("approval_certificate", _APPROVAL_ID)

    client.post.assert_awaited_once()
    retry.assert_not_called()


@pytest.mark.asyncio
async def test_public_token_verify_uses_public_v2_and_sanitizes_error_text():
    success_payload = {
        "verification_version": "2",
        "artifact_type": "chain_record",
        "artifact_version": "2",
        "integrity": {"status": "valid", "reason_code": None},
        "overall_status": "verified",
        "verified_at": _NOW,
    }
    with patch.object(_client, "_get_unauthenticated_json", new=AsyncMock(return_value=success_payload)) as mock_get:
        result = await _client.verify_public_token(_PUBLIC_TOKEN)
    mock_get.assert_called_once_with(f"/public/v2/verify/{_PUBLIC_TOKEN}")
    assert isinstance(result, PublicVerificationResponse)

    request = httpx.Request("GET", f"https://api.example.test/public/v2/verify/{_PUBLIC_TOKEN}")
    response = httpx.Response(404, request=request, json={"reason_code": "not_publicly_verifiable"})
    error = httpx.HTTPStatusError("not found", request=request, response=response)
    with patch.object(_client, "_get_unauthenticated_json", new=AsyncMock(side_effect=error)):
        with pytest.raises(ArclaspVerificationError) as exc_info:
            await _client.verify_public_token(_PUBLIC_TOKEN)
    assert _PUBLIC_TOKEN not in str(exc_info.value)
    assert exc_info.value.reason_code == "not_publicly_verifiable"


def test_public_verification_preserves_public_governance_fields():
    payload = {
        "verification_version": "2",
        "artifact_type": "chain_record",
        "artifact_version": "2",
        "issuance_mode": "forward",
        "overall_status": "verified",
        "integrity": {"status": "valid", "reason_code": None},
        "asymmetric_signature": {"status": "valid", "key_status": "trusted", "reason_code": None},
        "layers": {
            "compatibility_integrity": {"status": "valid", "reason_code": None},
            "asymmetric_signature": {"status": "valid", "key_status": "trusted", "reason_code": None},
        },
        "legacy_hmac_diagnostic": {
            "status": "missing",
            "authoritative": False,
            "reason_code": "legacy_hmac_missing",
        },
        "evidence_status": {
            "status_proof": "valid",
            "reason_code": "open_dispute",
            "history_count": 1,
            "current_status": {
                "reliance_status": "active",
                "dispute_status": "open",
                "correction_status": "none",
                "correction_count": 0,
            },
            "current_facts": {
                "revocation": None,
                "open_dispute": None,
                "latest_dispute_resolution": None,
                "latest_correction": None,
            },
        },
        "verified_at": _NOW,
        "unknown_future_field": True,
    }

    result = PublicVerificationResponse.model_validate(payload)

    assert result.issuance_mode == "forward"
    assert result.layers is not None
    assert result.layers["asymmetric_signature"].key_status == "trusted"
    assert result.legacy_hmac_diagnostic is not None
    assert result.legacy_hmac_diagnostic.authoritative is False
    assert result.evidence_status is not None
    assert result.evidence_status.current_status is not None
    assert result.evidence_status.current_status.dispute_status == "open"


@pytest.mark.asyncio
async def test_legacy_verify_receipt_warns_and_preserves_call_semantics():
    payload = {
        "valid": True,
        "receipt_number": "PR-2026-00001",
        "chain_id": "00000000-0000-0000-0000-000000000001",
        "generated_at": _NOW,
    }
    with _mock_get(payload) as mock_get, pytest.warns(DeprecationWarning) as warnings:
        result = await _client.verify_receipt(_RECEIPT_ID)
    mock_get.assert_called_once_with(f"/v1/receipts/{_RECEIPT_ID}/verify")
    assert isinstance(result, ReceiptVerifyResponse)
    assert result.valid is True
    message = str(warnings[0].message)
    assert "deprecated legacy receipt verifier" in message
    assert "verify_receipt_v2" in message
    assert _RECEIPT_ID not in message
