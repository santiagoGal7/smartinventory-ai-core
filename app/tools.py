import httpx
from langchain.tools import tool
from app.config import settings  # Donde guardarás la URL de .NET

@tool
async def buscar_producto_semantico(query: string) -> str:
    """Usa esta herramienta cuando el usuario pregunte por un producto, 
    sus características, precios o disponibilidad. Realiza una búsqueda semántica."""
    
    # En lugar de consultar la DB aquí, llamamos al endpoint interno de .NET
    url = f"{settings.NET_BACKEND_URL}/api/internal/products/semantic-search?query={query}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=5.0)
            if response.status_code == 200:
                return str(response.json())  # Le retornamos el contexto al LLM
            return "Error: No se pudo conectar con el servicio de inventario central."
        except httpx.RequestError:
            return "Error de comunicación con el backend de negocio."