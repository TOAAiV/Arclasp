"""
aag.langgraph — LangGraph integration for the aag SDK.

Wraps a compiled LangGraph graph so that every node execution is
automatically recorded as a governed chain event.

Usage
-----
    import aag
    from aag.langgraph import govern

    aag.init(api_key="aag_...")
    governed = govern(compiled_graph, chain_name="my-workflow")
    result = await governed.ainvoke({"messages": [...]})
"""

from aag.langgraph.adapter import govern

__all__ = ["govern"]
