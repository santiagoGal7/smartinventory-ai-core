from uuid import UUID

import httpx
from langchain.tools import tool

from app.config import settings
from app.schemas import CreateSalePayload, InventoryCheckResult, SaleConsolidationResult


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


async def verificar_inventario(
    product_variant_id: UUID,
    requested_quantity: int,
) -> InventoryCheckResult:
    """Consulta el stock real de una variante en el backend de negocio."""

    inventory_url = (
        f"{settings.NET_BACKEND_URL}/api/inventory/variant/{product_variant_id}"
    )
    base_result = InventoryCheckResult(
        product_variant_id=product_variant_id,
        requested_quantity=requested_quantity,
        available=False,
    )

    async with httpx.AsyncClient(timeout=settings.NET_BACKEND_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(inventory_url)
            if response.status_code == 200:
                data = response.json()
                quantity = data.get("quantity")
                if quantity is None:
                    return base_result.model_copy(
                        update={
                            "error_message": (
                                "Respuesta de inventario inválida del backend de negocio."
                            ),
                        }
                    )
                available_quantity = int(quantity)
                if available_quantity >= requested_quantity:
                    return base_result.model_copy(
                        update={
                            "available": True,
                            "available_quantity": available_quantity,
                        }
                    )
                return base_result.model_copy(
                    update={"available_quantity": available_quantity}
                )
            if response.status_code == 404:
                return base_result.model_copy(
                    update={
                        "error_message": "Producto no encontrado en inventario.",
                    }
                )
            return base_result.model_copy(
                update={
                    "error_message": (
                        f"Error: el backend de inventario respondió con código "
                        f"{response.status_code}. No se pudo verificar stock."
                    ),
                }
            )
        except httpx.TimeoutException:
            return base_result.model_copy(
                update={
                    "error_message": (
                        "Error: tiempo de espera agotado al comunicarse con el backend "
                        "de negocio. No se pudo verificar stock."
                    ),
                }
            )
        except httpx.RequestError:
            return base_result.model_copy(
                update={
                    "error_message": (
                        "Error de comunicación con el backend de negocio. "
                        "No se pudo verificar stock."
                    ),
                }
            )


async def consolidar_venta(payload: CreateSalePayload) -> SaleConsolidationResult:
    """Registra la venta consolidada en el backend de negocio."""

    sales_url = f"{settings.NET_BACKEND_URL}/api/sales"
    failure = SaleConsolidationResult(success=False)

    async with httpx.AsyncClient(timeout=settings.NET_BACKEND_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                sales_url,
                json=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            if response.status_code == 201:
                data = response.json()
                sale_id = data.get("id")
                return SaleConsolidationResult(
                    success=True,
                    invoice_number=data.get("invoiceNumber"),
                    sale_id=UUID(sale_id) if sale_id is not None else None,
                    total=data.get("total"),
                )
            if response.status_code == 400:
                data = response.json()
                message = data.get("message", "Error de regla de negocio al crear la venta.")
                return failure.model_copy(update={"error_message": message})
            return failure.model_copy(
                update={
                    "error_message": (
                        f"Error: el backend respondió con código {response.status_code}. "
                        "No se pudo consolidar la venta."
                    ),
                }
            )
        except httpx.TimeoutException:
            return failure.model_copy(
                update={
                    "error_message": (
                        "Error: tiempo de espera agotado al comunicarse con el backend "
                        "de negocio. No se pudo consolidar la venta."
                    ),
                }
            )
        except httpx.RequestError:
            return failure.model_copy(
                update={
                    "error_message": (
                        "Error de comunicación con el backend de negocio. "
                        "No se pudo consolidar la venta."
                    ),
                }
            )


verificar_inventario_tool = tool(verificar_inventario)
consolidar_venta_tool = tool(consolidar_venta)
