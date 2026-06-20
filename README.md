# ProofRail

Governance for AI agent workflows.

Track what your agents do — individually and together — apply policies to the whole workflow, route risky actions for human approval, and produce tamper-evident audit trails.

## Why this exists

Most AI safety tools evaluate one tool call at a time. That misses the failure mode that actually matters in production: agents that each look fine individually but commit you to something serious in aggregate.

A research agent looks up vendor pricing. A negotiation agent calculates an offer. An email agent drafts the message. A commitment agent records the deal. Each step passes its own per-call review, and the chain quietly hands a vendor $50,000 with no human in the loop.

ProofRail watches the whole chain. Cumulative spend, which agents have run, what external domains they've touched, how close the workflow is to its configured thresholds — that context goes into every policy decision. When a policy says a human needs to sign off, execution actually blocks until they do.

This is built for people running real agents in real systems. Solo developers, small teams, and startups all qualify. You don't need to be at scale to want this; you need to be one chain of agent decisions away from a problem you can't take back.

## Install

```bash
pip install proofrail
```

Framework adapters are optional extras:

```bash
pip install "proofrail[langgraph]"   # LangGraph
pip install "proofrail[langchain]"   # LangChain
pip install "proofrail[crewai]"      # CrewAI
pip install "proofrail[mcp]"         # MCP servers
pip install "proofrail[all]"         # everything
```

## Quick start

The core pattern is framework-agnostic. Open a chain, record each agent action, get a policy decision back.

```python
import proofrail

proofrail.init(api_key="prail_...")

async with proofrail.Chain("checkout-flow", metadata={"order_id": "ord_8821"}) as chain:
    decision = await chain.record_agent_action(
        agent_name="payment-agent",
        action_type="tool_call",
        action_name="charge_card",
        payload={"amount_usd": 350.00, "card_last4": "4242"},
    )
    # Execution reaches here only if the action was allowed.
    # If policy required human approval, that gate has already resolved.
    # decision.decision_source is one of:
    #   "backend_evaluation" | "human_approval" | "local_fast_path"
```

If a policy requires human approval, `record_agent_action` blocks until the reviewer responds. On approval the call returns with `decision_source="human_approval"`. On denial or timeout, it raises `ActionDeniedError`. If the chain itself exceeds its configured timeout, `ChainTimeoutError` raises.

That's the whole core surface. The framework adapters below wrap this same pattern so you don't have to instrument every node by hand.

## Framework adapters

### LangGraph

Wrap a compiled graph once. Every node execution is recorded and evaluated.

```python
import proofrail
from proofrail.langgraph import govern
from langchain_core.messages import HumanMessage

proofrail.init(api_key="prail_...")

governed = govern(compiled_graph, chain_name="research-workflow")
result = await governed.ainvoke({"messages": [HumanMessage(content="Summarize Q3 revenue")]})
```

`governed` has the same `.invoke` / `.ainvoke` signatures as the original graph. If any node is denied, `ActionDeniedError` propagates out of `ainvoke` and the graph halts at that node.

### MCP

Wrap an MCP server's tool handler so every tool call goes through ProofRail before execution.

```python
import proofrail
from proofrail.mcp import ProofRailMcpAdapter
from mcp.server import Server

proofrail.init(api_key="prail_...")
server = Server("my-tools")

async with proofrail.Chain("mcp-session") as chain:
    adapter = ProofRailMcpAdapter(chain=chain, agent_name="my-tools")
    adapter.install(server)
    await server.run(...)
```

A denied tool call returns an MCP-compatible error to the client. Approved calls execute normally.

### CrewAI

```python
import proofrail
from proofrail.crewai import govern

proofrail.init(api_key="prail_...")
governed = govern(crew, chain_name="research-crew")
result = await governed.kickoff_async(inputs={"topic": "AI safety"})
```

See `proofrail/crewai/README.md` for details on CrewAI's specific behaviors and the post-execution governance model.

### LangChain

```python
import proofrail
from proofrail.langchain import govern

proofrail.init(api_key="prail_...")
governed = govern(agent_executor, chain_name="support-agent")
result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
```

