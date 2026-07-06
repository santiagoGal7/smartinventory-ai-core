# SmartInventory AI — (Chatbot Service)

Microservicio conversacional del proyecto SmartInventory AI. Recibe mensajes de chat, interpreta intención con Gemini, consulta catálogo e inventario en el backend .NET y, si el flujo lo permite, registra ventas allí.

**Este repo no es** el backend de negocio (ProjectNetIa / .NET) ni el frontend React. Es la capa intermedia de IA.

**Quién lo consume:** el `ChatService.cs` del backend .NET, que reenvía mensajes del frontend y espera la respuesta estructurada (`response`, `state`, `invoiceNumber`, `saleOrigin`). Ese backend corre por defecto en `http://localhost:5083`.

---

## Stack

Lo que está en `requirements.txt` y se usa en código:

| Componente | Uso |
|------------|-----|
| Python 3.11+ | Runtime (tipado estricto en todo el repo) |
| FastAPI + Uvicorn | API HTTP (`main.py`) |
| Pydantic v2 + pydantic-settings | Contratos HTTP y config (`app/schemas.py`, `app/config.py`) |
| LangChain | LLM estructurado (`OrderTextExtraction`) y tools |
| LangGraph | Grafo de estados (`app/graph.py`) |
| langchain-google-genai | `ChatGoogleGenerativeAI` contra Google AI Studio |
| httpx | Llamadas async al backend .NET (`app/tools.py`) |
| pytest + pytest-asyncio | Tests en `tests/` |

Modelo por defecto: `gemini-2.5-flash` (`GOOGLE_MODEL` en `.env`, fallback en `app/config.py`).

---

## Arquitectura del flujo conversacional

Cada turno de chat ejecuta el grafo LangGraph compilado en `app/graph.py`. El estado se persiste en memoria con `MemorySaver`, indexado por `sessionId` (`thread_id` en `build_thread_config`).

En `main.py`, cada `sessionId` tiene un `asyncio.Lock` para serializar turnos concurrentes de la misma sesión.

### Nodos del grafo

```
START → extract_intent → [router] → search_product | resolve_and_validate_stock | confirmation_gate | general_response → END
```

| Nodo | Qué hace |
|------|----------|
| `extract_intent` | Gemini extrae intención estructurada (`OrderTextExtraction`). En `CONFIRMING_ORDER` solo evalúa confirmación/cancelación. |
| `search_product` | Busca en catálogo vía .NET. No cambia el estado interno. |
| `resolve_and_validate_stock` | Resuelve variante (nombre/SKU + filtros `size`/`color`), verifica stock real. Solo este nodo puede pasar a `CONFIRMING_ORDER`. |
| `confirmation_gate` | En espera de confirmación: procesa sí/no/ambiguo. Si confirma, llama a `consolidate_sale` (no es nodo separado del grafo). |
| `general_response` | Saludos y mensajes no transaccionales. No avanza el flujo de venta. |

El enrutamiento post-extracción está en `route_after_extraction` (`app/nodes.py`): nunca envía búsquedas o ítems nuevos a `confirmation_gate` si no hay una compra pendiente validada.

### Estado interno vs contrato externo

El grafo usa estados internos (`AgentConversationState` en `app/schemas.py`). La API expone cuatro valores fijos mapeados por `to_contract_state()`:

| Interno | Contrato HTTP |
|---------|---------------|
| `idle`, `greeting`, `error` | `START` |
| `collecting_items`, `collecting_customer`, `collecting_address` | `VALIDATING_STOCK` |
| `confirming_order` | `WAITING_CONFIRMATION` |
| `processing_sale`, `completed` | `SALE_COMPLETED` |

En el flujo actual verificado, los estados que aparecen en producción son principalmente `START` → `WAITING_CONFIRMATION` → `SALE_COMPLETED` → `START`. `VALIDATING_STOCK` está reservado en el contrato para estados de recolección intermedios; el test unitario lo simula en un turno de catálogo.

