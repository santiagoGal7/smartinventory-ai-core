"""Grafo conversacional de SmartInventory AI.

Blinda el flujo del contrato del proyecto: el nodo del grafo bajo ninguna circunstancia
podrá transicionar hacia confirmación o venta sin una validación de stock exitosa previa
(enrutamiento centralizado en route_after_extraction).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.nodes import (
    confirmation_gate,
    extract_intent,
    general_response,
    resolve_and_validate_stock,
    route_after_extraction,
    search_product,
)
from app.schemas import AgentGraphState

workflow = StateGraph(AgentGraphState)

workflow.add_node("extract_intent", extract_intent)
workflow.add_node("search_product", search_product)
workflow.add_node("resolve_and_validate_stock", resolve_and_validate_stock)
workflow.add_node("confirmation_gate", confirmation_gate)
workflow.add_node("general_response", general_response)

workflow.add_edge(START, "extract_intent")

workflow.add_conditional_edges(
    "extract_intent",
    route_after_extraction,
    {
        "search_product": "search_product",
        "resolve_and_validate_stock": "resolve_and_validate_stock",
        "confirmation_gate": "confirmation_gate",
        "general_response": "general_response",
    },
)

workflow.add_edge("search_product", END)
workflow.add_edge("resolve_and_validate_stock", END)
workflow.add_edge("confirmation_gate", END)
workflow.add_edge("general_response", END)

# TODO: MemorySaver acumula checkpoints por thread_id sin expiración (misma fuga de memoria
# potencial que _session_locks). LangGraph no expone eviction por TTL en MemorySaver; evaluar
# migrar a un checkpointer con backend persistente (ej. SqliteSaver) si el proyecto crece
# más allá del entorno académico.
checkpointer = MemorySaver()
compiled_graph = workflow.compile(checkpointer=checkpointer)


def build_thread_config(session_id: str) -> dict[str, Any]:
    """Arma el config de LangGraph para aislar el checkpoint por sessionId (thread_id)."""

    return {"configurable": {"thread_id": session_id}}
