# Arclasp LangGraph Adapter

This adapter wraps a compiled LangGraph graph so the workflow runs inside one
Arclasp Chain and node-start evidence is recorded.

For nodes with consequential side effects, wrap the node callable with
`governed_node()` before compiling the graph. That wrapper performs Arclasp
policy enforcement before the customer's node body runs.

## Contents

- [Installation](#installation)
- [Basic usage](#basic-usage)
- [Node naming affects policy enforcement](#node-naming-affects-policy-enforcement)
- [Technical internals](#technical-internals)

---

## Installation

```bash
pip install arclasp langgraph
```

---

## Basic usage

```python
import arclasp
from arclasp.langgraph import govern, governed_node

arclasp.init(api_key="prail_...")

graph.add_node("send_payment", governed_node(send_payment, name="send_payment"))
governed = govern(compiled_graph, chain_name="my-workflow")

# Drop-in replacement — same interface as the original graph:
result = await governed.ainvoke({"messages": [...]})
```

`govern()` accepts two optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `chain_name` | `"langgraph_workflow"` | Name shown in the Arclasp dashboard for each run |
| `metadata` | `{}` | Extra key/value pairs attached to every chain (e.g. version, team) |

Use `governed_node()` for pre-execution authority:

```python
graph.add_node(
    "send_payment",
    governed_node(send_payment, name="send_payment"),
)
compiled_graph = graph.compile()
governed = govern(compiled_graph, chain_name="payment-workflow")
```

If policy requires approval, `governed_node()` waits for the approval result
before calling `send_payment`. Denial, timeout, or backend unavailability fails
closed and the node body is not executed.

`govern()` by itself is a Chain/lifecycle integration. It is useful for
tracking which nodes participated in a graph run, but LangGraph lifecycle
callbacks are observer events and are not a hard pre-execution boundary for
arbitrary node side effects. Wrap consequential nodes with `governed_node()`.

When `governed_node()` and `govern()` are used together, the governed node call
is the authority-bearing proposed action. Result/error lifecycle telemetry is
observed locally and is not submitted as a second governed action in this
release, so an already-approved operation is not re-gated merely because the
Chain's cumulative metrics remain above a threshold. A later consequential
node wrapped with `governed_node()` is still evaluated normally.

---

## Node naming affects policy enforcement

Arclasp's LangGraph adapter uses your node names as the `action_name`
field in policy events. The backend's risk classifier scans these names
for keywords that may trigger policy rules:

- Names containing `"email"`, `"send"`, `"message"`, `"post"` → communication
  category (+25 risk score, may trigger external communication approval)
- Names containing `"delete"`, `"remove"`, `"drop"` → destructive category
- Names containing `"schema"`, `"migration"`, `"alter"` → schema_change category
- Names containing `"commit"`, `"transfer"`, `"pay"` → may trigger financial policy
- Names containing `"key"`, `"secret"`, `"credential"` → may trigger credential
  exposure check

Choose node names with policy implications in mind. If you want a node
to be unambiguously policy-neutral, use a name that doesn't match these
patterns — for example, `"process_request"` instead of `"delete_old_records"`,
or `"send_notification"` only when you actually want the communication
policy to fire.

This behavior is intentional and helps catch policy-violating workflows
that name their nodes descriptively. The full list of keyword patterns
is in the policy engine source.

---

## Technical internals

For details on how lifecycle observation works (Strategy A via
`astream_events`, Strategy B via `AsyncCallbackHandler`), how policy
exceptions propagate, and how `parent_agent_name` is populated for nested
graphs, see the module docstring in
[`adapter.py`](adapter.py).

Lifecycle observation is not the same as pre-execution blocking. Use
`governed_node()` when a node body performs tool calls, writes, HTTP requests,
filesystem changes, or other consequential side effects.