Tras una venta completada, `main.py` resetea el checkpoint a `idle` para el siguiente turno, pero la respuesta HTTP de ese turno sigue devolviendo `SALE_COMPLETED` e `invoiceNumber`.

---

## Levantarlo localmente

### 1. Prerrequisitos

- Python 3.11+
- Backend .NET de SmartInventory corriendo en `http://localhost:5083` (PostgreSQL operativo, catálogo con productos)
- API key de Google AI Studio (Gemini)

### 2. Variables de entorno

Crear `.env` en la raíz del repo:

```env
GOOGLE_API_KEY=tu-api-key-de-google-ai-studio
GOOGLE_MODEL=gemini-2.5-flash
NET_BACKEND_URL=http://localhost:5083
NET_BACKEND_TIMEOUT_SECONDS=5.0
```

`GOOGLE_API_KEY` es obligatoria. Las demás tienen default en `app/config.py`.

**Importante:** `get_settings()` usa `@lru_cache`. Si cambias `.env`, hay que reiniciar uvicorn para que tome los nuevos valores.

### 3. Instalar dependencias

```powershell
cd smartinventory-ai-core
pip install -r requirements.txt
```

### 4. Arrancar el servicio

```powershell
uvicorn main:app --host 127.0.0.1 --port 8000
```

Verificar:

```powershell
curl http://127.0.0.1:8000/
```

El script `run_all.ps1` levanta .NET y FastAPI en ventanas separadas (con `--reload` en uvicorn). Ajusta la ruta de ProjectNetIa si tu clone está en otra ubicación.

---

## PUERTO 8000 — NO CAMBIAR

El backend .NET del equipo tiene hardcodeado en su `Program.cs`:

```csharp
client.BaseAddress = new Uri("http://localhost:8000");
```

Este microservicio **debe** escuchar en el puerto **8000**. No hay variable de entorno en este repo para cambiarlo del lado del consumidor .NET.

Si el puerto está ocupado, libéralo antes de arrancar:

