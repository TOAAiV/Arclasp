# Arclasp

## Govern the workflow, not just the API call.

**Runtime governance for AI-agent workflows that can take consequential actions.**

A safe action can be part of an unsafe workflow. Arclasp keeps state across an agent workflow and uses that accumulated context to decide whether the next governed action should be **allowed, flagged, paused for human approval, or denied**.

**Runtime governance means deciding whether consequential agent actions can proceed using the state of the workflow—not just the current call.** The decision authority lives outside the model: the Arclasp backend is authoritative, and governed execution fails closed when a required decision cannot safely be obtained.

**Keep your agent stack. Add governance around it.**

```bash
pip install arclasp
```

**Python 3.10+ · Public beta · Apache-2.0**

Website: https://arclasp.com
Documentation: https://docs.arclasp.com
Dashboard: https://app.arclasp.com

---

## A safe action can be part of an unsafe workflow

With a **$10,000 approval boundary** configured, a workflow can look like this:

```text
Agent action #1      $4,000   → allow
Agent action #2     +$3,000   → allow
Agent action #3     +$4,000   → require approval

Workflow exposure:  $11,000
Policy boundary:    $10,000
```

The third action is only $4,000. The workflow is not.

That is the problem Arclasp is built around. Traditional request-level controls naturally evaluate the action in front of them, but agent risk often emerges from what has already happened. Three purchases can cross a spending boundary, repeated refunds can accumulate material financial exposure, or a Critical registered agent can reach an action that requires human review even though the immediate tool call looks routine.

Arclasp represents the workflow as a **Chain**. Governed actions become durable **ChainEvents**, and the backend evaluates new actions against the state already accumulated in that Chain. The point is not to replace ordinary authorization or application logic. It is to add a runtime decision boundary for the part they often do not capture well: **what should happen next, given everything this workflow has already done?**

---

## Install

Install the framework-agnostic Python SDK:

```bash
pip install arclasp
```

Optional integrations are available for the agent stacks Arclasp currently supports:

```bash
pip install "arclasp[langchain]"
pip install "arclasp[langgraph]"
pip install "arclasp[crewai]"
pip install "arclasp[mcp]"
```

Or install all supported optional integrations:

```bash
pip install "arclasp[all]"
```

Arclasp is Python-first today. Your application, tools, orchestration framework, and external side effects remain yours; the hosted Arclasp service supplies the governance authority around the actions you choose to govern.

---

## Quick start

Create an organization API key in the Arclasp dashboard, initialize the SDK, and place the consequential part of your workflow inside a Chain.

```python
import os
import arclasp

arclasp.init(
    api_key=os.environ["ARCLASP_API_KEY"],
    backend_url="https://api.arclasp.com",
)

async with arclasp.Chain("vendor-purchase") as chain:
    decision = await chain.record_agent_action(
        agent_name="purchasing-agent",
        action_type="tool_call",
        action_name="record_vendor_commitment",
        payload={
            "vendor": "Northstar Components",
            "amount_usd": 4000,
        },
    )

    # Perform the real customer-side action only after
    # Arclasp permits the governed action.
```

The normal integration surface is deliberately small:

```python
arclasp.init(...)
arclasp.Chain(...)
chain.record_agent_action(...)
```

`record_agent_action(...)` submits the action to the backend-authoritative governance service. If policy allows the action, your application can continue. If policy requires human approval, the Chain enters the approval lifecycle and the SDK waits for that decision. If the action is denied, the approval is denied or times out, the organization kill switch blocks the action, the Chain has been auto-paused, or the governance backend cannot safely provide the required decision, the SDK raises instead of silently treating the operation as authorized.

Arclasp does **not** execute `record_vendor_commitment` for you. The customer application still performs the real tool call or external side effect after governance permits it.

> **Note:** `prail_` remains the current API-key prefix for compatibility.

---

## How Arclasp works

A Chain is the workflow-level unit of governance. It gives Arclasp a durable place to keep sequence, status, accumulated metrics, approval state, and completion evidence across multiple actions. That is what lets the second, fifth, or fiftieth governed action be evaluated with knowledge of earlier events instead of starting from zero every time.

When your application records an action, the SDK applies sanitization and redaction to supported event-payload paths before submitting the governed event to the Arclasp backend. The backend applies authentication and organization checks, evaluates policy against the action and the relevant Chain state, persists the resulting ChainEvent, and returns one of four decisions:

