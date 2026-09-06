# Changelog

All notable changes to Arclasp (formerly ProofRail) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0b2] - 2026-09-05

Documentation and release correction for the public beta package metadata.

### Changed

- Corrected the README Quick Start so the public PyPI long description shows a runnable async script using `async def main()` and `asyncio.run(main())`.
- Corrected public SDK and configuration wording to match the current backend-authoritative SDK surface.
- No intended SDK runtime behavior changes.

## [0.1.0b1] - 2026-09-05

Public beta release candidate for the first public Arclasp SDK publication.

### Changed

- **Breaking:** the Python import namespace is now `arclasp` (previously `proofrail`). `import arclasp`, `from arclasp import Chain`, `from arclasp.exceptions import ...` replace the old `proofrail` imports. The PyPI distribution name (`arclasp`) is unchanged from the prior prerelease. Exception classes `ProofRailPolicyError`, `ProofRailVerificationError`, and `ProofRailKillSwitchError` are renamed to `ArclaspPolicyError`, `ArclaspVerificationError`, and `ArclaspKillSwitchError`. There is no `proofrail` compatibility shim — the project has no external customers on the prior namespace yet.
- Governed SDK execution now requires backend authority for every public Chain action. The deprecated `enable_local_fast_path=True` setting no longer produces local allow decisions, and backend unavailability raises `BackendUnavailableError` even when legacy `fail_mode="allow"` compatibility settings are supplied.
- Finalized the first public Arclasp Python namespace before PyPI publication: removed dead local-governance compatibility APIs, exposed `ArclaspPolicyError` at top level, kept legacy receipt verification under `arclasp.client`, and kept backend/wire verification contracts unchanged.
- Updated package metadata, security contacts, and public documentation links for the Arclasp public beta.

## [0.1.0a10] — 2026-08-19

Pre-release repository hygiene pass ahead of first public GitHub/PyPI release.

### Changed

- Repository renamed to `TOAAiV/Arclasp`; all project URLs, issue templates,
  and in-repo documentation links now point at the current repository instead
  of the prior `proofrail` name.
- Fixed then-current ProofRail documentation-domain inconsistency in exception
  remediation links before the later Arclasp domain migration.
- Removed internal engineering/audit working documents and a private-backend
  end-to-end test from the public repository; they do not affect the public
  SDK's behavior or test coverage.
- Removed a Windows-only development workaround from the production demo
  scripts that disabled TLS certificate verification; production examples now
  use normal certificate-verified HTTPS only.
- Demo scripts no longer hardcode a personal notification address; they read
  the approver email from an `ARCLASP_APPROVER_EMAIL` environment variable.
- Package metadata classifier corrected to `Alpha` to match the `a10`
  pre-release version string.

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
- Cost tracking and monthly budgets: recorded LLM token usage and estimated
  dollar cost, with dashboard-configured UTC calendar month budgets. A governed
  chain requires approval only after a newly recorded total is greater than the
  configured budget; unknown model pricing is not counted as zero.
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

[0.1.0]: https://github.com/TOAAiV/Arclasp/releases/tag/v0.1.0
