"""
arclasp.crewai — CrewAI integration for the Arclasp SDK.

Wraps a CrewAI Crew so that every task execution is automatically recorded
as a governed chain event.

Usage
-----
    import arclasp
    from arclasp.crewai import govern

    arclasp.init(api_key="prail_...")
    governed = govern(crew, chain_name="research-crew")
    result = await governed.kickoff_async(inputs={"topic": "AI safety"})
"""

from arclasp.crewai.adapter import govern

__all__ = ["govern"]
