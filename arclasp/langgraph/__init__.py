"""
arclasp.langgraph — LangGraph integration for the Arclasp SDK.

Wraps a compiled LangGraph graph so that every node execution is
automatically recorded as a governed chain event.

Usage
-----
    import arclasp
    from arclasp.langgraph import govern

    arclasp.init(api_key="prail_...")
    governed = govern(compiled_graph, chain_name="my-workflow")
    result = await governed.ainvoke({"messages": [...]})
"""

from arclasp.langgraph.adapter import govern

__all__ = ["govern"]
