# Security Policy

Security is part of Arclasp's product boundary, not a marketing label. This document explains which releases receive security fixes, what kinds of issues we want reported privately, and the limits that matter when testing or deploying Arclasp.

Arclasp is currently in public beta. The open-source repository contains the Python SDK and supported integrations; the hosted Arclasp governance service is operated separately. Security issues affecting either the public SDK or the hosted service can be reported through the process below.

## Supported versions

During the public beta, Arclasp supports the latest published public-beta release. Security fixes may require upgrading to that release.

| Version | Security support |
| --- | --- |
| Latest published public-beta release | Supported |

This policy will be revised when Arclasp reaches a stable release line with a longer-term compatibility policy.

## Security model

A few boundaries are important to understand before deploying or testing Arclasp.

**Backend-authoritative governance.** Governed SDK actions rely on the Arclasp backend for authoritative policy decisions. The SDK does not silently replace a required backend decision with a local allow decision.

**Fail-closed behavior.** If a required governance decision cannot be obtained after the configured retry behavior, the governed operation raises an error instead of implicitly continuing.

**Organization scoping.** SDK API keys and authenticated product operations are scoped to the relevant Arclasp organization. Cross-organization access is not an intended capability.

**Sensitive-data handling.** The SDK applies pattern-based sanitization and redaction to supported event-payload and chain-metadata paths before submission. These controls are defense in depth, not a guarantee that every secret or sensitive value will be detected. Applications should still minimize sensitive data and keep credentials out of source code, logs, screenshots, issue reports, and test fixtures.

**Approval and verification boundaries.** Approval decisions may be submitted through authenticated product flows or signed approval-token mechanisms, and resolved approvals may produce signed certificate evidence. Public verification uses scoped tokens and exposes a narrower projection than authenticated verification.

**Tamper-evident evidence.** Completed governance evidence can use signing and hash linkage with supported verification paths. Arclasp describes this as tamper-evident, not tamper-proof. It is not a guarantee of legal admissibility, regulatory compliance, legal non-repudiation, or proof of an external action that the customer did not instrument.

**Customer-side execution.** Arclasp governs actions that actually pass through the Arclasp governance boundary. It does not remotely control arbitrary customer code or guarantee exactly-once execution of customer-side tools.

## API keys

Treat an Arclasp API key as a secret. Store it in an environment variable or appropriate secret-management system, avoid logging it, and rotate or revoke it if you believe it has been exposed.

The current `prail_` prefix is an intentional compatibility identifier for Arclasp API keys.

Do not include real API keys in:

- GitHub issues or pull requests
- logs or screenshots
- example projects
- benchmark output
- support messages unless a secure support process explicitly requires it

## Network transport

For the hosted service, use the canonical HTTPS endpoint:

`https://api.arclasp.com`

Plain HTTP should be limited to intentional local-development use, such as a loopback backend on `localhost` or `127.0.0.1`.

Do not disable TLS verification in production integrations.

## What to report privately

Examples of issues that should be reported to the security address include:

- cross-organization data access or authorization bypass
- API-key exposure or authentication bypass
- a path that lets governed execution silently bypass a required backend decision
- approval-token forgery, replay, or authorization problems
- public-verification exposure beyond the intended scoped projection
- receipt or approval-certificate signing/verification flaws with security impact
- sanitization failures that expose sensitive information in a way the documented boundary should prevent
- code execution, injection, request smuggling, or similar vulnerabilities in Arclasp-controlled surfaces
- dependency vulnerabilities that are actually exploitable through Arclasp

Documentation mistakes, ordinary feature requests, expected public-beta limitations, and general support questions can use the public issue tracker once the repository is public.

## Responsible security research

Please limit security testing to systems, accounts, and data you are authorized to use. Avoid disrupting production availability, accessing or retaining another user's data, targeting third-party services, or using social engineering.

If testing unexpectedly exposes another user's data or a production secret, stop and report it promptly. Please allow a reasonable opportunity to investigate and remediate a vulnerability before public disclosure.

## Reporting a vulnerability

Email:

**security@arclasp.com**

Please do not report security vulnerabilities through a public GitHub issue.

If GitHub Private Vulnerability Reporting is enabled for this repository, you may also use GitHub's private vulnerability-reporting workflow.

A useful report includes:

- a clear description of the issue
- the affected Arclasp component and version
- reproduction steps or a minimal proof of concept
- the security impact you believe is possible
- relevant logs, request/response examples, or screenshots with secrets removed
- any suggested mitigation, if you have one

You do not need a polished write-up before contacting us. If the issue appears serious, send the initial report and we can coordinate details privately.

## Disclosure process

We aim to acknowledge security reports promptly, investigate reported issues in good faith, and coordinate remediation and disclosure where appropriate. Please allow a reasonable remediation window before public disclosure, especially where a fix requires coordinated deployment of the hosted service and a new SDK release.

Arclasp does not currently advertise a public bug-bounty program. A security report is welcome regardless of whether a bounty program exists.

We may credit reporters in release notes or security advisories when appropriate, unless anonymity is requested.

## Security advisories and fixes

When a vulnerability affects the public SDK, fixes may be released through a new PyPI version and documented in the changelog or a GitHub security advisory where appropriate.

When an issue affects the hosted service, remediation may be deployed server-side without requiring an SDK release.

Users should keep the Arclasp SDK current during the public beta because security fixes may require upgrading to the latest public-beta release.

## General support

For non-security questions, use the public issue tracker once the repository is public or contact:

**support@arclasp.com**

Product documentation is available at:

https://docs.arclasp.com