## What ProofRail does

### Chain-level governance

Policies see cumulative state across the whole workflow, not just the single event in front of them. Total spend so far, which agents have been active, which external domains have been contacted, how many records have been modified — all of it goes into the decision. A $4,000 charge looks fine in isolation; the same charge after three other $3,000 charges is the one that should require approval.

This is the central design difference from per-call governance tools.

### Local fast-path evaluation

Low-risk, non-financial actions resolve locally in under 5ms with no backend round-trip. The event is still sent to the backend asynchronously so the dashboard and audit log stay accurate. A typical agent workflow has many obviously-safe actions (reading a config, listing items, lookups) interleaved with the few that actually need scrutiny — fast-path means you don't pay network latency on the safe ones.

### Blocking human approval gate

When a policy returns `require_approval`, `record_agent_action` suspends. An email goes to the configured approver with a clean summary of what the chain has done so far and what action triggered the gate. The approver clicks through, approves or denies with optional notes, and the workflow resumes (or raises `ActionDeniedError` on denial).

Timeout, fallback approvers, and time-boxed exceptions are all configurable per policy. A denied or timed-out approval surfaces in your code as a typed exception with structured remediation hints.

### Tamper-evident audit receipts

Every chain closes with an HMAC-SHA256 signed receipt. Anyone with a receipt ID can verify it through our public no-auth endpoint, and the receipt's hash chain to its predecessor is publicly walkable for end-to-end integrity verification.

Receipts are also hash-chained across an organization: each new receipt embeds the hash of the previous one. Tampering with any single receipt breaks the chain in a way that's publicly detectable. Delete a receipt entirely, and the gap shows up the same way.

### Parity-tested policy engine

The local policy engine is the same algorithm that runs on the backend. They're verified against identical inputs on every test run. If they diverge, the build fails. The full algorithm is in [`proofrail/policies.py`](proofrail/policies.py) — read it before you install if you want to know exactly what rules your agents are subject to.

### Per-action-class fail modes

When the backend is unreachable, you don't have to choose between "everything fails closed" and "everything fails open." Configure different fail modes per action class:

```python
proofrail.init(
    api_key="prail_...",
    fail_modes={
        "financial": "deny",
        "external_communication": "deny",
        "destructive": "deny",
        "default": "allow",
    },
)
```

Payments and external emails fail closed. Reads and drafts fail open. Velocity stays intact for the things where velocity is fine; the dangerous actions still get the brake pedal they need.

## More features

ProofRail ships with a number of additional capabilities, configured per-organization through the dashboard or per-chain through SDK options:

- **Policy shadow mode.** Run new policies in observe-only mode against real traffic before flipping them to enforce. Shadow decisions are logged separately from enforced ones so you can calibrate without disruption.
- **Agent registry.** Pre-declare every agent you expect to see. Unregistered agents that show up in chain events are flagged for review, surfacing shadow agents without blocking legitimate work.
- **Cost tracking and budget alerts.** Token usage and dollar cost tracked per chain, per agent, and per model. Alerts at 80% of monthly budget; auto-gate at 100%.
- **Org-wide kill switch.** When something goes wrong, an admin can halt all agent activity with one click. Raises `ProofRailKillSwitchError` so applications can distinguish a halt from a policy violation and respond appropriately.
- **Admin audit log.** Every dashboard action — policy edits, kill switch toggles, approver changes, key rotations — is logged with before/after diff to an append-only table. Visible to admins for compliance review.
- **Time-boxed policy exceptions.** Approvers can grant exceptions for a specific scope and duration (one hour, one day, one week, single use). Exceptions auto-expire; no permanent allow-lists by accident.
- **Payload sanitization.** Default redaction patterns cover API keys, passwords, credit cards, SSNs, private keys, and several common token formats (OpenAI, Stripe, GitHub, Hugging Face, AWS, JWT). Extend with your own patterns. Raw payloads are never persisted.
- **Offline buffer with deduplication.** Events buffer locally when the backend is unreachable, with idempotency keys so retries don't duplicate audit records.
- **Cross-organization isolation.** Every UUID-bearing endpoint enforces org scoping. Tests confirm one organization's API key can never access another organization's chains, events, or receipts.