| Decision | What it means |
| --- | --- |
| `allow` | The governed action may continue. |
| `allow_with_flag` | The action may continue, while Arclasp records the flagged governance outcome. |
| `require_approval` | The Chain pauses at the governance boundary until an authorized human decision resolves. |
| `deny` | The governed action must not continue through the normal flow. |

A simplified lifecycle looks like this:

```text
Agent / application
        |
        v
   Arclasp SDK
        |
        v
      Chain
        |
        v
 Governed action
        |
        v
Arclasp governance backend
        |
        +---- allow -----------------> customer executes action
        |
        +---- allow_with_flag -------> customer executes action
        |
        +---- require_approval ------> Chain pauses -> human decision
        |
        +---- deny ------------------> action stops

After valid completion:

Chain -> Chain Record -> verification / scoped public verification
```

The model or agent does not authorize itself. Arclasp is the decision boundary around the governed action; your application remains the executor.

---

## Govern workflow state, not isolated calls

The reason Arclasp uses Chains is not merely to group logs. Chain state is part of the governance model.

### Cumulative financial exposure

Arclasp can track running financial exposure across a Chain using recorded values such as `amount_usd`, `amount`, or `value`. When the configured cumulative boundary is crossed, policy can require human approval or deny the triggering action, depending on the policy configuration.

This makes cumulative governance useful for workflows such as purchasing, refunds, credits, payments, discounts, and vendor commitments. The individual operation can stay below a threshold while the workflow as a whole exceeds it, and the event that crosses the boundary remains part of the Chain's evidence.

### Registered-agent risk

Organizations can register agents with ownership and risk-tier metadata, and that risk can participate in policy decisions. A registered Critical agent, for example, can require approval for governed actions.

The Agents Registry is a governance profile, not a process-health monitor. An enabled agent profile means its governance configuration participates in policy; it does not mean Arclasp is claiming that the underlying process is online or healthy.

### LLM cost-budget governance

Arclasp can govern **recorded LLM token-cost estimates** against organization-level monthly budget state. A crossing event can require approval, and unknown or unpriceable model-cost states can be treated conservatively rather than silently assuming a zero cost.

This is not provider billing. Arclasp does not directly cap an OpenAI, Anthropic, or other provider invoice. It governs the token-cost estimates recorded through Arclasp and uses those estimates as workflow and organization governance state.

### Category and hard-deny controls

Arclasp can also apply policy to categories of consequential actions. Current policy behavior includes approval gates or denies for conditions such as destructive production actions, credential exposure, IAM or permission changes, irreversible actions, external communication, high-risk scores, and other configured risk conditions.

The distinction between a hard deny and an approval gate is deliberate. A hard deny stops the governed action through the normal flow. `require_approval` creates a controlled path for a human to decide whether the workflow may continue.

---

## Start in shadow mode, then enforce

Introducing a governance service into a production execution path should not require guessing how a policy will behave.

Arclasp supports **shadow mode**. In shadow mode, policy is evaluated and the would-be governance result is recorded, but the workflow is allowed to continue. That gives teams a way to observe how a rule behaves against real workflow traffic before giving it blocking authority.

A practical rollout can therefore be gradual:

```text
Observe the workflow
        ↓
Run policy in shadow mode
        ↓
Compare would-be decisions
        ↓
Enforce one consequential boundary
        ↓
Expand governance deliberately
```

When the policy behavior is understood, the same boundary can move into **enforce** mode. Arclasp also supports **disabled** mode, where ordinary policy evaluation is skipped after organization-level kill-switch checks.

---

## Human approval is part of the workflow

Some decisions should not be fully autonomous. When policy requires review, Arclasp creates or reuses a pending Approval, moves the Chain into `pending_approval`, and holds the governed workflow at that boundary while the decision is unresolved.

Authorized reviewers can approve or deny through the authenticated dashboard and supported signed email decision flows. The approval lifecycle distinguishes `pending`, `approved`, `denied`, and `timed_out`. After approval, the SDK returns an allow-style outcome so the customer application can continue. After denial or timeout, the governed path raises rather than proceeding as though approval had been granted.

Approvals can also create scoped, temporary policy exceptions where that is appropriate. Current exception durations include `1h`, `1d`, `1w`, `1m`, and `single_use`. These grants are intentionally bounded: they do not override hard denies or the organization kill switch, and single-use grants are consumed rather than becoming permanent bypasses.

