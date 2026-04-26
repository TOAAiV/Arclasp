"""
proofrail.langgraph — LangGraph integration for the ProofRail SDK.

Wraps a compiled LangGraph graph so that every node execution is
automatically recorded as a governed chain event.

Usage
-----
    import proofrail
    from proofrail.langgraph import govern

    proofrail.init(api_key="prail_...")
    governed = govern(compiled_graph, chain_name="my-workflow")
    result = await governed.ainvoke({"messages": [...]})
"""

from proofrail.langgraph.adapter import govern

__all__ = ["govern"]
