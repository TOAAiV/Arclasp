# Security And Trust Architecture

This document describes Arclasp's customer-facing security and trust model for
the public SDK and hosted governance service. It is architectural context, not
a vulnerability reporting policy. To report a vulnerability, see
[SECURITY.md](SECURITY.md).

Arclasp is currently in public beta. The public SDK is open source; the hosted
backend is operated as Arclasp infrastructure and remains the authority for
governed workflow decisions.

## 1. Security Model

Arclasp is designed to sit in the path of AI-agent actions that may have real
side effects. The SDK records governed workflow events, sanitizes configured
payload fields, sends them to the backend, and waits for an authoritative
decision before the governed action continues.

The core security goal is runtime governance of consequential workflow state:
policy decisions should account for cumulative chain context rather than only a
single isolated API call.

## 2. Backend As Governance Authority

The backend is the authority for governed allow, deny, approval, kill-switch,
and auto-pause decisions. The public SDK does not expose a local allow fast path
in this release.

The local policy code in the SDK is useful for transparency and local reference
behavior, but public governed execution depends on the hosted backend's
decision.

## 3. Fail-Closed Governed Execution

Governed SDK paths require an authoritative backend response. If the backend is
unreachable after configured retries, transport helpers raise
`BackendUnavailableError`; they do not silently allow governed execution.

This fail-closed behavior applies to governed action paths. Ordinary
application code outside Arclasp, or side effects that were not placed behind
Arclasp SDK calls or supported adapters, remain the application's
responsibility.

## 4. Organization And Tenant Boundaries

Organization-owned operations are scoped by organization. The backend derives
the effective organization from authentication rather than trusting caller-owned
organization identifiers for authority.

The public SDK models organization scoping in its request and response
contracts. Cross-organization enforcement is a hosted-backend responsibility
and is not proven solely by this standalone SDK repository.

## 5. Authentication Model

SDK requests use an Arclasp API key, currently represented by the `prail_`
prefix for compatibility. The SDK stores the API key as a secret value, sends it
as a bearer token to the configured backend, and redacts matching token-like
values from governed payloads through the sanitization layer.

Hosted dashboard and human approval flows use the backend's authenticated
operator model. Administrative and member visibility differs by role.

## 6. Approval Integrity

Governed actions can be paused for human approval before continuation. Approval
decisions are represented as backend-governed state and are exposed through
typed SDK responses and verification paths where applicable.

Approval/evidence side effects are protected by transactional integrity checks
in the backend design so a terminal approval decision is not treated as complete
without its required evidence shape.

## 7. Receipts And Evidence

Completed chains can produce tamper-evident receipts. Receipt and approval
evidence can include signed, hash-linked records that support later
verification through supported APIs.

The canonical top-level SDK verification APIs are:

- `arclasp.verify_receipt_v2(receipt_id)`
- `arclasp.verify_approval_v2(approval_id)`
- `arclasp.verify_public_token(token)`

Verification responses distinguish integrity status, timestamp-anchor status,
evidence-chain status, and post-issuance governance status where available.

## 8. Public Verification Tokens

Public verification uses scoped opaque tokens with the `arv_` prefix. Tokenized
public verification responses are intentionally minimized compared with
authenticated verification responses.

Plaintext public verification tokens are returned when issued and should be
handled as bearer secrets. Public SDK response models exclude token hashes from
token metadata and avoid echoing opaque tokens in verification error text.

According to the current backend implementation, public verification token
storage uses a hash-only representation rather than storing plaintext tokens.

## 9. Cryptographic Verification Boundaries

Arclasp evidence is designed to be tamper-evident and cryptographically
verifiable through supported verification paths. Some verification is
server-attested: the SDK asks the backend to verify artifact integrity and
returns the backend's role-aware response.

Not every verification mode is universal offline independent verification. The
SDK does not expose signing secrets, and ordinary SDK verification does not
make every historical or key-rotation case independently reproducible offline.

## 10. Timestamp Anchoring

Timestamp-anchor status may be included in verification responses where
configured. Timestamp anchoring is optional and may be asynchronous. Its status
can depend on external timestamp systems and may be pending, unavailable, or
indeterminate for a given artifact.

Timestamp anchoring strengthens evidence when present; it should not be treated
as a universal guarantee for every artifact or workflow.

## 11. Data Minimization And Sanitization

The SDK sanitizes governed payloads before transmission using default
field-name and value-prefix redaction patterns. Defaults cover common
credential and secret shapes, and callers can extend the configured patterns.

Verification responses are minimized by audience. Public token verification
omits administrative identity context and raw secret material. Authenticated
verification can expose more context according to role and capability flags.

## 12. Dependency, CI, And Package Controls

The public package is built and tested separately from the production backend.
The release candidate is checked with package build, Twine rendering, SDK
tests, wheel/sdist hygiene, and GitHub CI on supported Python versions.

Optional framework integrations remain optional package extras. Installing the
base SDK does not make LangChain, LangGraph, CrewAI, or MCP mandatory runtime
dependencies.

## 13. Retention And Deletion Limitations

Evidence systems create deliberate tension between retention, deletion, and
auditability. Teams should evaluate retention and deletion behavior alongside
their own evidence preservation requirements.

Arclasp should not be treated as a legal hold system, compliance archive, or
guarantee that externally executed side effects can be undone.

## 14. Known Trust Boundaries

Security guarantees depend on correct integration. Arclasp can govern actions
that are routed through the SDK or supported adapters; it cannot govern side
effects that bypass those paths.

Operators remain responsible for protecting runtime environments, managing API
keys, configuring policies correctly, securing third-party agent frameworks,
and using HTTPS for non-local backend connections.

The current product brand is Arclasp. Canonical public services use
`api.arclasp.com`, `app.arclasp.com`, `docs.arclasp.com`, and `arclasp.com`.
Legacy ProofRail domains may remain available temporarily for compatibility,
but they are not the canonical public Arclasp brand domains.

Arclasp is not a legal certification, compliance certification, guarantee of
legal admissibility, guarantee of non-repudiation in every context, or guarantee
of universal exactly-once execution.

## 15. Reporting Security Issues

The published reporting address is `security@arclasp.com`.

Please do not report security vulnerabilities through public GitHub issues.
Include a description, affected component, reproduction steps if available, and
your assessment of severity.
