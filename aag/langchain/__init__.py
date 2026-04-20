"""
aag.langchain — LangChain integration for the aag SDK.

Wraps a LangChain AgentExecutor or Runnable chain so that every tool call
and LLM invocation is automatically recorded as a governed chain event.

Usage
-----
    import aag
    from aag.langchain import govern

    aag.init(api_key="aag_...")
    governed = govern(agent_executor, chain_name="support-agent")
    result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
"""

from aag.langchain.adapter import govern

__all__ = ["govern"]
