import httpx
from langchain.tools import tool

from app.config import settings


@tool
async def buscar_producto_semantico(query: str) -> str:
    """Usa esta herramienta cuando el usuario pregunte por un producto,
    sus características, precios o disponibilidad. Consulta el catálogo del backend de negocio."""

    search_url = f"{settings.NET_BACKEND_URL}/api/products/variants/search"
    params = {"query": query, "onlyAvailable": True}

    async with httpx.AsyncClient(timeout=settings.NET_BACKEND_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(search_url, params=params)
            if response.status_code == 200:
                return str(response.json())
            return (
                f"Error: el backend de inventario respondió con código {response.status_code}. "
                "No hay datos de stock ni catálogo disponibles en este momento."
            )
        except httpx.TimeoutException:
            return (
                "Error: tiempo de espera agotado al comunicarse con el backend de negocio. "
                "No se pudo verificar stock ni productos."
            )
        except httpx.RequestError:
            return (
                "Error de comunicación con el backend de negocio. "
                "No se pudo consultar el catálogo ni la disponibilidad."
            )
