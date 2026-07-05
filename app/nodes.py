from __future__ import annotations

import ast
import asyncio
from typing import Any
from uuid import UUID

from app.config import settings
from app.llm import build_gemini_chat_model
from app.schemas import (
    AgentConversationState,
    AgentGraphState,
    CreateSalePayload,
    ExtractedOrderItem,
    InventoryCheckResult,
    OrderIntent,
    OrderTextExtraction,
    ResolvedSaleItem,
)
from app.tools import buscar_producto_semantico, consolidar_venta, verificar_inventario

_START_STATES = frozenset(
    {
        AgentConversationState.IDLE,
        AgentConversationState.GREETING,
        AgentConversationState.ERROR,
    }
)


def to_contract_state(internal_state: AgentConversationState) -> str:
    """Mapea el estado interno del grafo al contrato expuesto al backend .NET."""

    match internal_state:
        case (
            AgentConversationState.IDLE
            | AgentConversationState.GREETING
            | AgentConversationState.ERROR
        ):
            return "START"
        case (
            AgentConversationState.COLLECTING_ITEMS
            | AgentConversationState.COLLECTING_CUSTOMER
            | AgentConversationState.COLLECTING_ADDRESS
        ):
            return "VALIDATING_STOCK"
        case AgentConversationState.CONFIRMING_ORDER:
            return "WAITING_CONFIRMATION"
        case AgentConversationState.PROCESSING_SALE | AgentConversationState.COMPLETED:
            return "SALE_COMPLETED"
        case _:
            return "START"


def route_after_extraction(state: AgentGraphState) -> str:
    """Enrutamiento condicional posterior a la extracción de intención.

    Regla rígida del contrato: confirmation_gate y consolidate_sale solo son alcanzables
    después de que resolve_and_validate_stock haya puesto conversation_state en
    CONFIRMING_ORDER con resolved_items válidos. Este router nunca envía ADD_ITEM ni
    SEARCH_PRODUCT hacia confirmation_gate ni consolidate_sale.
    """

    extraction = state.last_extraction
    conversation_state = state.conversation_state

    if conversation_state == AgentConversationState.CONFIRMING_ORDER:
        return "confirmation_gate"

    if conversation_state in _START_STATES and extraction is not None:
        if extraction.intent == OrderIntent.ADD_ITEM:
            return "resolve_and_validate_stock"
        if extraction.intent in (OrderIntent.SEARCH_PRODUCT, OrderIntent.CHECK_STOCK):
            return "search_product"

    return "general_response"


def _is_tool_error(raw: str) -> bool:
    return raw.startswith("Error:")


def _parse_search_results(raw: str) -> list[dict[str, Any]] | None:
    if _is_tool_error(raw):
        return None

    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return None

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return None


def _build_item_search_query(item: ExtractedOrderItem) -> str:
    parts = [
        item.product_name,
        item.sku,
        item.size,
        item.color,
    ]
    query = " ".join(part.strip() for part in parts if part and part.strip())
    return query or "producto"


