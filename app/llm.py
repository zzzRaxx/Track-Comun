import asyncio
import logging
from datetime import datetime
from typing import Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Eres un asistente para agentes de atención al cliente y ventas.
Recibes la transcripción de UN chat entre el negocio y un cliente. Cada línea empieza con la hora y con
«Cliente» o «Negocio». Las marcas [audio], [imagen], [documento] indican archivos que no puedes ver: no
supongas su contenido. La conversación puede estar recortada y empezar a mitad de una charla.
Devuelve un análisis breve y fiel.

Reglas:
- No inventes datos: si algo no aparece en el chat, déjalo en null.
- "resumen": 1 a 3 frases con lo hablado: qué quería el cliente y en qué quedó.
- "estado": elige UNO. Revisa en este orden y gana el primero que se cumpla:
  1. perdida: el cliente rechazó, canceló, dijo que ya no le interesa o que compró en otro lado.
  2. vendida: hay una compra o un pago CONFIRMADO por el cliente: aceptó comprar o contratar («me lo llevo»,
     «lo quiero», «contrato el plan»), se inscribió, pagó o envió un comprobante, o dejó un adelanto o una reserva
     pagada. Si además se habla de entrega, despacho, recojo, instalación o primera clase con fecha, eso es la
     logística de la venta: el estado sigue siendo vendida (NO cita_agendada). Pon esa fecha en
     "fecha_seguimiento" y lo que hay que hacer en "accion_siguiente" (por ejemplo, entregar el pedido).
  3. cita_agendada: se acordó una fecha y hora para una visita, consulta, llamada, demostración o revisión, y
     todavía NO hay compra ni pago confirmados.
  4. pendiente_seguimiento: falta algo: una respuesta, una cotización por enviar, una decisión del cliente
     («lo pienso», «te confirmo»), un pago prometido o una acción del negocio. Un precio o presupuesto dado
     sin pago ni aceptación de compra es pendiente_seguimiento, no vendida.
  5. resuelta: consulta atendida y sin nada pendiente ni venta (horario, ubicación, soporte resuelto).
- "pregunta_sin_responder": true solo si el último mensaje relevante es del cliente y espera respuesta.
- "fecha_seguimiento": fecha y hora local en formato ISO 8601 (YYYY-MM-DDTHH:MM) si se acordó o se puede deducir
  un momento para retomar o una cita; usa la fecha actual dada para resolver "mañana" o "el lunes". Si no, null.
- "accion_siguiente": la próxima acción concreta del agente, o null si no hay.
- "monto": importe de la venta o del presupuesto acordado si se menciona, sin símbolo de moneda, o null.
- Responde en español."""


class ChatAnalysis(BaseModel):
    cliente: str = Field(description="Nombre del cliente o 'Cliente' si no se conoce")
    resumen: str
    estado: Literal["vendida", "cita_agendada", "pendiente_seguimiento", "resuelta", "perdida"]
    pregunta_sin_responder: bool
    monto: Optional[float] = None
    fecha_seguimiento: Optional[str] = None
    accion_siguiente: Optional[str] = None


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _attempt_plan(attempts_per_model: int) -> list[str]:
    """Modelo principal primero y, si falla, el de respaldo (cuando es distinto)."""
    plan = [config.GEMINI_MODEL] * attempts_per_model
    if config.GEMINI_FALLBACK_MODEL and config.GEMINI_FALLBACK_MODEL != config.GEMINI_MODEL:
        plan += [config.GEMINI_FALLBACK_MODEL] * attempts_per_model
    return plan


async def analyze(transcript: str, now_local: datetime, attempts_per_model: int = 2,
                  backoff_seconds: float = 1.0) -> ChatAnalysis:
    prompt = (
        f"Fecha y hora actual: {now_local.strftime('%Y-%m-%d %H:%M')} ({config.TZ_NAME}).\n\n"
        f"Transcripción:\n{transcript}"
    )
    plan = _attempt_plan(attempts_per_model)
    last_exc: Exception | None = None
    for i, model in enumerate(plan):
        try:
            resp = await _get_client().aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=ChatAnalysis,
                    temperature=0.1,
                ),
            )
            return ChatAnalysis.model_validate_json(resp.text)
        except Exception as exc:  # sobrecarga (503), límites de tasa (429), red o JSON inválido
            last_exc = exc
            log.warning("Gemini (%s) falló, intento %d/%d: %s", model, i + 1, len(plan), str(exc)[:200])
            if i < len(plan) - 1:
                await asyncio.sleep(backoff_seconds * 2 ** min(i, 3))
    raise RuntimeError(f"No se pudo analizar el chat: {last_exc}")
