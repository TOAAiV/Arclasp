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

## 2. `fail_mode` security model

When the ProofRail backend is unreachable (network failure, timeout,
5xx error), the SDK must make a local decision for each governed action.
This decision is controlled by `fail_mode`.

| Setting | Behavior on backend failure | When to use |
|---|---|---|
| `"deny"` **(default)** | Action is blocked; `ActionDeniedError` is raised | Any action with irreversible external effects (financial, data deletion, external API calls) |
| `"allow"` | Action proceeds; event is buffered for async sync | High-availability workflows where governance-induced downtime is unacceptable |

**Configuration:**

```python
# Global default — applied when no per-action-class override matches
proofrail.init(fail_mode="deny")

# Per-action-class overrides via fail_modes dict
proofrail.init(
    fail_mode="deny",
    fail_modes={
        "llm_inference": "allow",   # allow LLM calls when backend is down
        "tool_call":     "deny",    # always block tool calls on backend failure
    },
)
```

`fail_mode` affects only **backend-path** events. Fast-path events are
evaluated locally and are never subject to `fail_mode` — see Section 3.

**Recommendation:** Use `fail_mode="deny"` as the global default and selectively
set `"allow"` for action types that are truly safe to proceed without governance.
A global `fail_mode="allow"` means a network partition silently disables all
governance. *(Audit finding: SDK-S-14)*

---

## 3. Fast-path security model

The local fast-path evaluates low-risk actions without a backend round-trip.
All five eligibility criteria must pass; the first failure routes to the
backend synchronously.

**Eligibility criteria:**
1. `enable_local_fast_path=True` (set explicitly — not production default).
2. `environment != "production"` — production always uses the backend.
3. Risk score < 40 AND no blocking categories (destructive, financial,
   credential_exposure, exfiltration, privilege_escalation, financial_high).
4. Cumulative `financial_exposure_usd` < 80 % of `cumulative_financial_threshold_usd`.
5. Agent not in `high_risk_agents`.

Fast-path decisions are queued for async backend logging (drain task) so the
dashboard remains accurate. Criterion 4 is evaluated against a **local
in-process metrics snapshot** that is updated after every fast-path allow —
the snapshot is accurate within the current process lifetime but does not
reflect decisions made in parallel processes or previous runs.

**Kill-switch limitation (SDK-S-3 / SDK-S-15):** Fast-path currently hardcodes
`kill_switch_active=False` (`sdk/proofrail/fast_path.py:185`). A
backend-activated organisation kill switch is therefore not honoured by
fast-path decisions — the action is evaluated locally and proceeds if the
other four criteria pass. Backend-path decisions (when fast-path is ineligible
or `enable_local_fast_path=False`) honour the kill switch correctly.

**Mitigation:** For workflows where kill-switch enforcement is critical
(compliance-sensitive agents, financial automation), set
`enable_local_fast_path=False`. All actions are then routed to the backend
synchronously and the kill switch is always checked.

**Post-launch:** Background kill-switch polling (BACKLOG B-10) will close
this gap by caching the organisation's kill-switch state with a short TTL.

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

## 6. Offline buffer limitations

When the backend is unreachable, governed events are held in an **in-memory
list** on the `Chain` instance (`_offline_buffer`). The drain task replays
buffered events to the backend when connectivity is restored, or at chain
completion.

**Operators must understand the following constraints:** *(Audit finding: SDK-S-13)*

- **Lost on process crash.** The buffer is not persisted to disk. If the
  process crashes or is killed while the buffer holds events, those events are
  permanently lost. There is no write-ahead log or durable queue.
- **Bounded by `offline_buffer_max_events`** (default: 100 events). When the
  buffer is full, the **oldest** event is evicted to make room for the newest —
  keeping the audit trail as current as possible under pressure. Each eviction
  logs a WARNING.
- **Permanent backend errors discard events.** During drain, HTTP `4xx`
  responses in `{400, 401, 403, 404, 422}` are classified as permanent: the
  event is logged at WARNING and discarded. Transient errors (`429`, `5xx`)
  retry with exponential backoff.
- **`fail_mode` does not apply to drain failures.** `fail_mode` governs
  whether an action *proceeds* when the backend is down at record time. Drain
  failures are handled by the eviction and retry policies described above.

For compliance workflows that cannot tolerate any event loss, set
`fail_mode="deny"` globally (no per-action `"allow"` overrides). The SDK will
then raise `ActionDeniedError` or `BackendUnavailableError` at record time when
the backend is unreachable, so calling code can react immediately rather than
continuing with degraded governance. Ensure your monitoring captures both
exception types.

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
