"""Punto de entrada FastAPI del microservicio conversacional SmartInventory AI."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from google.genai.errors import ClientError
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

from app.config import settings

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


# Locks por sessionId con timestamp de último uso (monotonic). La evicción perezosa en
# _get_session_lock evita fuga de memoria en sesiones de larga duración, sin afectar
# la garantía de exclusión mutua por sesión activa (nunca se elimina un lock locked()).
_session_locks: dict[str, tuple[asyncio.Lock, float]] = {}
_locks_guard = asyncio.Lock()


def _is_gemini_rate_limit_error(exc: BaseException) -> bool:
    """True si la cadena de excepciones incluye un 429 RESOURCE_EXHAUSTED del SDK google-genai."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ClientError):
            if current.code == 429 or current.status == "RESOURCE_EXHAUSTED":
                return True
        current = current.__cause__ or current.__context__
    return False


def _evict_stale_locks() -> None:
    """Elimina locks inactivos cuyo TTL expiró. Debe invocarse bajo _locks_guard."""

    now = time.monotonic()
    ttl = settings.SESSION_LOCK_TTL_SECONDS
    stale_session_ids = [
        session_id
        for session_id, (lock, last_used) in _session_locks.items()
        if not lock.locked() and (now - last_used) > ttl
    ]
    for session_id in stale_session_ids:
        del _session_locks[session_id]


async def _get_session_lock(session_id: str) -> asyncio.Lock:
    """Obtiene o crea el lock de una sesión protegiendo el dict contra condiciones de carrera."""

    async with _locks_guard:
        _evict_stale_locks()
        now = time.monotonic()
        if session_id not in _session_locks:
            lock = asyncio.Lock()
            _session_locks[session_id] = (lock, now)
        else:
            lock, _ = _session_locks[session_id]
            _session_locks[session_id] = (lock, now)
        return lock


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
    except (ClientError, ChatGoogleGenerativeAIError) as exc:
        if _is_gemini_rate_limit_error(exc):
            logger.warning(
                "Cuota/rate-limit Gemini (429 RESOURCE_EXHAUSTED) para session_id=%s",
                request.session_id,
            )
            # Mantener el contrato JSON intacto ante fallos transitorios conocidos, en vez de
            # forzar a .NET a improvisar un error genérico hacia React; comunica honestamente
            # sin alucinar stock ni ventas.
            return AgentChatResponse(
                response=(
                    "Estoy teniendo problemas técnicos temporales para procesar tu mensaje. "
                    "Por favor, intenta de nuevo en unos minutos."
                ),
                state="START",
                sale_origin="CHATBOT",
                invoice_number=None,
            )
        logger.exception(
            "Error de Gemini procesando chat para session_id=%s",
            request.session_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Error interno del agente conversacional",
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
