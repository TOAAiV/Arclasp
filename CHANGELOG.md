# Changelog

All notable changes to ProofRail will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-06-17

Initial public release.

### Added

**Core governance**
- Chain-level governance with cumulative metrics across an entire agent workflow
  (financial exposure, external communications, records modified, privileged actions,
  tokens used, external domains contacted).
- Open-source reference policy engine in `proofrail/policies.py`, verified for parity
  against the backend policy engine on every test run.
- Blocking human approval gate with configurable timeouts and fallback approvers.
  Returns a fully-resolved `PolicyDecision` to the caller after the approver responds.
- Local fast-path evaluation for sub-5ms decisions on obviously-safe actions,
  with asynchronous backend logging so the dashboard and audit trail stay accurate.
- Policy shadow mode for testing new policies against real traffic in observe-only
  mode before flipping them to enforce.
- Per-action-class fail modes for backend-unreachable scenarios (e.g., deny financial
  actions, allow reads).
- Time-boxed policy exceptions with explicit scope and expiration.
- Org-wide kill switch raising `ProofRailKillSwitchError` distinct from regular
  policy denials.

**Audit and trust**
- HMAC-SHA256 signed audit receipts, hash-chained across an organization so
  tampering with any single receipt breaks the chain publicly.
- Public receipt verification endpoint requiring no authentication.
- Append-only admin audit log capturing before/after diffs for every dashboard
  mutation.
- Agent registry with risk tiers; unregistered agents are flagged but not blocked.

**Framework adapters**
- LangGraph adapter via `astream_events` (preferred) with `AsyncCallbackHandler`
  fallback.
- LangChain adapter via `AsyncCallbackHandler` for tool calls and LLM invocations.
- CrewAI adapter with both native callback (Strategy A) and `execute_task`
  monkey-patch (Strategy B) instrumentation paths.
- MCP adapter for instrumenting MCP server tool handlers.

**Reliability and safety**
- Payload sanitization with default patterns for API keys, passwords, credit cards,
  SSNs, private keys, and common token formats (OpenAI, Stripe, GitHub,
  Hugging Face, AWS, JWT). Raw payloads are never persisted.
- Offline buffer with idempotency keys to prevent duplicate audit events on
  network retry.
- Cost tracking and budget alerts: per-chain, per-agent, per-model token usage
  and dollar cost, with email warnings at 80% of monthly budget.
- Cross-organization isolation enforced on every UUID-bearing endpoint, with a
  dedicated test suite verifying one organization's API key cannot reach
  another organization's data.

### Security

This release ships after a 15-finding security audit of the SDK. All findings
have been resolved:

- Metadata sanitization applied at chain start to prevent unsanitized payloads
  from reaching the backend.
- API keys stored as `pydantic.SecretStr` to prevent accidental logging.
- HTTP-to-production warnings switched to logger output and extended to all
  non-localhost URLs.
- Additional sensitive-value patterns added (JWT prefixes, AWS access keys,
  ProofRail API key prefix).
- Bytes-type payload handling added to the sanitizer.
- Action name truncation hardened against oversized inputs.
- Log sanitization helper applied across all framework adapter log sites.
- Fast-path cumulative metrics propagation fixed (the financial threshold gate
  was previously not updating local metrics from fast-path decisions).
- Documented kill-switch limitation in fast-path; full enforcement is on the
  post-launch backlog.
- Unused `cryptography` dependency removed.

Full security policy and disclosure process in [SECURITY.md](SECURITY.md).

### Fixed

- **Audit event duplication on retry** — events sent during a network blip
  could be recorded twice. Fixed via idempotency keys on every event POST
  and deduplication on the backend.
- **Infinite retry on permanent errors** — the offline buffer drain treated
  all HTTP errors as transient, looping forever on 4xx responses. Now
  classifies 400, 401, 403, 404, 422 as permanent (discard and continue) and
  429, 5xx as transient.
- **Silent event loss on slow backends** — fixed-duration drain timeout could
  abandon events when backend latency was high. Drain timeout now scales with
  buffered event count.
- **Network errors propagated as bare exceptions** — `httpx.ReadError` and
  `httpx.WriteError` (raised by load balancer terminations, backend crashes,
  cellular handoffs) were not retried. Now caught alongside `ConnectError`.

### Known limitations

- Single region. Backend runs in US-East. European and APAC users may see
  100-150ms additional latency. Multi-region is on the roadmap.
- CrewAI deny decisions cannot halt mid-task execution due to CrewAI's
  synchronous task architecture. Governance events are still recorded; the
  decision surfaces as `ActionDeniedError` on the next event loop cycle.
- Approval notifications are email-only. Slack and Teams integrations are
  planned but not shipped.
- No SSO beyond what Clerk provides out of the box.

[0.1.0]: https://github.com/TOAAiV/proofrail/releases/tag/v0.1.0
