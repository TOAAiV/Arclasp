# ProofRail SDK — Security Model

This document describes the security guarantees the ProofRail SDK provides,
the intentional design trade-offs that operators must understand before
deploying it, and how to report vulnerabilities.

For a full audit history, see [AUDIT_FINDINGS.md](https://github.com/TOAAiV/ProofRail/blob/main/AUDIT_FINDINGS.md).

---

## 1. Threat model

The ProofRail SDK acts as a governance intercept layer between AI agent
frameworks (LangChain, LangGraph, CrewAI, MCP) and the ProofRail backend.
Its security goal is to ensure that agent actions are **evaluated against
policy before (or immediately after) they execute**, and that a tamper-evident
audit trail of every governed action is transmitted to the backend.

**In scope:**
- Preventing sensitive values in action payloads from reaching the backend
  in plaintext (sanitization layer).
- Ensuring the API key is never exposed via logs, repr, or debug output.
- Applying `fail_mode` correctly when the backend is unreachable.
- Transmitting all governed events to the backend, including fast-path
  decisions (via async drain).

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
`BackendUnavailableError`.

The historical `fail_mode="allow"` and per-action `fail_modes` configuration
values remain accepted for API compatibility, but they are deprecated and do
not permit governed actions to execute offline. If one resolves to `"allow"`,
the error reports that effective value while still failing closed.

**Configuration compatibility:**

```python
# Recommended and default: fail closed on backend failure.
proofrail.init(fail_mode="deny")

# Deprecated compatibility input. This still fails closed when backend authority
# is unavailable, and emits a DeprecationWarning at init time.
proofrail.init(fail_mode="allow")
```

A network partition must not silently disable governance or produce audit gaps.

---

## 3. Local fast-path compatibility

`enable_local_fast_path` is retained as a deprecated configuration field for
older callers, and `proofrail.fast_path` remains importable for compatibility.
Public governed execution no longer uses local fast-path decisions as allow
authority. `Chain.record_agent_action()` records each action through the
backend and waits for the backend decision before returning.

Explicitly passing `enable_local_fast_path=True` emits a `DeprecationWarning`
and does not bypass backend evaluation. Backend-enforced controls such as the
organization kill switch, human approval, monthly budgets, and audit recording
therefore stay authoritative for governed SDK execution.

---

## 4. HTTP/HTTPS policy

The SDK's default `backend_url` is `http://localhost:8000`, which is
intentionally HTTP — it targets a local development server where TLS is
not needed.

**Warning behaviour:** At `proofrail.init()` time, the SDK logs a WARNING if
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

## 5. API key handling

The ProofRail API key (`api_key`) is stored as a Pydantic `SecretStr`. Its
value is masked in all repr and str output — `str(config)` and log lines
that include `config` will display `**********` rather than the raw key.
*(Audit finding: SDK-S-7)*

The key is transmitted **only** as a Bearer token in the `Authorization`
header of outbound requests to `backend_url`. It is never:
- Interpolated into log messages or exception strings.
- Included in event payload bodies sent to the backend.
- Passed to third-party framework adapters (LangChain, LangGraph, CrewAI).

The sanitization layer also redacts string values that begin with `prail_`
(the ProofRail API key prefix) from action payloads, so an agent that echoes
a key back in a tool result will have it redacted before transmission.

---

## 6. Transitional buffer limitations

Public governed execution no longer creates offline or fast-path buffers. The
`Chain` object still retains legacy internal buffer fields and drain helpers for
compatibility with older state and focused tests, but backend unavailability at
record time raises `BackendUnavailableError` instead of allowing the action.

If transitional internal buffer state exists, it remains in-memory only, bounded
by `offline_buffer_max_events`, and best-effort drained. It must not be treated
as a compliance-grade durable queue.

---

## 7. Receipt verification model

ProofRail audit receipts are integrity-protected server-side. When
`chain.receipt()` returns a `ChainReceiptResponse`, the `signature` field
contains an HMAC computed by the backend over the receipt's `structured_data`.

**Verification flow:**

```python
receipt = await chain.receipt()
if receipt and receipt.id:
    result = await proofrail.verify_receipt_v2(receipt.id)
    assert result.verification.integrity.status == "valid"
```

`proofrail.verify_receipt_v2(receipt_id)` calls
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

To report a security vulnerability in ProofRail, please email
**security@proofrail.dev**.

Please include:
- A description of the vulnerability and the affected component.
- Steps to reproduce, if applicable.
- Your assessment of severity and exploitability.
- Any suggested mitigations.

We aim to acknowledge reports within **2 business days** and to provide an
initial assessment within **7 business days**.

We follow a **90-day coordinated disclosure** policy: we ask that you give
us 90 days to investigate and ship a fix before publishing your findings.
We will credit reporters in release notes unless you request anonymity.

For general support questions (non-security), open an issue on the public
repository or contact support@proofrail.dev.
