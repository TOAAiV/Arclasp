"""Minimal Arclasp + CrewAI example.

Install the optional dependency with:

    pip install "arclasp[crewai]"

This file shows the current Arclasp wrapper for an existing CrewAI ``Crew``.
CrewAI model/provider configuration remains your application's responsibility.
Running the example requires ``ARCLASP_API_KEY`` and a CrewAI environment that
can execute the crew you define. Arclasp approval, denial, receipt, and
verification outcomes are backend-generated, not preset here.
"""

from __future__ import annotations

import asyncio
import os

import arclasp
from arclasp.crewai import govern


def _require_api_key() -> str:
    try:
        return os.environ["ARCLASP_API_KEY"]
    except KeyError as exc:
        raise SystemExit("Set ARCLASP_API_KEY before running this example.") from exc


def build_crew():
    try:
        from crewai import Agent, Crew, Task
    except ImportError as exc:
        raise SystemExit(
            'Install the CrewAI extra first: pip install "arclasp[crewai]"'
        ) from exc

    reviewer = Agent(
        role="vendor reviewer",
        goal="Prepare a short purchasing recommendation for human review.",
        backstory="You summarize vendor commitments before they are approved.",
        verbose=False,
    )
    task = Task(
        description="Summarize the proposed Example Vendor commitment for review.",
        expected_output="A concise recommendation with the amount and rationale.",
        agent=reviewer,
    )
    return Crew(agents=[reviewer], tasks=[task], verbose=False)


async def main() -> None:
    arclasp.init(api_key=_require_api_key())
    governed = govern(
        build_crew(),
        chain_name="example-crewai-vendor-review",
        metadata={"example": "crewai_minimal"},
    )
    result = await governed.kickoff_async(inputs={"vendor": "Example Vendor"})
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
