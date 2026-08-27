"""
arclasp.langgraph — LangGraph integration for the Arclasp SDK.

Wraps a compiled LangGraph graph in one Arclasp Chain. Use ``governed_node()``
for node bodies that need pre-execution policy enforcement.

Usage
-----
    import arclasp
    from arclasp.langgraph import govern, governed_node

    arclasp.init(api_key="prail_...")
    graph.add_node("send_payment", governed_node(send_payment, name="send_payment"))
    governed = govern(compiled_graph, chain_name="my-workflow")
    result = await governed.ainvoke({"messages": [...]})
"""

from arclasp.langgraph.adapter import govern
from arclasp.langgraph.nodes import governed_node

__all__ = ["govern", "governed_node"]