```powershell
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

No uses otro puerto esperando que .NET lo detecte: no lo hará.

---

## Endpoints

### `GET /`

Health check.

```json
{"service": "SmartInventory AI - Chatbot Service", "status": "ok"}
```

### `POST /agent/chat`

Ruta oficial del contrato.

**Request** (`AgentChatRequest`):

```json
{
  "sessionId": "user-session-abc",
  "message": "Hola"
}
```

**Response** (`AgentChatResponse`, camelCase en JSON):

```json
{
  "response": "¡Hola! Bienvenido/a. ¿En qué puedo ayudarte hoy? Puedes buscar productos o explorar lo que tenemos.",
  "state": "START",
  "saleOrigin": "CHATBOT",
  "invoiceNumber": null
}
```

Ejemplo tras confirmar una compra:

```json
{
  "response": "¡Compra registrada con éxito! Tu número de factura es: FAC-000001.",
  "state": "SALE_COMPLETED",
  "saleOrigin": "CHATBOT",
  "invoiceNumber": "FAC-000001"
}
```

Reglas del contrato que implementa el código:

- `saleOrigin` siempre es el string `"CHATBOT"` (hardcodeado en `main.py`).
- `invoiceNumber` solo viene informado cuando `state` es `SALE_COMPLETED`.
- Los cuatro valores válidos de `state` son: `START`, `VALIDATING_STOCK`, `WAITING_CONFIRMATION`, `SALE_COMPLETED`.

### `POST /chat/message`

Alias que consume `ChatService.cs` de .NET hoy. Ejecuta exactamente la misma lógica que `/agent/chat` (`_handle_chat_request`). Está marcado como temporal en el código hasta que el equipo unifique la ruta canónica.

---

## Dependencia del backend .NET

Este servicio no tiene base de datos propia. Toda la verdad de catálogo, stock y ventas viene de .NET vía `httpx.AsyncClient`:

| Endpoint .NET | Uso en Repo 2 |
|---------------|---------------|
| `GET /api/products/variants/search?query=&size=&color=&onlyAvailable=true` | Búsqueda de variantes (`buscar_producto_semantico`). `query`, `size` y `color` son parámetros independientes. |
| `GET /api/inventory/variant/{productVariantId}` | Verificación de stock antes de confirmar (`verificar_inventario`) |
| `POST /api/sales` | Registro de venta al confirmar (`consolidar_venta`, `saleOriginId: 2`) |

### Degradación cuando .NET no responde

- Las tools devuelven mensajes de error explícitos (`"Error: ..."`) en lugar de inventar datos.
- `resolve_and_validate_stock` y `search_product` informan al usuario que no pudieron consultar catálogo/stock y mantienen `START`.
- `consolidate_sale` ante fallo vuelve a `idle`, limpia el carrito y responde que la compra no se completó — **sin** `invoiceNumber` ni `SALE_COMPLETED`.
- Si `httpx` lanza `TimeoutException` o `ConnectError` fuera de las tools (nivel `main.py`), la API responde **503** con `{"detail": "Servicio de negocio no disponible"}`.

Regla del proyecto: nunca inventar stock ni confirmar ventas si el backend no respondió con éxito.

---

## Manejo de errores de Gemini

| Situación | HTTP | Respuesta |
|-----------|------|-----------|
| Cuota/rate-limit (429 `RESOURCE_EXHAUSTED`) | **200** | Mensaje honesto de problema temporal, `state: START`, contrato JSON intacto |
| Otro error de Gemini | 500 | `{"detail": "Error interno del agente conversacional"}` |
| Bug no previsto | 500 | Igual |

El caso 429 devuelve 200 a propósito: .NET y React parsean siempre el mismo schema; un 500 rompería el contrato estricto de `ChatMessageResponse.cs`.

---

## Limitaciones conocidas

**Cuota gratuita de Gemini.** Google AI Studio impone límites diarios/por minuto (p. ej. 20 req/día en free tier para `gemini-2.5-flash`). Al agotarse, el servicio responde con el mensaje de “intenta de nuevo en unos minutos” en lugar de fallar con 500.

**Variantes ambiguas.** Si la búsqueda devuelve más de una variante tras filtrar, el bot lista opciones y pide talla/color más específicos. Las opciones se guardan en `metadata.pending_search_options` del checkpoint, pero el turno siguiente **no** las reutiliza automáticamente: el usuario debe repetir producto con talla y color explícitos.

**Estado en memoria.** `MemorySaver` pierde sesiones al reiniciar uvicorn. No hay persistencia entre despliegues.

**Reinicio de config.** Cambios en `.env` requieren reinicio del proceso; el cache de settings no se invalida solo.

---

## Tests

```powershell
pytest tests/test_chat_flow.py -v
```

Los tests no llaman a Gemini ni a .NET real. Mockean:

- `app.nodes.build_gemini_chat_model` — extracciones de intención predefinidas
- `app.nodes._invoke_product_search` — resultados de catálogo fake
- `app.nodes.verificar_inventario` — stock disponible simulado
- `app.nodes.consolidar_venta` — venta exitosa simulada

`tests/conftest.py` define `GOOGLE_API_KEY=test-key-for-pytest` para que Settings cargue sin `.env` real.

Casos cubiertos:

- Transiciones de estado del flujo completo (`START` → … → `SALE_COMPLETED` → `START`) vía `/agent/chat`
- Equivalencia del alias `/chat/message`

---

## Estructura del repo

```
smartinventory-ai-core/
├── main.py              # FastAPI, locks por sesión, manejo de errores HTTP
├── app/
│   ├── config.py        # Settings desde .env
│   ├── schemas.py       # Modelos Pydantic (HTTP + grafo)
│   ├── graph.py         # StateGraph + MemorySaver
│   ├── nodes.py         # Nodos y lógica conversacional
│   ├── tools.py         # Integración httpx con .NET
│   └── llm.py           # Factory Gemini
├── tests/
│   ├── conftest.py
│   └── test_chat_flow.py
├── requirements.txt
└── run_all.ps1          # Arranque conjunto .NET + FastAPI (Windows)
```