def _filter_item_matches(
    item: ExtractedOrderItem,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered = matches

    if item.sku:
        filtered = [
            match
            for match in filtered
            if str(match.get("sku", "")).lower() == item.sku.lower()
        ]

    if item.size:
        filtered = [
            match
            for match in filtered
            if item.size.lower() in str(match.get("sizeName", "")).lower()
        ]

    if item.color:
        filtered = [
            match
            for match in filtered
            if item.color.lower() in str(match.get("colorName", "")).lower()
        ]

    active_matches = [
        match
        for match in filtered
        if match.get("isAvailable", True) is not False
    ]
    return active_matches


def _format_product_option(match: dict[str, Any]) -> str:
    name = match.get("productName", "Producto")
    sku = match.get("sku", "N/A")
    size = match.get("sizeName", "N/A")
    color = match.get("colorName", "N/A")
    price = match.get("price")
    price_text = f", precio: ${price}" if price is not None else ""
    return f"- {name} (SKU: {sku}, talla: {size}, color: {color}{price_text})"


def _format_inventory_failure(result: InventoryCheckResult) -> str:
    if result.error_message:
        return result.error_message
    if result.available_quantity is None:
        return "no hay stock disponible"
    return (
        f"solo hay {result.available_quantity} unidad(es) disponible(s), "
        f"pero solicitaste {result.requested_quantity}"
    )


async def _invoke_product_search(query: str) -> str:
    tool_result = await buscar_producto_semantico.ainvoke({"query": query})
    if isinstance(tool_result, str):
        return tool_result
    return str(tool_result)


async def extract_intent(state: AgentGraphState) -> dict[str, Any]:
    """Extrae intención estructurada del mensaje entrante.

    Regla de Bloqueo en Espera: en CONFIRMING_ORDER el LLM solo evalúa confirmación
    o cancelación, ignorando productos u otras intenciones mencionadas en el mensaje.
    """

    system_prompt = (
        "Eres un asistente de extracción para un chatbot de inventario. "
        "Analiza el mensaje del usuario y devuelve la estructura solicitada. "
        f"Estado conversacional actual: {state.conversation_state.value}."
    )

    if state.conversation_state == AgentConversationState.CONFIRMING_ORDER:
        system_prompt += (
            " El usuario está confirmando una compra pendiente. "
            "Enfócate únicamente en detectar si confirma (sí) o cancela (no). "
            "Ignora cualquier producto, talla, color o cantidad mencionados. "
            "Usa confirmation=true para afirmación clara, confirmation=false para "
            "negación clara y confirmation=null si la respuesta es ambigua."
        )

    llm = build_gemini_chat_model(temperature=0).with_structured_output(
        OrderTextExtraction
    )

    extraction = await llm.ainvoke(
        [
            ("system", system_prompt),
            ("human", state.incoming_message),
        ]
    )

    if not isinstance(extraction, OrderTextExtraction):
        extraction = OrderTextExtraction(intent=OrderIntent.UNKNOWN)

    return {"last_extraction": extraction}


async def search_product(state: AgentGraphState) -> dict[str, Any]:
    """Busca productos en catálogo sin cambiar el estado conversacional (START).

    Prohibición Absoluta de Alucinación de Stock: si la tool falla, se informa
    el error y no se inventan productos ni disponibilidad.
    """

    extraction = state.last_extraction
    query = ""
    if extraction and extraction.product_query:
        query = extraction.product_query.query or extraction.product_query.product_name or ""
    if not query.strip():
        query = state.incoming_message

    raw_result = await _invoke_product_search(query.strip())
    if _is_tool_error(raw_result):
        return {
            "response_text": (
                "Lo siento, no pude consultar el catálogo en este momento. "
                "Por favor, intenta de nuevo en unos instantes."
            ),
        }

    matches = _parse_search_results(raw_result) or []
    if not matches:
        return {
            "response_text": (
                "No encontré productos que coincidan con tu búsqueda. "
                "¿Podrías intentar con otro nombre o referencia?"
            ),
        }

    options = "\n".join(_format_product_option(match) for match in matches[:3])
    suffix = (
        f"\n\nEncontré {len(matches)} opción(es) en total."
        if len(matches) > 3
        else ""
    )
    return {
        "response_text": (
            "Estas son las opciones que encontré:\n"
            f"{options}{suffix}\n\n"
            "Si deseas comprar alguna, indícame el producto con talla y color."
        ),
    }


async def resolve_and_validate_stock(state: AgentGraphState) -> dict[str, Any]:
    """Resuelve variantes y valida stock real antes de permitir confirmación.

    Regla rígida del contrato: solo este nodo puede llevar el flujo a
    CONFIRMING_ORDER. Sin stock verificado no hay confirmación ni venta.
    """

    extraction = state.last_extraction
    if extraction is None or not extraction.items:
        return {
            "conversation_state": AgentConversationState.IDLE,
            "response_text": (
                "No identifiqué un producto claro en tu mensaje. "
                "Cuéntame qué deseas comprar, incluyendo talla y color si aplica."
            ),
        }

    resolved_items: list[ResolvedSaleItem] = []

    for item in extraction.items:
        raw_result = await _invoke_product_search(_build_item_search_query(item))
        if _is_tool_error(raw_result):
            return {
                "conversation_state": AgentConversationState.IDLE,
                "response_text": (
                    "No pude consultar el catálogo para validar tu pedido. "
                    "Por favor, intenta de nuevo."
                ),
            }

        matches = _parse_search_results(raw_result) or []
        filtered_matches = _filter_item_matches(item, matches)

        if not filtered_matches:
            product_label = item.product_name or item.sku or "ese producto"
            return {
                "conversation_state": AgentConversationState.IDLE,
                "response_text": (
                    f"No encontré '{product_label}' en el catálogo. "
                    "Verifica el nombre e intenta de nuevo."
                ),
            }

        if len(filtered_matches) > 1:
            options = "\n".join(
                _format_product_option(match) for match in filtered_matches[:5]
            )
            metadata = dict(state.metadata)
            metadata["pending_search_options"] = filtered_matches[:5]
            return {
                "conversation_state": AgentConversationState.IDLE,
                "metadata": metadata,
                "response_text": (
                    "Encontré varias variantes que podrían coincidir. "
                    "Por favor, especifica la talla y el color:\n"
                    f"{options}"
                ),
            }

        match = filtered_matches[0]
        variant_id = match.get("productVariantId")
        if variant_id is None:
            return {
                "conversation_state": AgentConversationState.IDLE,
                "response_text": (
                    "Encontré el producto pero no pude identificar su variante. "
                    "Intenta ser más específico con talla y color."
                ),
            }

        resolved_items.append(
            ResolvedSaleItem(
                product_variant_id=UUID(str(variant_id)),
                quantity=item.quantity or 1,
            )
        )

    inventory_results = await asyncio.gather(
        *[
            verificar_inventario(
                product_variant_id=resolved_item.product_variant_id,
                requested_quantity=resolved_item.quantity,
            )
            for resolved_item in resolved_items
        ]
    )

    unavailable = [
        result for result in inventory_results if not result.available
    ]
    if unavailable:
        failure_lines = [
            f"- Variante {result.product_variant_id}: {_format_inventory_failure(result)}"
            for result in unavailable
        ]
        return {
            "conversation_state": AgentConversationState.IDLE,
            "pending_items": [],
            "resolved_items": [],
            "response_text": (
                "No puedo continuar con la compra porque el stock no alcanza "
                "o no pudo verificarse:\n"
                f"{chr(10).join(failure_lines)}\n\n"
                "Puedes intentar de nuevo con otra cantidad o producto."
            ),
        }

    summary_lines = [
        (
            f"- Variante {item.product_variant_id}: "
            f"{item.quantity} unidad(es)"
        )
        for item in resolved_items
    ]
    return {
        "conversation_state": AgentConversationState.CONFIRMING_ORDER,
        "resolved_items": resolved_items,
        "pending_items": [],
        "response_text": (
            "Resumen de tu pedido:\n"
            f"{chr(10).join(summary_lines)}\n\n"
            "¿Confirmas la compra? Responde sí o no."
        ),
    }


async def confirmation_gate(state: AgentGraphState) -> dict[str, Any]:
    """Puerta de confirmación exclusiva para CONFIRMING_ORDER.

    Regla de Bloqueo en Espera: mientras se espera confirmación, cualquier
    ambigüedad mantiene el estado y se re-pregunta sin procesar nuevos productos.
    """

    if state.conversation_state != AgentConversationState.CONFIRMING_ORDER:
        return {
            "response_text": (
                "No hay una compra pendiente de confirmar. "
                "Indícame qué producto deseas comprar."
            ),
        }

    extraction = state.last_extraction
    confirmation = extraction.confirmation if extraction else None

    if confirmation is True:
        return await consolidate_sale(state)

    if confirmation is False:
        return {
            "conversation_state": AgentConversationState.IDLE,
            "pending_items": [],
            "resolved_items": [],
            "response_text": (
                "Entendido, cancelé tu pedido. "
                "Si deseas comprar algo más, cuéntame desde el inicio."
            ),
        }

    return {
        "response_text": "¿Confirmas la compra? (sí/no)",
    }


async def consolidate_sale(state: AgentGraphState) -> dict[str, Any]:
    """Consolida la venta en el backend .NET.

    Cierre y Descarga de Memoria: al completar o fallar de forma segura, se limpia
    el carrito. Prohibición Absoluta de Alucinación de Stock: ante fallo se vuelve
    a START (IDLE) sin asumir que la venta se registró.
    """

    if not state.resolved_items:
        return {
            "conversation_state": AgentConversationState.IDLE,
            "pending_items": [],
            "resolved_items": [],
            "response_text": (
                "No hay productos validados para consolidar. "
                "Inicia un nuevo pedido indicando el producto deseado."
            ),
        }

    payload = CreateSalePayload(
        customer_id=None,
        sale_origin_id=2,
        items=state.resolved_items,
    )
    result = await consolidar_venta(payload)

    if result.success:
        invoice_number = result.invoice_number or "N/A"
        return {
            "conversation_state": AgentConversationState.COMPLETED,
            "invoice_number": invoice_number,
            "pending_items": [],
            "resolved_items": [],
            "response_text": (
                "¡Compra registrada con éxito! "
                f"Tu número de factura es: {invoice_number}."
            ),
        }

    error_message = result.error_message or "No se pudo completar la venta."
    return {
        "conversation_state": AgentConversationState.IDLE,
        "pending_items": [],
        "resolved_items": [],
        "response_text": (
            "Lo siento, tu compra no pudo completarse. "
            f"Motivo: {error_message}. "
            "Por favor, intenta de nuevo desde cero."
        ),
    }


async def general_response(state: AgentGraphState) -> dict[str, Any]:
    """Respuesta conversacional para saludos e intenciones no transaccionales.

    Permanece en START: no avanza el flujo hacia confirmación ni venta.
    """

    extraction = state.last_extraction
    intent = extraction.intent if extraction else OrderIntent.UNKNOWN

    llm = build_gemini_chat_model(temperature=0.3)

    system_prompt = (
        "Eres un asistente amable de una tienda de inventario. "
        "Responde en español de forma breve y útil. "
        f"Intención detectada: {intent.value}. "
        "Si el usuario saluda, responde cordialmente e invítalo a buscar o comprar productos. "
        "No inventes stock, precios ni productos."
    )

    response = await llm.ainvoke(
        [
            ("system", system_prompt),
            ("human", state.incoming_message),
        ]
    )
    content = response.content if hasattr(response, "content") else str(response)

    return {"response_text": str(content)}
