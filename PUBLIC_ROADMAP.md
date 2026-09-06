# Public Roadmap

What's planned after the initial release. This is directional, not a committed
timeline, and priorities may change based on customer usage, security review,
and product learning during the public beta.

If something on this list is the difference between adopting Arclasp and not,
open an issue with your use case once the repository is public.

## Near-term

**Additional framework adapters.**
LangGraph, LangChain, CrewAI, and MCP shipped with the initial release. AutoGen
and LlamaIndex are possible future candidates, but no additional adapter is
committed until there is enough demand and a reliable governance boundary.

**Slack and Teams approval integration.**
The human approval gate currently surfaces via email and the Arclasp dashboard.
Native Slack and Teams integrations would let reviewers approve or deny directly
from a message, without opening a separate UI.

**Improved CrewAI pre-execution interception.**
The current CrewAI adapter records governance events at task execution time but
does not provide a universal synchronous pre-execution barrier for every task
body. Future work may improve this boundary if supported framework hooks make
that reliable.

## Medium-term

**Self-hosted deployment.**
A self-hosted deployment path may matter for teams that cannot send governed
workflow data to a hosted service. This would be a significant product and
support commitment, so it remains exploratory during the public beta.

**Multi-region deployment.**
Broader deployment-region options may become important for latency,
availability, and data-residency requirements.

## Longer-term

**Policy-as-code editor.**
Right now policies are configured in the dashboard. A code-based policy format
(YAML or a small DSL) would make policies version-controllable and reviewable
in PRs.

**Non-Python SDKs.**
TypeScript and Go are possible future SDK directions after the Python SDK and
hosted governance model are stable enough to support additional language
surfaces.
