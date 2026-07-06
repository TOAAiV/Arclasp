# ProofRail

Governance for AI agent workflows.

Track what your agents do — individually and together — apply policies to the whole workflow, route risky actions for human approval, and produce tamper-evident audit trails.

## Why this exists

Most AI safety tools evaluate one tool call at a time. That misses the failure mode that actually matters in production: agents that each look fine individually but commit you to something serious in aggregate.

A research agent looks up vendor pricing. A negotiation agent calculates an offer. An email agent drafts the message. A commitment agent records the deal. Each step passes its own per-call review, and the chain quietly hands a vendor `$50,000` with no human in the loop.

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

**Python version notes:**

- The base SDK and all adapters except CrewAI support Python 3.10+, including 3.14.
- `proofrail[crewai]` requires **Python 3.10–3.13**. CrewAI's own dependencies are not yet compatible with Python 3.14. If you're on Python 3.14, use the other adapters or pin your CrewAI environment to Python 3.13.

## Quick start

The core pattern is framework-agnostic. Open a chain, record each agent action, get a policy decision back.

```python
import proofrail

proofrail.init(api_key="prail_...")

chain = proofrail.Chain("checkout-flow", metadata={"order_id": "ord_8821"})
chain.add_financial_threshold(usd=10_000, notify=["ops@yourco.com"])

async with chain:
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

`add_financial_threshold()` sets a cumulative spend limit for *this chain only* — if your org already has a default via `proofrail.init(cumulative_financial_threshold_usd=...)`, this overrides it just for `checkout-flow`. See [Composing custom policies](#composing-custom-policies) below for the full per-chain configuration surface.

If a policy requires human approval, `record_agent_action` blocks until the reviewer responds. On approval the call returns with `decision_source="human_approval"`. On denial or timeout, it raises `ActionDeniedError`. If the chain itself exceeds its configured timeout, `ChainTimeoutError` raises.

That's the whole core surface. The framework adapters below wrap this same pattern so you don't have to instrument every node by hand.

## Documentation

Full documentation — framework adapter guides, policy configuration, dashboard usage, cost tracking, kill switch, approval flows — is at **https://docs.proofrail.dev**.

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

See https://docs.proofrail.dev/frameworks/langgraph for LangGraph integration details.

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

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        return await adapter.handle_tool_call(
            tool_name=name,
            arguments=arguments,
            handler=your_actual_handler,
        )

    await server.run(...)
```

A denied tool call raises `ActionDeniedError` out of `handle_tool_call`. Approved calls execute normally.

See https://docs.proofrail.dev/frameworks/mcp for MCP integration details.

### CrewAI

> **Python 3.14:** CrewAI's dependencies do not yet support Python 3.14. On Python 3.14, `proofrail[crewai]` installs the base SDK but skips CrewAI itself. Use Python 3.10–3.13 if you need CrewAI integration.

> **Note for Python 3.11 users:** If you see a `distutils_hack` assertion error when installing `proofrail[crewai]`, set `SETUPTOOLS_USE_DISTUTILS=stdlib` before running pip install. This is a CrewAI dependency packaging issue, not a ProofRail one.
> ```bash
> SETUPTOOLS_USE_DISTUTILS=stdlib pip install "proofrail[crewai]"
> ```
> On Windows: `$env:SETUPTOOLS_USE_DISTUTILS="stdlib"; pip install "proofrail[crewai]"`

```python
import proofrail
from proofrail.crewai import govern

proofrail.init(api_key="prail_...")
governed = govern(crew, chain_name="research-crew")
result = await governed.kickoff_async(inputs={"topic": "AI safety"})
```

