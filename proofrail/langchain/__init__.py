"""
proofrail.langchain — LangChain integration for the ProofRail SDK.

Wraps a LangChain AgentExecutor or Runnable chain so that every tool call
and LLM invocation is automatically recorded as a governed chain event.

Usage
-----
    import proofrail
    from proofrail.langchain import govern

    proofrail.init(api_key="prail_...")
    governed = govern(agent_executor, chain_name="support-agent")
    result = await governed.ainvoke({"input": "Book a flight to Tokyo"})
"""

from proofrail.langchain.adapter import govern

__all__ = ["govern"]
