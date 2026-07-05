"""Factory del modelo conversacional Google Gemini con herramientas del backend .NET."""

from __future__ import annotations

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.runnables import Runnable

from app.config import settings
from app.tools import (
    buscar_producto_semantico,
    consolidar_venta_tool,
    verificar_inventario_tool,
)

CHAT_TOOLS = [
    buscar_producto_semantico,
    verificar_inventario_tool,
    consolidar_venta_tool,
]


def build_gemini_chat_model(*, temperature: float = 0) -> ChatGoogleGenerativeAI:
    """Instancia ChatGoogleGenerativeAI usando Google AI Studio (capa gratuita)."""

    return ChatGoogleGenerativeAI(
        model=settings.GOOGLE_MODEL,
        temperature=temperature,
        google_api_key=settings.GOOGLE_API_KEY,
    )


def build_gemini_chat_model_with_tools(*, temperature: float = 0) -> Runnable:
    """Gemini con Tools de httpx enlazadas para consultar catálogo, stock y ventas .NET."""

    return build_gemini_chat_model(temperature=temperature).bind_tools(CHAT_TOOLS)
