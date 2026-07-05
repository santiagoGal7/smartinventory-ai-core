from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentConversationState(StrEnum):
    """Estados del flujo conversacional del agente."""

    IDLE = "idle"
    GREETING = "greeting"
    COLLECTING_ITEMS = "collecting_items"
    CONFIRMING_ORDER = "confirming_order"
    COLLECTING_CUSTOMER = "collecting_customer"
    COLLECTING_ADDRESS = "collecting_address"
    PROCESSING_SALE = "processing_sale"
    COMPLETED = "completed"
    ERROR = "error"


class SaleOriginName(StrEnum):
    """Orígenes de venta definidos en el backend .NET (SaleOrigins)."""

    MANUAL = "Manual"
    CHATBOT = "Chatbot"


class AgentChatRequest(BaseModel):
    """Cuerpo de POST /agent/chat."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: Annotated[str, Field(alias="sessionId", min_length=1)]
    message: Annotated[str, Field(min_length=1)]


class AgentChatResponse(BaseModel):
    """Respuesta de POST /agent/chat."""

    model_config = ConfigDict(populate_by_name=True)

    response: str
    state: AgentConversationState | str
    sale_origin: Annotated[SaleOriginName | str | None, Field(alias="saleOrigin")] = None
    invoice_number: Annotated[str | None, Field(alias="invoiceNumber")] = None


class ExtractedAddress(BaseModel):
    """Dirección de entrega extraída del texto natural del usuario."""

    street: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    department: str | None = None
    postal_code: Annotated[str | None, Field(alias="postalCode")] = None
    reference: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ExtractedOrderItem(BaseModel):
    """Línea de producto extraída antes de resolver la variante en el catálogo."""

    product_name: Annotated[str | None, Field(alias="productName")] = None
    quantity: int | None = Field(default=None, ge=1)
    size: str | None = None
    color: str | None = None
    sku: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ExtractedCustomerInfo(BaseModel):
    """Datos del cliente mencionados de forma conversacional."""

    full_name: Annotated[str | None, Field(alias="fullName")] = None
    document_number: Annotated[str | None, Field(alias="documentNumber")] = None
    email: str | None = None
    phone: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ExtractedProductQuery(BaseModel):
    """Consulta de catálogo o inventario inferida del mensaje."""

    query: str | None = None
    product_name: Annotated[str | None, Field(alias="productName")] = None
    size: str | None = None
    color: str | None = None
    sku: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ExtractedQuantityUpdate(BaseModel):
    """Cantidad detectada cuando el usuario responde solo con un número o expresión numérica."""

    quantity: int = Field(ge=1)
    applies_to_last_item: Annotated[bool, Field(alias="appliesToLastItem")] = True

    model_config = ConfigDict(populate_by_name=True)


class OrderIntent(StrEnum):
    """Intención principal detectada en el mensaje."""

    GREETING = "greeting"
    SEARCH_PRODUCT = "search_product"
    ADD_ITEM = "add_item"
    UPDATE_QUANTITY = "update_quantity"
    REMOVE_ITEM = "remove_item"
    PROVIDE_CUSTOMER = "provide_customer"
    PROVIDE_ADDRESS = "provide_address"
    CONFIRM_ORDER = "confirm_order"
    CANCEL_ORDER = "cancel_order"
    CHECK_STOCK = "check_stock"
    UNKNOWN = "unknown"


class OrderTextExtraction(BaseModel):
    """Extracción estructurada intermedia usada por el LLM para procesar la orden."""

    intent: OrderIntent = OrderIntent.UNKNOWN
    items: list[ExtractedOrderItem] = Field(default_factory=list)
    quantity_update: Annotated[ExtractedQuantityUpdate | None, Field(alias="quantityUpdate")] = None
    customer: ExtractedCustomerInfo | None = None
    address: ExtractedAddress | None = None
    product_query: Annotated[ExtractedProductQuery | None, Field(alias="productQuery")] = None
    confirmation: bool | None = None
    notes: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ResolvedSaleItem(BaseModel):
    """Ítem listo para enviar al backend .NET (CreateSaleItemRequest)."""

    product_variant_id: Annotated[UUID, Field(alias="productVariantId")]
    quantity: int = Field(ge=1)

    model_config = ConfigDict(populate_by_name=True)


class CreateSalePayload(BaseModel):
    """Payload alineado con CreateSaleRequest del backend .NET."""

    customer_id: Annotated[UUID | None, Field(alias="customerId")] = None
    sale_origin_id: Annotated[int, Field(alias="saleOriginId")] = 2
    items: list[ResolvedSaleItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("items")
    @classmethod
    def validate_items_not_empty(cls, value: list[ResolvedSaleItem]) -> list[ResolvedSaleItem]:
        if not value:
            raise ValueError("La venta debe tener al menos un producto.")
        return value


class AgentGraphState(BaseModel):
    """Estado persistido del grafo LangGraph indexado por sessionId."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: Annotated[str, Field(alias="sessionId")]
    conversation_state: Annotated[
        AgentConversationState,
        Field(alias="conversationState"),
    ] = AgentConversationState.IDLE
    pending_items: Annotated[
        list[ExtractedOrderItem],
        Field(alias="pendingItems"),
    ] = Field(default_factory=list)
    resolved_items: Annotated[
        list[ResolvedSaleItem],
        Field(alias="resolvedItems"),
    ] = Field(default_factory=list)
    customer: ExtractedCustomerInfo | None = None
    address: ExtractedAddress | None = None
    sale_origin: Annotated[SaleOriginName, Field(alias="saleOrigin")] = SaleOriginName.CHATBOT
    invoice_number: Annotated[str | None, Field(alias="invoiceNumber")] = None
    last_extraction: Annotated[OrderTextExtraction | None, Field(alias="lastExtraction")] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
