"""Minimal Arclasp + LangGraph example using real graph primitives.

Install the optional dependency with:

    pip install "arclasp[langgraph]"

The consequential node is wrapped with ``governed_node(...)`` before the graph
is compiled, so Arclasp records and resolves governance before that node body
runs. Running this example contacts the configured Arclasp backend; approval,
denial, receipt, and verification outcomes come from Arclasp, not this file.
"""

from __future__ import annotations

import asyncio
import os
from typing import TypedDict

import arclasp
from arclasp.langgraph import govern, governed_node


class VendorState(TypedDict, total=False):
    vendor: str
    amount_usd: float
    ready_to_commit: bool


def _require_api_key() -> str:
    try:
        return os.environ["ARCLASP_API_KEY"]
    except KeyError as exc:
        raise SystemExit("Set ARCLASP_API_KEY before running this example.") from exc


def _payload(state: VendorState) -> dict:
    return {
        "vendor": state.get("vendor"),
        "amount_usd": state.get("amount_usd"),
        "operation": "prepare_vendor_commitment",
    }


def prepare_commitment(state: VendorState) -> VendorState:
    return {"ready_to_commit": True}


def build_graph():
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise SystemExit(
            'Install the LangGraph extra first: pip install "arclasp[langgraph]"'
        ) from exc

    graph = StateGraph(VendorState)
    graph.add_node(
        "prepare_commitment",
        governed_node(
            prepare_commitment,
            name="prepare_commitment",
            action_name="prepare_vendor_commitment",
            payload=_payload,
        ),
    )
    graph.set_entry_point("prepare_commitment")
    graph.add_edge("prepare_commitment", END)
    return graph.compile()


async def main() -> None:
    arclasp.init(api_key=_require_api_key())
    governed = govern(
        build_graph(),
        chain_name="example-langgraph-vendor-review",
        metadata={"example": "langgraph_minimal"},
    )
    result = await governed.ainvoke({"vendor": "Example Vendor", "amount_usd": 2500})
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
