# Public Roadmap

What's planned after the initial release. Directional, not a committed timeline —
priorities shift based on what people actually try to use ProofRail for.

If something on this list is the difference between adopting ProofRail and not,
file an issue or email me. That's how priorities get adjusted — louder than
internal guesses about what matters.

## Near-term

**Additional framework adapters.**
LangGraph, LangChain, CrewAI, and MCP shipped with the initial release. AutoGen
and LlamaIndex are candidates for later releases. No ETA — I want to see
real demand before committing.

**Slack and Teams approval integration.**
The human approval gate currently surfaces via email and the ProofRail dashboard.
Native Slack and Teams integrations would let reviewers approve or deny directly
from a message, without opening a separate UI. This is high on the list.

**Improved CrewAI pre-execution interception.**
The current CrewAI adapter records governance events at task execution time but
can't block mid-flight denials synchronously — CrewAI's architecture doesn't
expose the hook we'd need. Tracking upstream changes; if their callback API
opens up, this gets fixed.

## Medium-term

**Self-hosted deployment.**
A Docker-based deployment for teams that can't send chain data to a hosted
service. Significant undertaking — depends on demand from the beta. If your
security team won't sign off on a hosted backend, tell me; that's the signal
that moves this up.

**Multi-region deployment.**
The hosted backend currently runs in a single region (US-East). Multi-region is
on the roadmap for organizations with data-residency requirements.

## Longer-term

**Policy-as-code editor.**
Right now policies are configured in the dashboard. A code-based policy format
(YAML or a small DSL) would make policies version-controllable and reviewable
in PRs.

**Non-Python SDKs.**
TypeScript first, then likely Go. Not happening soon — the Python SDK needs to
be solid before I split focus.
