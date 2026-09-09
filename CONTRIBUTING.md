# Contributing to Arclasp

Thanks for considering a contribution to Arclasp.

This repository contains the public Python SDK, supported framework integrations, tests, examples, and release metadata for Arclasp. The hosted governance backend and dashboard are separate Arclasp infrastructure; opening an issue or pull request here does not imply that every server-side feature is implemented in this repository.

The best contributions are focused, reproducible, and easy to review.

## Before you start

For a small bug fix, documentation correction, or test improvement, you can open a pull request directly.

For a large behavioral change, a new public API, a new framework integration, or anything that changes governance semantics, open an issue first. That gives us a chance to agree on the boundary before substantial implementation work begins.

Security vulnerabilities should **not** be discussed in a public issue. Follow `SECURITY.md` and report them privately to **security@arclasp.com**.

## Development setup

Arclasp supports Python 3.10 and newer. Python 3.11 is a good default for local development because it is also used in release validation.

Clone the repository and create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

On macOS or Linux:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Upgrade pip and install the SDK with development dependencies and the supported optional integrations:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev,all]"
```

If you are working only on the framework-agnostic SDK, you can install a smaller dependency set. If you modify a framework adapter, install the corresponding extra and run its integration tests.

## Run the tests

Run the public test suite before submitting a pull request:

```bash
python -m pytest -q
```

Some tests are intentionally environment-dependent and may skip when an optional framework runtime is unavailable. A skip should be explainable; do not turn a failing regression test into a skip simply to make CI green.

Run lint checks as well:

```bash
python -m ruff check .
```

If your change affects packaging, release metadata, imports, or optional dependencies, also build the distributions locally:

```bash
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

The release pipeline performs additional clean-environment and artifact checks before publication.

## Keep changes narrow

A pull request should solve one understandable problem.

Please avoid mixing a behavior change with broad formatting, unrelated refactors, dependency churn, and documentation rewrites in the same PR. Narrow changes are easier to review and safer in a governance SDK where small control-flow differences can change runtime behavior.

If a refactor is necessary to support the change, explain why in the pull request.

## Preserve the governance boundary

Arclasp is not a normal convenience wrapper. Some implementation choices are security and product semantics.

Changes must preserve these principles unless the project explicitly decides to change them:

**Backend authority.** A governed operation that requires backend authority must not silently acquire a local allow path.

**Fail-closed behavior.** Backend uncertainty must not quietly become permission for governed execution.

**Customer-side execution.** Arclasp governs configured actions; the customer application still performs the real tool call or side effect after governance permits it.

**Scoped evidence.** Do not broaden what public verification, logs, errors, examples, or artifacts expose without reviewing the privacy and security impact.

**Precise claims.** Do not change documentation to claim tamper-proof records, guaranteed compliance, legal admissibility, universal exactly-once execution, universal result capture, mature enterprise SSO/custom RBAC, mature self-hosting, or other capabilities that are not part of the current product contract.

## Public API changes

The supported top-level API is intentionally small.

Before adding a new exported symbol, changing an exception contract, or altering the normal `init` / `Chain` / `record_agent_action` path, explain why the change belongs in the public API instead of an internal module or framework-specific surface.

Public API changes should include:

- tests covering the new behavior
- documentation updates
- changelog notes when user-visible
- compatibility analysis for existing integrations

Do not globally rename compatibility identifiers such as the current `prail_` API-key prefix without a deliberate migration plan.

## Framework integrations

Arclasp currently supports custom Python workflows and integration surfaces for LangChain, LangGraph, CrewAI, and MCP.

When changing an adapter:

- test against the real framework dependency where the test is intended to verify real framework behavior
- preserve existing callbacks or configuration instead of replacing customer state unnecessarily
- make sure governance exceptions are not swallowed by framework callback machinery
- restore temporary monkey patches or hooks even when execution fails
- be explicit about whether the adapter observes invocation, result, error, or only a subset of those events
- do not claim universal tool-result capture where the adapter cannot provide it

A framework upgrade that changes callback or execution behavior should come with a regression test for the affected path.

## Tests should prove behavior

Prefer tests that assert externally meaningful behavior rather than implementation details.

Good tests answer questions such as:

- Did the governed action stop on deny?
- Did a required approval block until resolution?
- Did backend unavailability fail closed?
- Did the Chain preserve cumulative state?
- Did the adapter let the governance exception escape?
- Did sanitization remove the supported sensitive value from a covered event-payload or chain-metadata path?
- Did the package expose only the intended public contract?

Avoid tests that depend on timing luck, real customer data, or mutable production state.

## Documentation changes

Documentation is part of the product contract.

When editing public copy:

- use **Arclasp** as the current product name
- use canonical public domains such as `arclasp.com`, `docs.arclasp.com`, `app.arclasp.com`, and `api.arclasp.com`
- keep intentional compatibility identifiers when the product still uses them
- distinguish tamper-evident evidence from tamper-proof claims
- distinguish recorded LLM cost governance from provider billing
- state that customer applications execute their own tools
- keep public-beta limitations accurate

If behavior and documentation disagree, do not paper over the mismatch with wording. Fix the behavior, fix the documentation, or raise the discrepancy explicitly.

## Examples and test data

Examples must use synthetic data.

Do not commit:

- real API keys or tokens
- private keys or certificates
- customer payloads
- personal email addresses used as live recipients
- production database URLs
- internal service credentials
- copied production logs containing identifiers or secrets

Use obviously synthetic values such as `finance@example.com` and test-only keys.

Before committing a new fixture, ask whether it is necessary for a public SDK repository and whether it reveals anything that a user needs in order to understand or verify the SDK.

## Pull request checklist

Before opening a pull request, make sure:

- the change has a clear purpose
- relevant tests were added or updated
- `python -m pytest -q` passes in the intended development environment
- `python -m ruff check .` passes
- user-facing behavior is documented
- the changelog is updated when the change is release-noteworthy
- no secrets, customer data, or machine-specific paths were added
- new dependencies are justified and appropriately bounded
- public API changes are deliberate
- security-sensitive behavior has not been weakened accidentally

In the pull request description, explain **what changed, why it changed, and how you verified it**.

## Bug reports

A useful bug report usually includes:

- Arclasp version
- Python version
- operating system
- framework and framework version, if relevant
- a minimal reproduction
- expected behavior
- actual behavior
- sanitized error output or traceback

Please remove API keys, tokens, customer data, and other secrets before posting.

## Feature requests

Feature requests are welcome, especially when they describe the workflow problem rather than only a proposed API shape.

Useful context includes:

- what the agent is allowed to do
- where the consequential side effect occurs
- what decision should happen before execution
- what workflow state the decision depends on
- whether a human reviewer is involved
- what evidence needs to exist afterward
- which framework or runtime is involved

That context helps determine whether the request belongs in the SDK, the hosted governance service, an integration, or documentation.

## License

Arclasp's public Python SDK is licensed under the Apache License 2.0.

By contributing code or documentation that is accepted into this repository, you agree that the contribution may be distributed under the repository's Apache-2.0 license.