The approval decision is also tied to evidence. Arclasp signs an approval-certificate snapshot as part of the resolution path so a later reviewer can connect the human decision to the workflow and governance context that required it.

---

## Runtime safety beyond approval

Approval thresholds are only one part of the runtime control surface.

The **organization kill switch** denies new governed Arclasp actions for the organization before ordinary policy mode is considered. It is a backend governance control, not remote process control: it stops work that calls Arclasp for governance, but it cannot magically terminate arbitrary customer code that never crosses the Arclasp boundary.

Chains can also be **auto-paused** when configured runtime, event, or token-style safety limits are exceeded. That gives the system a way to stop governed progress when a workflow becomes unexpectedly long or behaves outside its configured operating envelope.

For backend-authoritative operations, availability is part of the safety model as well. If Arclasp cannot obtain the required governance decision after its configured retry behavior, the SDK raises `BackendUnavailableError` instead of converting uncertainty into permission.

That is the fail-closed boundary: backend uncertainty becomes an explicit error rather than implicit permission.

---

## Evidence that survives the request

A runtime decision becomes more valuable when the reasoning and evidence around it can still be inspected later.

When a valid Chain completes, Arclasp can produce a **signed, hash-linked, tamper-evident Chain Record with supported verification paths**. Approval decisions can produce signed approval-certificate evidence as well, keeping the human decision tied to the workflow and governance context that required it.

Authenticated verification can expose richer governance and evidence state to authorized organization members. Scoped public verification lets an administrator share a bounded verification view for eligible Chain Records or approval certificates without exposing unrestricted internal data. The dashboard also supports Chain Record verification and JSON/PDF downloads, with ZIP export for eligible verified records.

Arclasp deliberately calls this evidence **tamper-evident, not tamper-proof**. Supported verification can reveal when signed evidence no longer matches what was issued, but a Chain Record does not automatically prove legal admissibility, regulatory compliance, legal non-repudiation, or a real-world side effect that the customer application never instrumented.

The claim is deliberately narrower: Arclasp preserves verifiable governance evidence around the actions and decisions it was actually asked to govern.

---

## Keep your agent stack

Arclasp is not another orchestration framework. You can use the framework-agnostic Chain API directly or bring supported agent runtimes into the same governance model.

**Custom Python** gives you the most explicit integration: you decide which operations should cross the governance boundary and call `record_agent_action(...)` around them.

**LangChain** integration uses supported wrapper and callback paths so relevant agent and tool activity can participate in Arclasp governance, including propagation of governance exceptions instead of treating them as ordinary callback noise.

**LangGraph** integration uses event and callback surfaces to bring graph execution into Chains while preserving the surrounding LangGraph runtime.

**CrewAI** integration wraps supported crew and task execution paths so governed activity can be evaluated before the customer-side workflow continues where the adapter has a reliable interception point.

**MCP** integration uses `ArclaspMcpAdapter` to govern an MCP tool invocation before forwarding to the customer's real handler. This is an invocation boundary; it should not be read as universal automatic capture of every arbitrary downstream tool result, exception, or external side effect.

The framework remains your framework; Arclasp supplies the governance boundary.

---

## Operational visibility

The hosted dashboard gives teams a place to inspect and operate the governance layer rather than treating policy as a black box.

**Observe**
- Chains and event timelines
- policy outcomes and accumulated workflow state
- registered agents and risk tiers
- recorded LLM cost and budget state

**Intervene**
- pending and resolved approvals
- policy modes
- organization kill switch
- eligible governance/evidence-status workflows

**Review evidence**
- Chain Records
- approval certificates
- authenticated verification
- scoped public verification

**Administer**
- API keys
- audit activity
- team and settings
- onboarding

Chain detail ties the workflow together with status, involved agents, decision summaries, metadata, the event timeline, policy outcomes, approvals, and receipt relationships. The SDK also exposes read paths for Chain detail, event history, and the completed receipt, so governance state is not limited to the dashboard.

---

## A small public API

The supported top-level Python API is intentionally compact. Most integrations begin with three concepts:

```python
arclasp.init(...)
arclasp.Chain(...)
chain.record_agent_action(...)
```

The package also exposes supported verification helpers and typed governance exceptions for applications that need explicit handling of denial, backend unavailability, kill-switch decisions, auto-pause, approval timeout, Chain completion, or verification failures.

