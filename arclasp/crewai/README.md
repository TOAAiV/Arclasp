# Arclasp CrewAI Adapter

This adapter wraps a CrewAI `Crew` so that supported task execution paths are
recorded as governed events in Arclasp. CrewAI does not expose a universal
pre-task interception point in current supported versions, so denial and approval
gates may surface after the task that triggered them has already completed.

## Contents

- [Installation](#installation)
- [Basic usage](#basic-usage)
- [How it works](#how-it-works)
- [Hierarchy in receipts](#hierarchy-in-receipts)
- [Policy enforcement semantics](#policy-enforcement-semantics)
- [Version compatibility](#version-compatibility)
- [Limitations and notes](#limitations-and-notes)
- [Example: deny halts execution](#example-deny-halts-execution)
- [Example: approval gate](#example-approval-gate)

---

## Installation

```bash
pip install "arclasp[crewai]"
```

---

## Basic usage

```python
import asyncio
import arclasp
from arclasp.crewai import govern

arclasp.init(api_key="prail_...")

governed = govern(crew, chain_name="research-crew")

async def main():
    # Drop-in replacement — same interface as the original crew:
    return await governed.kickoff_async(inputs={"topic": "AI safety"})

asyncio.run(main())
```

`govern()` accepts two optional parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `chain_name` | `"crewai_workflow"` | Name shown in the Arclasp dashboard for each run |
| `metadata` | `{}` | Extra key/value pairs attached to every chain (e.g. version, team) |

---

## How it works

Instrumentation is applied via two strategies, selected automatically at runtime:

**Strategy A — native callbacks** (preferred): wraps `crew.task_callback` to fire
`on_task_end` for every completed task. If `crew.before_task_callback` is also present,
it is wrapped to fire `on_task_start`.

**Strategy B — `execute_task` monkey-patch**: patches each `agent.execute_task` to
fire `on_task_start` before execution. Used when Strategy A has no start hook.

In CrewAI 1.x both strategies activate together: `task_callback` exists but
`before_task_callback` was removed, so Strategy A handles `task_end` and Strategy B
handles `task_start` (with its own `on_task_end` suppressed to avoid double-firing).
This is transparent to the caller — the correct event pair fires exactly once per task.

---

## Hierarchy in receipts

Each task appears in the audit trail with:

- `agent_name` = the task description (the governed action)
- `parent_agent_name` = the agent's `role` (the owner)

This preserves the Crew → Agent → Task hierarchy across Arclasp audit views,
consistent with the LangGraph and LangChain adapters.

---

## Policy enforcement semantics

| Decision | Behaviour |
|----------|-----------|
| `deny` | `ActionDeniedError` is raised after the governed task event is recorded; subsequent workflow stages halt |
| `require_approval` | The adapter may block after the task event is recorded until approved; `ChainTimeoutError` is raised if no decision arrives within timeout |

For strict pre-execution prevention, place `record_agent_action()` inside the tool or task implementation before the side effect runs.

---

## Version compatibility

Tested against **crewai 1.14.6** on Python 3.11. Supported range: see `pyproject.toml`; the current `crewai` extra is constrained to Python <3.14 because of CrewAI's upstream dependency support.

CrewAI 0.x included `before_task_callback`; CrewAI 1.x removed it. The adapter detects
which callbacks are present at runtime and activates the correct strategy combination —
no configuration required.

---

## Limitations and notes

- `GovernedCrew.kickoff()` (sync) raises `RuntimeError` if called from inside a running
  event loop. Use `kickoff_async()` for governed execution in async contexts.
- The default backend timeout is 5 s. If event POST latencies are slow (e.g. free-tier
  Supabase), set `backend_timeout_seconds=15` in `arclasp.init()`.
- Do not call `agent.execute_task` or set `crew.task_callback` directly while a governed
  run is in progress — the adapter manages these for the duration of the run and restores
  them in a `finally` block.

---

## Example: deny halts execution

```python
import asyncio
import arclasp
from arclasp.crewai import govern
from arclasp.exceptions import ActionDeniedError

arclasp.init(api_key="prail_...")
governed = govern(crew, chain_name="sensitive-ops")

async def main():
    try:
        result = await governed.kickoff_async(inputs={"task": "delete all records"})
    except ActionDeniedError as exc:
        print(f"Crew halted: {exc.policy_name} denied '{exc.action_name}'")
        # Receipt records the partial run and the denial decision.

asyncio.run(main())
```

The receipt captures all tasks that completed before the denial plus the policy
decision, so auditors can see exactly where and why the crew stopped.

---

## Example: approval gate

```python
import asyncio
import arclasp
from arclasp.crewai import govern
from arclasp.exceptions import ActionDeniedError, ChainTimeoutError

arclasp.init(api_key="prail_...")
governed = govern(crew, chain_name="finance-crew")

async def main():
    try:
        result = await governed.kickoff_async(inputs={"amount": 50000})
    except ChainTimeoutError:
        print("Approval not received within timeout — crew halted.")
    except ActionDeniedError as exc:
        print(f"Approval denied: {exc.policy_name}")

asyncio.run(main())
```

Approve or deny via the Arclasp dashboard. A denial raises `ActionDeniedError`
at the pending task; an approval resumes execution.

---

For implementation details — callback internals, `_fire()` threading model, and the
Pydantic v2 `object.__setattr__` patch — see the module docstring in
[`adapter.py`](adapter.py).
