"""Punto de entrada FastAPI del microservicio conversacional SmartInventory AI."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from app.graph import build_thread_config, compiled_graph
from app.nodes import to_contract_state
from app.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    AgentConversationState,
    AgentGraphState,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SmartInventory AI - Chatbot Service")


@app.get("/")
async def root() -> dict[str, str]:
    """Health check para verificar que el microservicio está activo."""

    return {
        "service": "SmartInventory AI - Chatbot Service",
        "status": "ok",
    }


_session_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    """Obtiene o crea el lock de una sesión protegiendo el dict contra condiciones de carrera."""

    async with _locks_guard:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
        return _session_locks[session_id]


async def _process_chat_turn(request: AgentChatRequest) -> AgentChatResponse:
    """Ejecuta un turno conversacional aislado por sessionId."""

    session_lock = await _get_session_lock(request.session_id)
    async with session_lock:
        config = build_thread_config(request.session_id)
        graph_input: dict[str, Any] = {
            "session_id": request.session_id,
            "incoming_message": request.message,
        }

        final_state_raw = await compiled_graph.ainvoke(graph_input, config=config)
        final_state = AgentGraphState.model_validate(final_state_raw)

        contract_state = to_contract_state(final_state.conversation_state)
        invoice_number: str | None = None

        if final_state.conversation_state == AgentConversationState.COMPLETED:
            invoice_number = final_state.invoice_number

            # Reinicio de ciclo del contrato: el checkpoint debe volver a START para la
            # siguiente interacción, pero la respuesta de ESTE turno conserva
            # SALE_COMPLETED e invoiceNumber capturados antes del reset.
            await compiled_graph.aupdate_state(
                config,
                {
                    "conversation_state": AgentConversationState.IDLE,
                    "invoice_number": None,
                    "resolved_items": [],
                    "pending_items": [],
                },
            )

        return AgentChatResponse(
            response=final_state.response_text,
            state=contract_state,
            sale_origin="CHATBOT",
            invoice_number=invoice_number,
        )


async def _handle_chat_request(request: AgentChatRequest) -> AgentChatResponse:
    """Envuelve el procesamiento del turno con manejo explícito de errores HTTP."""

    try:
        return await _process_chat_turn(request)
    except (httpx.TimeoutException, httpx.ConnectError):
        raise HTTPException(
            status_code=503,
            detail="Servicio de negocio no disponible",
        ) from None
    except Exception:
        logger.exception(
            "Error interno procesando chat para session_id=%s",
            request.session_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Error interno del agente conversacional",
        ) from None


@app.post("/agent/chat", response_model=AgentChatResponse, response_model_by_alias=True)
async def agent_chat(request: AgentChatRequest) -> AgentChatResponse:
    """Ruta oficial del contrato de Repo 2."""

    return await _handle_chat_request(request)


@app.post("/chat/message", response_model=AgentChatResponse, response_model_by_alias=True)
async def chat_message_alias(request: AgentChatRequest) -> AgentChatResponse:
    """Alias temporal para ChatService.cs de Dev1.

    ELIMINAR cuando el equipo confirme cuál ruta es la canónica.
    """

    return await _handle_chat_request(request)