The detailed SDK reference, framework-specific APIs, and complete signatures belong in the documentation rather than in the README:

https://docs.arclasp.com

---

## Security model

Arclasp's security model is easier to understand through concrete boundaries than through adjectives.

**Backend-authoritative decisions.** Governed actions do not locally override a required backend policy decision.

**Fail-closed behavior.** Backend uncertainty becomes an explicit error rather than implicit permission.

**Organization isolation.** Authenticated operations and API keys are scoped to the relevant organization.

**Sanitized evidence.** The SDK applies pattern-based sanitization and redaction to supported event-payload and chain-metadata paths before submission. These controls are defense in depth, not a guarantee that every secret or sensitive value will be detected.

**Scoped approval and verification.** Approval decisions use authenticated or signed decision paths, while public verification uses scoped tokens and exposes a bounded view of governance evidence.

**Tamper-evident records.** Completed Chain Records can use signing and hash linkage with supported verification paths, while approval decisions can produce signed certificate evidence.

These controls protect the paths that actually go through Arclasp; they are not a claim of universal control over customer code outside the governance boundary.

For security issues, use the private reporting process in `SECURITY.md` or contact **security@arclasp.com**.

---

## Who Arclasp is for

Arclasp is most useful once an agent can do more than generate a suggestion.

That includes workflows that can spend or commit money, issue refunds or credits, write to databases or systems of record, change production infrastructure, communicate externally, modify access or permissions, trigger other agents, or otherwise create a consequence that deserves an explicit control point before execution continues.

It is also useful when teams are rebuilding the same governance machinery in multiple applications: cumulative limits, approval queues, exception rules, emergency shutdown controls, audit history, and evidence for later review. Arclasp gives those concerns a dedicated runtime layer without requiring the organization to replace its existing Python agent stack.

If an application only generates drafts or recommendations that a human already reviews before anything consequential happens, runtime governance may not be necessary yet. Arclasp becomes more valuable as agents receive real authority.

---

## What Arclasp is not

**It is not a tool executor.** The customer application still performs the real tool call or side effect after governance allows it.

**It is not passive-only observability.** Arclasp records evidence, but its primary job is runtime decision and enforcement for governed actions.

**It is not a provider billing system.** LLM cost governance is based on recorded token-cost estimates inside Arclasp, not direct control over provider invoices.

**It does not promise universal result capture.** Evidence coverage depends on the integration and what the customer application actually instruments. In particular, MCP governance covers the invocation boundary rather than every downstream external effect.

**It is not a mature enterprise IAM or GRC suite.** Organization roles and permissions exist, but mature custom RBAC, enterprise SSO/group mapping, and fine-grained enterprise IAM are not current public-beta capabilities.

**It is not a mature self-hosting product today.** The current customer model uses the hosted Arclasp governance backend.

**It is Python-first.** Non-Python SDKs are not current customer-ready capabilities.

Arclasp also does not claim universal distributed exactly-once execution. Its idempotency and transactional controls protect Arclasp evidence paths in scoped cases, but customer-side tools run outside Arclasp's direct execution control.

---

## Public beta

**Arclasp is currently in public beta.** The supported Python API is deliberately small, the hosted product is under active development, and prerelease versioning should be treated accordingly.

The public beta is for teams that want to evaluate runtime governance in real agent workflows now: start with one consequential workflow, observe it in shadow mode, put one meaningful boundary into enforcement, use human approval where autonomy should stop, and inspect the evidence that remains after completion.

**Give agents more autonomy without giving up control.**

```bash
pip install arclasp
```

---

## Documentation

Product documentation:

https://docs.arclasp.com

Product website:

https://arclasp.com

Hosted dashboard:

https://app.arclasp.com

---

## Contributing

Focused bug reports, reproducible integration issues, and well-scoped contributions are welcome. Please read [CONTRIBUTING.md](https://github.com/TOAAiV/Arclasp/blob/main/CONTRIBUTING.md) before opening a pull request.

---

## Security reporting

Please do not report security vulnerabilities through a public issue. Use the [security reporting process](https://github.com/TOAAiV/Arclasp/security/policy) or contact:

**security@arclasp.com**

---

## License

The public Arclasp Python SDK is licensed under the **Apache License 2.0**. The full license text is included in `LICENSE`.
