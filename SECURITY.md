# Arclasp SDK — Security Model

This document describes the security guarantees the Arclasp SDK provides,
the intentional design trade-offs that operators must understand before
deploying it, and how to report vulnerabilities.

---

## 0. Supported versions

Arclasp is currently in **public beta**. Only the latest published
version on PyPI receives security fixes; there is no backport policy for
older pre-release versions. Once a stable `1.0` ships, this section will be
updated with a supported-version table.

---

## 1. Threat model

The Arclasp SDK acts as a governance intercept layer between AI agent
frameworks (LangChain, LangGraph, CrewAI, MCP) and the Arclasp backend.
Its security goal is to ensure that agent actions are **evaluated against
policy before (or immediately after) they execute**, and that a tamper-evident
audit trail of every governed action is transmitted to the backend.

**In scope:**
- Preventing sensitive values in action payloads from reaching the backend
  in plaintext (sanitization layer).
- Ensuring the API key is never exposed via logs, repr, or debug output.
- Failing closed when the backend is unreachable.
- Transmitting all governed events to the backend before returning an allow decision.

**Out of scope (operator responsibility):**
- TLS certificate pinning for the backend connection.
- Encrypting the offline event buffer at rest.
- Securing the process environment where the SDK runs (API key in env vars,
  memory access by co-resident processes).
- Vulnerabilities in third-party agent frameworks (LangChain, LangGraph,
  CrewAI, MCP) themselves; we operate as an intercept layer and inherit their
  security posture for in-framework operations.
- Backend data-at-rest encryption and access control.

---

## 2. Backend-unavailable handling

Governed SDK execution requires an authoritative backend response before an
action is allowed to proceed. When the backend is unreachable after configured
retries (network failure, timeout, 5xx, or 429 exhaustion), the SDK raises
`BackendUnavailableError` and fails closed. There is no public fail-open
configuration in the first Arclasp package.

A network partition must not silently disable governance or produce audit gaps.

---

## 3. HTTP/HTTPS policy

The SDK's default `backend_url` is `http://localhost:8000`, which is
intentionally HTTP — it targets a local development server where TLS is
not needed.

**Warning behaviour:** At `arclasp.init()` time, the SDK logs a WARNING if
`backend_url` uses plaintext HTTP and the host is **not** localhost
(`127.0.0.1`, `localhost`, `::1`). The warning text names the impact:
> *"All audit data will be transmitted unencrypted."*

Localhost URLs (any port) are exempt from this warning. They are assumed to
be development or test environments where TLS overhead is inappropriate.

**Production requirement:** Any deployment where `backend_url` points to a
remote host must use an `https://` URL. Sending the API key (Bearer token)
and governance payloads over unencrypted HTTP exposes both credentials and
potentially sensitive agent data to network interception. *(Audit finding:
SDK-S-6)*

---

## 4. API key handling

The Arclasp API key (`api_key`) is stored as a Pydantic `SecretStr`. Its
value is masked in all repr and str output — `str(config)` and log lines
that include `config` will display `**********` rather than the raw key.
*(Audit finding: SDK-S-7)*

The key is transmitted **only** as a Bearer token in the `Authorization`
header of outbound requests to `backend_url`. It is never:
- Interpolated into log messages or exception strings.
- Included in event payload bodies sent to the backend.
- Passed to third-party framework adapters (LangChain, LangGraph, CrewAI).

The sanitization layer also redacts string values that begin with `prail_`
(the Arclasp API key prefix) from action payloads, so an agent that echoes
a key back in a tool result will have it redacted before transmission.

---

## 5. Transitional buffer limitations

Public governed execution does not create offline or fast-path buffers. The
`Chain` object still retains internal buffer fields used by focused lifecycle
tests, but backend unavailability at record time raises `BackendUnavailableError`
instead of allowing the action.

If transitional internal buffer state exists, it remains in-memory only, bounded
by `offline_buffer_max_events`, and best-effort drained. It must not be treated
as a compliance-grade durable queue.

---

## 6. Receipt verification model

Arclasp audit receipts are integrity-protected server-side. When
`chain.receipt()` returns a `ChainReceiptResponse`, the `signature` field
contains an HMAC computed by the backend over the receipt's `structured_data`.

**Verification flow:**

```python
receipt = await chain.receipt()
if receipt and receipt.id:
    result = await arclasp.verify_receipt_v2(receipt.id)
    assert result.verification.integrity.status == "valid"
```

`arclasp.verify_receipt_v2(receipt_id)` calls
`GET /v1/verification/v2/receipts/{id}` on the backend. The backend re-derives
HMAC integrity server-side and returns a role-aware verification envelope. This
is server-attested integrity verification, not independent or offline proof.

**Trust model:** The SDK trusts the backend's verification response. There is no
independent client-side hash-chain verification; the SDK does not re-compute the
HMAC locally and the backend does not expose the HMAC secret. The security
guarantee therefore depends on the integrity of the TLS connection to the backend
and the backend's own tamper-resistance. *(Audit finding: SDK-S-12)*

A future explicit admin evidence export may provide raw signatures,
certificates, RFC3161 payloads, OpenTimestamps proofs, or full snapshots for
separate offline analysis. That export is future work and is not part of the
ordinary SDK verification JSON today.

---

## 8. Security disclosure

To report a security vulnerability in Arclasp, please email the published
reporting address: **security@arclasp.com**. Please do not report
vulnerabilities via public GitHub issues.

Please include:
- A description of the vulnerability and the affected component.
- Steps to reproduce, if applicable.
- Your assessment of severity and exploitability.
- Any suggested mitigations.

Please allow reasonable time for investigation and remediation before public
disclosure. We will credit reporters in release notes unless you request
anonymity.

For general support questions (non-security), open an issue on the public
repository or contact support@arclasp.com.