## How it works

Two components: this SDK (open-source, Apache 2.0) and a hosted backend (closed, operated by us). The SDK handles chain lifecycle, payload sanitization, and a local fast-path for obviously-safe actions. Anything the fast-path won't evaluate — financial actions, high-risk agents, actions near a configured threshold — goes to the backend for an authoritative decision.

The reference policy that powers the fast-path lives in [`proofrail/policies.py`](proofrail/policies.py). The backend runs equivalent logic, and the parity tests in [`tests/test_policies_backend_parity.py`](tests/test_policies_backend_parity.py) verify the two stay in sync.

The backend implementation itself is not open-source. The reasons are practical (operational complexity) rather than ideological, and we publish the policy algorithm in full so you can verify what the backend is doing.

## Supported frameworks

| Framework | Adapter | Notes |
|-----------|---------|-------|
| LangGraph | `proofrail.langgraph.govern` | Node-level instrumentation via `astream_events` |
| LangChain | `proofrail.langchain.govern` | Via `AsyncCallbackHandler` |
| CrewAI | `proofrail.crewai.govern` | Post-execution governance; see CrewAI README for details |
| MCP | `proofrail.mcp.ProofRailMcpAdapter` | Tool-call instrumentation for MCP servers |

For the framework-agnostic case (custom agent loops, untested frameworks, or your own orchestration), use the `Chain` context manager directly as shown in Quick start above.

## Limitations

What this release doesn't do, so you find out from us and not from production:

- Single region. The backend runs in US-East. European and APAC users may see 100-150ms additional latency. Multi-region is on the roadmap.
- Backend round-trip for most actions. The local fast-path handles obviously-safe cases; everything else hits the backend.
- CrewAI deny decisions can't halt a task mid-execution due to how CrewAI's architecture handles synchronous task execution. Governance events are still recorded and the audit trail is complete; the decision surfaces as `ActionDeniedError` on the next event loop cycle rather than at the exact point the task runs. See `proofrail/crewai/README.md` for details.
- No SSO beyond what Clerk provides out of the box.
- Email-only approval notifications. Slack and Teams integrations are planned, not shipped.

If any of these is a blocker for your use case, file an issue. We'd rather tell you honestly whether to wait than have you discover the limitation in production.

## Trust

We're asking you to install a governance SDK and let it sit in the path of every agent action your product takes. That's a real ask, and the answer to "should I trust this?" shouldn't be "the marketing site says so."

The whole SDK is in this repo. You can read every line of code that runs in your process. The fast-path that decides actions locally is in [`proofrail/policies.py`](proofrail/policies.py) — open it; that's the entire algorithm. The payload sanitizer that decides what leaves your machine is in [`proofrail/sanitization.py`](proofrail/sanitization.py). The HTTP client and every payload format the SDK sends are inspectable.

The backend isn't open-source. What we've done instead:

- **Parity tests.** The SDK's local policy and the backend's policy run against identical inputs on every test run. If they diverge — if the backend decides something the open-source code wouldn't — the build fails. The tests are in [`tests/test_policies_backend_parity.py`](tests/test_policies_backend_parity.py).
- **Cross-organization isolation tests.** A dedicated test suite confirms one organization's API key cannot reach another organization's chains, events, or receipts. The application enforces org scoping on every UUID-bearing endpoint.
- **Publicly verifiable receipts.** Audit receipts are HMAC-signed and hash-chained across an organization. Anyone with a receipt ID can verify it through our public no-auth endpoint. Tamper with one and the chain breaks publicly.
- **Security audit complete.** Fifteen findings covering the SDK have been addressed; the full security policy is in [SECURITY.md](SECURITY.md).

Most agent governance tools ask you to trust a closed-source policy engine. ProofRail's policy engine is open. Read it before you install it.

Vulnerability reports: see [SECURITY.md](SECURITY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).

## Status

Public beta. We read every issue and respond. File at [github.com/TOAAiV/proofrail/issues](https://github.com/TOAAiV/proofrail/issues).