See [proofrail/crewai/README.md](https://github.com/TOAAiV/proofrail/blob/master/proofrail/crewai/README.md) for details on CrewAI's specific behaviors and the post-execution governance model.

See https://docs.proofrail.dev/frameworks/crewai for CrewAI integration details.

### LangChain

```python
import proofrail
from proofrail.langchain import govern

proofrail.init(api_key="prail_...")
governed = govern(agent_executor, chain_name="support-agent")
result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
```

See https://docs.proofrail.dev/frameworks/langchain for LangChain integration details.

## What ProofRail does

### Chain-level governance

Policies see cumulative state across the whole workflow, not just the single event in front of them. Total spend so far, which agents have been active, which external domains have been contacted, how many records have been modified — all of it goes into the decision. A `$3,000` charge looks fine in isolation. The same charge after nine prior `$300` charges — nine steps that each passed review — is the one that should require approval.

This is the central design difference from per-call governance tools.

### Local fast-path evaluation

Low-risk, non-financial actions resolve locally without a backend round-trip when `environment="development"` is set in `proofrail.init()`. The event is still sent to the backend asynchronously so the dashboard and audit log stay accurate. A typical agent workflow has many obviously-safe actions (reading a config, listing items, lookups) interleaved with the few that actually need scrutiny — fast-path means you don't pay network latency on the safe ones. Fast-path is disabled in `environment="production"` (the default) so the production backend is always authoritative.

### Blocking human approval gate

When a policy returns `require_approval`, `record_agent_action` suspends. An email goes to the configured approver with a clean summary of what the chain has done so far and what action triggered the gate. The approver clicks through the dashboard, approves or denies with optional notes, and the workflow resumes (or raises `ActionDeniedError` on denial). Approval decisions are currently made through the dashboard; programmatic resume via API key is not supported in this release.

Timeout, fallback approvers, and time-boxed exceptions are all configurable per policy. A denied or timed-out approval surfaces in your code as a typed exception with structured remediation hints.

### Tamper-evident audit receipts

Every chain closes with an HMAC-SHA256 signed receipt. Receipts are hash-chained across an organization — each embeds the hash of the previous receipt. Anyone with a receipt ID can verify it through the public no-auth `/v1/receipts/{id}/verify` endpoint — no API key required.

Receipts are also hash-chained across an organization: each new receipt embeds the hash of the previous one. Tampering with any single receipt breaks the chain in a way that's publicly detectable. Delete a receipt entirely, and the gap shows up the same way.

### Parity-tested policy engine

The local policy engine is the same algorithm that runs on the backend. They're verified against identical inputs on every test run. If they diverge, the build fails. The full algorithm is in [`proofrail/policies.py`](https://github.com/TOAAiV/proofrail/blob/master/proofrail/policies.py) — read it before you install if you want to know exactly what rules your agents are subject to.

### Per-action-class fail modes

When the backend is unreachable, you don't have to choose between "everything fails closed" and "everything fails open." Configure different fail modes per action class:

```python
proofrail.init(
    api_key="prail_...",
    fail_modes={
        "tool_call": "deny",       # block all tool calls when backend is unreachable
        "llm_inference": "allow",  # allow LLM calls — no external side-effects
        "default": "allow",
    },
)
```

Keys must match the `action_type` strings your code passes to `record_agent_action`. Framework adapters record `"tool_call"` and `"llm_inference"`. For custom action classes, use any string you define.

Tool calls fail closed. Reads and drafts fail open. Velocity stays intact for the things where velocity is fine; the dangerous actions still get the brake pedal they need.

## Composing custom policies

`add_financial_threshold()` is a facade over a plain dict — `Chain.policy_config` — sent once in the chain-creation request and merged by the backend over your org's default config (chain value wins where set, org value fills the rest). There's no separate class hierarchy; the facade and the raw dict form produce byte-identical requests, so reach for whichever fits your code.

```python
# Facade — covers the common case (one threshold, one notify list)
chain = proofrail.Chain("vendor-payouts")
chain.add_financial_threshold(usd=10_000, notify=["finance@yourco.com"])

# Equivalent raw dict — same wire format, useful when composing config
# programmatically or setting fields the facade doesn't expose yet
chain = proofrail.Chain(
    "vendor-payouts",
    policy_config={
        "cumulative_financial_threshold_usd": 10_000,
        "notify": ["finance@yourco.com"],
    },
)
```

Recognised `policy_config` keys:

| Key | Type | Effect |
|---|---|---|
| `cumulative_financial_threshold_usd` | `float` | Overrides the org's cumulative spend threshold for this chain only. |
| `financial_approval_threshold_usd` | `float` | Overrides the org's single-transaction approval threshold for this chain only. |
| `cumulative_financial_threshold_action` | `"pause_for_approval"` \| `"deny"` | What happens when the cumulative threshold crosses. Defaults to pausing for human approval; set `deny` (or pass `deny=True` to the facade) to hard-deny instead. |
| `notify` | `list[str]` | Additional approver emails for this chain, unioned with `fallback_approvers` and deduplicated — does not replace them. |

A chain with no `policy_config` (or `{}`) behaves exactly like one with no override at all — the org-wide config from `proofrail.init()` applies unchanged.

## More features

Features marked **[SDK]** are available to every caller with an API key. Features marked **[Dashboard]** require the web dashboard (or Clerk JWT auth) and are intended for human operators.

- **Payload sanitization** [SDK]. Default redaction patterns cover API keys, passwords, credit cards, SSNs, private keys via field-name matching, plus common token formats by value prefix (Stripe `sk_`/`pk_`, GitHub `ghp_`, Hugging Face `hf_`, AWS access key IDs `AKIA`, JWT `eyJ`). Extend with your own patterns. Raw payloads are never persisted.
- **Per-action-class fail modes** [SDK]. Covered above; `fail_modes` per action type when the backend is unreachable. Note: per-class overrides apply to backend evaluation; global `fail_mode` governs offline-stub decisions.
- **Offline buffer** [SDK]. When the backend is unreachable and `fail_mode="allow"`, actions resolve locally via `source=offline_stub`. These events are not sent to the backend after reconnect — they are absent from the audit trail. Use `fail_mode="deny"` if audit completeness is required.
- **Cross-organization isolation** [SDK + Backend]. Every UUID-bearing endpoint enforces org scoping at the backend. Tests confirm one organization's API key can never access another organization's chains, events, or receipts.
- **Policy shadow mode** [Dashboard]. Run new policies in observe-only mode against real traffic before flipping them to enforce. Shadow decisions are logged separately so you can calibrate without disruption. `evaluation_mode` and `shadow_decision` appear in SDK responses when active.
- **Agent registry** [Dashboard]. Register every agent you expect to see via the dashboard. Unregistered agents that show up in chain events are flagged for review, surfacing shadow agents without blocking legitimate work. Note: `registered_agents=[...]` in `proofrail.init()` only affects the local fast-path evaluator and has no effect when `environment="production"` (the default).
- **Cost tracking and budget alerts** [Dashboard]. Token usage and dollar cost tracked per chain, per agent, and per model. Alerts at 80% of monthly budget; auto-gate at 100%. Accessible via the dashboard; not exposed to API key auth.
- **Org-wide kill switch** [Dashboard — admin only]. When something goes wrong, an admin can halt all agent activity with one click. The SDK raises `ProofRailKillSwitchError` so applications can distinguish a halt from a policy violation.
- **Admin audit log** [Dashboard — admin only]. Every dashboard action — policy edits, kill switch toggles, approver changes, key rotations — is logged with before/after diff to an append-only table for compliance review.
- **Time-boxed policy exceptions** [Dashboard]. Approvers can grant exceptions for a specific scope and duration (one hour, one day, one week, single use). Exceptions auto-expire; no permanent allow-lists by accident. Exceptions are created through the approval workflow, not directly via the SDK.

## How it works

Two components: this SDK (open-source, Apache 2.0) and a hosted backend (closed, operated by us). The SDK handles chain lifecycle, payload sanitization, and a local fast-path for obviously-safe actions. Anything the fast-path won't evaluate — financial actions, high-risk agents, actions near a configured threshold — goes to the backend for an authoritative decision.

The reference policy that powers the fast-path lives in [`proofrail/policies.py`](https://github.com/TOAAiV/proofrail/blob/master/proofrail/policies.py). The backend runs equivalent logic, and the parity tests in [`tests/test_policies_backend_parity.py`](https://github.com/TOAAiV/proofrail/blob/master/tests/test_policies_backend_parity.py) verify the two stay in sync.

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

- Single region. The backend runs in AWS us-east-1 (Virginia). European and APAC users may see 100-150ms additional latency. Multi-region is on the roadmap.
- Backend round-trip for most actions. The local fast-path handles obviously-safe cases; everything else hits the backend.
- CrewAI governance fires synchronously per task: on task start, governance evaluates before the task runs; on task end, it fires after the task completes. An `ActionDeniedError` propagates immediately through `kickoff_async`, halting the remaining crew. The task that was already running when governance fires cannot be retroactively prevented. See [proofrail/crewai/README.md](https://github.com/TOAAiV/proofrail/blob/master/proofrail/crewai/README.md) for details.
- No SSO beyond what Clerk provides out of the box.
- Email-only approval notifications. Slack and Teams integrations are planned, not shipped.

- Approval emails may land in spam on first delivery. Add `notifications@proofrail.dev` to your contacts to avoid this.
- First request after 15 minutes of inactivity may take 5–15 seconds due to backend cold start (free tier).
- On Python 3.11, installing `proofrail[crewai]` requires `SETUPTOOLS_USE_DISTUTILS=stdlib` set before pip. See the CrewAI section above.
- `fail_modes` keys must match your `action_type` strings, not risk categories. Adapter users: use `"tool_call"` and `"llm_inference"` as keys.

If any of these is a blocker for your use case, file an issue. We'd rather tell you honestly whether to wait than have you discover the limitation in production.

## Trust

We're asking you to install a governance SDK and let it sit in the path of every agent action your product takes. That's a real ask, and the answer to "should I trust this?" shouldn't be "the marketing site says so."

The whole SDK is in this repo. You can read every line of code that runs in your process. The fast-path that decides actions locally is in [`proofrail/policies.py`](https://github.com/TOAAiV/proofrail/blob/master/proofrail/policies.py) — open it; that's the entire algorithm. The payload sanitizer that decides what leaves your machine is in [`proofrail/sanitization.py`](https://github.com/TOAAiV/proofrail/blob/master/proofrail/sanitization.py). The HTTP client and every payload format the SDK sends are inspectable.

The backend isn't open-source. What we've done instead:

- **Parity tests.** The SDK's local policy and the backend's policy run against identical inputs on every test run. If they diverge — if the backend decides something the open-source code wouldn't — the build fails. The tests are in [`tests/test_policies_backend_parity.py`](https://github.com/TOAAiV/proofrail/blob/master/tests/test_policies_backend_parity.py).
- **Cross-organization isolation tests.** A dedicated test suite confirms one organization's API key cannot reach another organization's chains, events, or receipts. The application enforces org scoping on every UUID-bearing endpoint.
- **Publicly verifiable receipts.** Audit receipts are HMAC-signed and hash-chained across an organization. Anyone with a receipt ID can verify it through the public no-auth `/v1/receipts/{id}/verify` endpoint. Tamper with one and the chain breaks publicly.
- **Security audit complete.** Fifteen findings covering the SDK have been addressed; the full security policy is in [SECURITY.md](https://github.com/TOAAiV/proofrail/blob/master/SECURITY.md).

Most agent governance tools ask you to trust a closed-source policy engine. ProofRail's policy engine is open. Read it before you install it.

Vulnerability reports: see [SECURITY.md](https://github.com/TOAAiV/proofrail/blob/master/SECURITY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).

## Status

Public beta. We read every issue and respond. File at [github.com/TOAAiV/proofrail/issues](https://github.com/TOAAiV/proofrail/issues).
