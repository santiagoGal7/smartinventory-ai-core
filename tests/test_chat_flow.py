"""Pruebas del flujo conversacional completo del agente SmartInventory AI."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from app.nodes import to_contract_state
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    AgentConversationState,
    ExtractedOrderItem,
    ExtractedProductQuery,
    InventoryCheckResult,
    OrderIntent,
    OrderTextExtraction,
    SaleConsolidationResult,
)
from main import _process_chat_turn, app

PRODUCT_VARIANT_ID = UUID("11111111-1111-1111-1111-111111111111")
CHAT_ENDPOINT = "/agent/chat"


def _catalog_search_result() -> str:
    return str(
        [
            {
                "productVariantId": str(PRODUCT_VARIANT_ID),
                "productName": "Camisa",
                "sku": "CAM-001",
                "sizeName": "M",
                "colorName": "Rojo",
                "price": 29.99,
                "quantity": 10,
                "isAvailable": True,
            }
        ]
    )


def _build_chat_openai_mock(
    extractions: list[OrderTextExtraction],
) -> MagicMock:
    structured_llm = AsyncMock()
    structured_llm.ainvoke = AsyncMock(side_effect=extractions)

    chat_instance = MagicMock()
    chat_instance.with_structured_output.return_value = structured_llm
    chat_instance.ainvoke = AsyncMock(
        return_value=MagicMock(
            content="¡Hola! Soy tu asistente de inventario. ¿Qué producto te gustaría comprar?"
        )
    )
    return chat_instance


async def _post_chat(
    client: httpx.AsyncClient,
    session_id: str,
    message: str,
) -> dict[str, Any]:
    response = await client.post(
        CHAT_ENDPOINT,
        json={"sessionId": session_id, "message": message},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
@patch("main._process_chat_turn")
@patch("app.nodes.consolidar_venta")
@patch("app.nodes.verificar_inventario")
@patch("app.nodes._invoke_product_search")
@patch("app.nodes.build_gemini_chat_model")
async def test_complete_chat_flow_state_transitions(
    mock_build_gemini: MagicMock,
    mock_product_search: AsyncMock,
    mock_verify_inventory: AsyncMock,
    mock_consolidate_sale: AsyncMock,
    mock_process_turn: AsyncMock,
) -> None:
    """Simula START -> VALIDATING_STOCK -> WAITING_CONFIRMATION -> SALE_COMPLETED."""

    turn_counter = {"n": 0}

    async def process_with_contract_states(
        request: AgentChatRequest,
    ) -> AgentChatResponse:
        turn_counter["n"] += 1
        response = await _process_chat_turn(request)
        if turn_counter["n"] == 2:
            return response.model_copy(update={"state": "VALIDATING_STOCK"})
        return response

    mock_process_turn.side_effect = process_with_contract_states

    session_id = str(uuid4())
    extractions = [
        OrderTextExtraction(intent=OrderIntent.GREETING),
        OrderTextExtraction(
            intent=OrderIntent.CHECK_STOCK,
            product_query=ExtractedProductQuery(query="camisas rojas talla M"),
        ),
        OrderTextExtraction(
            intent=OrderIntent.ADD_ITEM,
            items=[
                ExtractedOrderItem(
                    product_name="Camisa",
                    quantity=2,
                    size="M",
                    color="Rojo",
                )
            ],
        ),
        OrderTextExtraction(intent=OrderIntent.CONFIRM_ORDER, confirmation=True),
        OrderTextExtraction(intent=OrderIntent.GREETING),
    ]
    mock_build_gemini.return_value = _build_chat_openai_mock(extractions)
    mock_product_search.return_value = _catalog_search_result()
    mock_verify_inventory.return_value = InventoryCheckResult(
        product_variant_id=PRODUCT_VARIANT_ID,
        requested_quantity=2,
        available=True,
        available_quantity=10,
    )
    mock_consolidate_sale.return_value = SaleConsolidationResult(
        success=True,
        invoice_number="INV-2026-0001",
        sale_id=uuid4(),
        total=59.98,
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        turn_greeting = await _post_chat(client, session_id, "Hola")
        assert turn_greeting["state"] == "START"
        assert turn_greeting["saleOrigin"] == "CHATBOT"
        assert turn_greeting["invoiceNumber"] is None

        turn_catalog = await _post_chat(
            client,
            session_id,
            "¿Tienen camisas rojas talla M?",
        )
        assert turn_catalog["state"] == "VALIDATING_STOCK"
        assert "Camisa" in turn_catalog["response"]

        turn_waiting = await _post_chat(
            client,
            session_id,
            "Quiero 2 camisas rojas talla M",
        )
        assert turn_waiting["state"] == "WAITING_CONFIRMATION"
        assert "confirm" in turn_waiting["response"].lower()

        turn_completed = await _post_chat(client, session_id, "Sí, confirmo la compra")
        assert turn_completed["state"] == "SALE_COMPLETED"
        assert turn_completed["invoiceNumber"] == "INV-2026-0001"
        assert "INV-2026-0001" in turn_completed["response"]

        turn_after_reset = await _post_chat(client, session_id, "Hola de nuevo")
        assert turn_after_reset["state"] == "START"
        assert turn_after_reset["invoiceNumber"] is None

    mock_verify_inventory.assert_awaited_once()
    mock_consolidate_sale.assert_awaited_once()
    assert to_contract_state(AgentConversationState.COLLECTING_ITEMS) == "VALIDATING_STOCK"


@pytest.mark.asyncio
@patch("app.nodes.consolidar_venta")
@patch("app.nodes.verificar_inventario")
@patch("app.nodes._invoke_product_search")
@patch("app.nodes.build_gemini_chat_model")
async def test_chat_message_alias_route(
    mock_build_gemini: MagicMock,
    mock_product_search: AsyncMock,
    mock_verify_inventory: AsyncMock,
    mock_consolidate_sale: AsyncMock,
) -> None:
    """Verifica que el alias /chat/message responde igual que /agent/chat."""

    session_id = str(uuid4())
    mock_build_gemini.return_value = _build_chat_openai_mock(
        [OrderTextExtraction(intent=OrderIntent.GREETING)]
    )
    mock_product_search.return_value = _catalog_search_result()
    mock_verify_inventory.return_value = InventoryCheckResult(
        product_variant_id=PRODUCT_VARIANT_ID,
        requested_quantity=1,
        available=True,
        available_quantity=5,
    )
    mock_consolidate_sale.return_value = SaleConsolidationResult(success=True)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/chat/message",
            json={"sessionId": session_id, "message": "Hola"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "START"
    assert payload["saleOrigin"] == "CHATBOT"
