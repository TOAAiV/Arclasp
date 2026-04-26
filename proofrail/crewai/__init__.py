"""
proofrail.crewai — CrewAI integration for the ProofRail SDK.

Wraps a CrewAI Crew so that every task execution is automatically recorded
as a governed chain event.

Usage
-----
    import proofrail
    from proofrail.crewai import govern

    proofrail.init(api_key="prail_...")
    governed = govern(crew, chain_name="research-crew")
    result = await governed.kickoff_async(inputs={"topic": "AI safety"})
"""

from proofrail.crewai.adapter import govern

__all__ = ["govern"]
