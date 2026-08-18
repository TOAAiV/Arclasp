"""
arclasp.langchain — LangChain integration for the Arclasp SDK.

Wraps a LangChain AgentExecutor or Runnable chain so that every tool call
and LLM invocation is automatically recorded as a governed chain event.

Usage
-----
    import arclasp
    from arclasp.langchain import govern

    arclasp.init(api_key="prail_...")
    governed = govern(agent_executor, chain_name="support-agent")
    result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
"""

from arclasp.langchain.adapter import govern

__all__ = ["govern"]
