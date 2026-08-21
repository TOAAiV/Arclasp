# Arclasp

## Govern the workflow, not just the API call.

Arclasp is runtime governance for AI-agent workflows. It tracks cumulative risk
across a sequence of actions and enforces boundaries using the state of the
whole workflow, not just the current call.

When a workflow crosses a configured boundary, Arclasp can pause it for human
approval before execution continues.

**Backend-authoritative. Fail-closed.**

LangChain | LangGraph | CrewAI | MCP | custom Python

```bash
pip install arclasp
```

## Why Cumulative Governance Matters

A safe action can be part of an unsafe workflow.

Suppose a workflow makes several individually acceptable financial commitments:

```text
$4,000
$3,000
$4,000
```

Each action might pass when evaluated alone. The workflow has now accumulated
$11,000 of exposure.

Arclasp evaluates running workflow state and can pause a governed action when a
configured cumulative boundary is crossed. The application still chooses which
actions are placed behind governance and what metadata, such as `amount_usd`,
is sent for policy evaluation.

Keep your agent stack. Add governance around it.

## Quick Start

The smallest pattern is: initialize the SDK, open a chain, record the governed
action before the application executes the real side effect.

```python
import arclasp

arclasp.init(api_key="prail_...")

chain = arclasp.Chain("vendor-commitments")
chain.add_financial_threshold(usd=10_000, notify=["finance@example.com"])

async with chain:
    decision = await chain.record_agent_action(
        agent_name="procurement-agent",
        action_type="tool_call",
        action_name="create_purchase_order",
        payload={"amount_usd": 4_000, "vendor": "Acme Services"},
    )

    # The governed action is allowed here. If policy required human approval,
    # this line is reached only after the approval gate resolved.
    print(decision.decision_source)
```

If the backend denies the action, a human denies the approval, the approval
times out, or the organization kill switch is active, the SDK raises a typed
exception instead of silently continuing. If the governance backend is
unavailable after retries, governed execution raises `BackendUnavailableError`
and fails closed.

## Install Options

Framework integrations are optional extras. Install only what your application
uses.

```bash
pip install "arclasp[langchain]"
pip install "arclasp[langgraph]"
pip install "arclasp[crewai]"
pip install "arclasp[mcp]"
pip install "arclasp[all]"
```

The base SDK and all adapters except CrewAI support Python 3.10+. The CrewAI
extra is available on Python 3.10 through 3.13 because CrewAI's dependency graph
does not currently support Python 3.14.

## Control The Chain

Arclasp's primary job is enforcement.

Governed actions are recorded through the backend, evaluated against policy,
and allowed to continue only when the backend returns an allow decision or a
required human approval is granted.

Supported governance patterns include:

- cumulative financial thresholds
- per-action risk and policy boundaries
- blocking human approval gates
- tool-call governance through framework adapters
- cost and monthly budget controls
- time-boxed policy exceptions
- organization kill switch and chain auto-pause
- fail-closed handling for governed execution

The SDK does not expose a local allow fast path in this public release. The
backend is the governance authority.

## Understand Every Decision

Arclasp keeps chain context around the decision instead of treating each action
as an isolated request.

Depending on the workflow and configured policies, operators can inspect chain
context, agent actions, policy decisions, approvals, events, cumulative state,
and cost/governance state through supported SDK and dashboard paths.

## Prove What Happened

Generate tamper-evident, signed, hash-linked records that can be verified
later.

Verification paths vary by artifact and can include authenticated or scoped
public verification. Canonical top-level verification APIs are:

```python
await arclasp.verify_receipt_v2(receipt_id)
await arclasp.verify_approval_v2(approval_id)
await arclasp.verify_public_token("arv_...")
```

Evidence features are designed for integrity and transparency. They are not a
claim of legal certification, guaranteed compliance, universal offline
verification, or universal exactly-once execution.

See [SECURITY_AND_TRUST.md](SECURITY_AND_TRUST.md) for the security and trust
architecture.

## Human Approval

Policies can require human approval before a governed workflow continues. In
that case, `record_agent_action()` waits for the approval result. Approval lets
the workflow proceed; denial or timeout raises `ActionDeniedError` or
`ChainTimeoutError`.

Arclasp governs the actions you place behind its SDK calls or supported
adapters. It does not automatically intercept application side effects that
were never instrumented.

## Framework Integrations

### LangGraph

```python
import arclasp
from arclasp.langgraph import govern
from langchain_core.messages import HumanMessage

arclasp.init(api_key="prail_...")

governed = govern(compiled_graph, chain_name="research-workflow")
result = await governed.ainvoke({"messages": [HumanMessage(content="Summarize Q3 revenue")]})
```

### LangChain

```python
import arclasp
from arclasp.langchain import govern

arclasp.init(api_key="prail_...")

governed = govern(agent_executor, chain_name="support-agent")
result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
```

### CrewAI

```python
import arclasp
from arclasp.crewai import govern

arclasp.init(api_key="prail_...")

governed = govern(crew, chain_name="research-crew")
result = await governed.kickoff_async(inputs={"topic": "AI safety"})
```

CrewAI governance follows CrewAI's execution model. Use Python 3.10 through
3.13 for this extra.

### MCP

```python
import arclasp
from arclasp.mcp import ArclaspMcpAdapter

arclasp.init(api_key="prail_...")

async with arclasp.Chain("mcp-session") as chain:
    adapter = ArclaspMcpAdapter(chain=chain, agent_name="my-tools")

    async def handle_call_tool(name: str, arguments: dict):
        return await adapter.handle_tool_call(
            tool_name=name,
            arguments=arguments,
            handler=your_actual_handler,
        )
```

For custom agent loops, use `Chain` directly as shown in the quickstart.

## Who Arclasp Is For

Arclasp is built for agents that take consequential actions, such as agents
that can:

- spend money
- change production state
- communicate externally
- invoke privileged tools
- coordinate multi-step workflows

If an AI application only generates suggestions or drafts that a human always
reviews before anything happens, runtime governance may not be necessary yet.

## Policy Configuration

`add_financial_threshold()` is a convenience facade over the chain's
`policy_config` dictionary. The dictionary is sent when the chain is created
and merged by the backend over organization defaults.

```python
chain = arclasp.Chain("vendor-payouts")
chain.add_financial_threshold(usd=10_000, notify=["finance@example.com"])

same_chain = arclasp.Chain(
    "vendor-payouts",
    policy_config={
        "cumulative_financial_threshold_usd": 10_000,
        "notify": ["finance@example.com"],
    },
)
```

Recognized per-chain policy keys include:

| Key | Effect |
| --- | --- |
| `cumulative_financial_threshold_usd` | Overrides the cumulative spend threshold for this chain. |
| `financial_approval_threshold_usd` | Overrides the single-action financial approval threshold for this chain. |
| `cumulative_financial_threshold_action` | Uses `"pause_for_approval"` by default; `"deny"` hard-denies instead. |
| `notify` | Adds approver emails for this chain. |

## Alpha Status

Arclasp is currently in Alpha. The public API is intentionally small and the
release is designed for teams evaluating runtime governance in real agent
workflows. Expect active development before a stable 1.0 contract.

Install Arclasp and govern one workflow that can spend money, change production
state, or communicate externally.

## License

Apache 2.0. See [LICENSE](LICENSE).
