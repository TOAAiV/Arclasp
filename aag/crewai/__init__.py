"""
aag.crewai — CrewAI integration for the aag SDK.

Wraps a CrewAI Crew so that every task execution is automatically recorded
as a governed chain event.

Usage
-----
    import aag
    from aag.crewai import govern

    aag.init(api_key="aag_...")
    governed = govern(crew, chain_name="research-crew")
    result = await governed.kickoff_async(inputs={"topic": "AI safety"})
"""

from aag.crewai.adapter import govern

__all__ = ["govern"]
