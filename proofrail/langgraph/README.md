# ProofRail LangGraph Adapter

This adapter wraps a compiled LangGraph graph so that every node execution is
automatically recorded as a governed event in ProofRail.

## Contents

- [Installation](#installation)
- [Basic usage](#basic-usage)
- [Node naming affects policy enforcement](#node-naming-affects-policy-enforcement)
- [Technical internals](#technical-internals)

---

## Installation

```bash
pip install proofrail langgraph
```

---

## Basic usage

```python
import proofrail
from proofrail.langgraph import govern

proofrail.init(api_key="prail_...")

governed = govern(compiled_graph, chain_name="my-workflow")

# Drop-in replacement — same interface as the original graph:
result = await governed.ainvoke({"messages": [...]})
```

`govern()` accepts two optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `chain_name` | `"langgraph_workflow"` | Name shown in the ProofRail dashboard for each run |
| `metadata` | `{}` | Extra key/value pairs attached to every chain (e.g. version, team) |

---

## Node naming affects policy enforcement

ProofRail's LangGraph adapter uses your node names as the `action_name`
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

For details on how node execution is intercepted (Strategy A via
`astream_events`, Strategy B via `AsyncCallbackHandler`), how policy
exceptions propagate, and how `parent_agent_name` is populated for
nested graphs, see the module docstring in
[`adapter.py`](adapter.py).
