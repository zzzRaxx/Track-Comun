"""Análisis de conversaciones con el LLM: qué analizar, cuándo y cómo guardar el resultado."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app import config, llm
from app.db import Connection, Conversation, Message, now_utc, session_scope

log = logging.getLogger(__name__)

AnalyzeFn = Callable[[str, datetime], Awaitable[llm.ChatAnalysis]]

BATCH_SIZE = 10          # conversaciones por ciclo, para no saturar la cuota del LLM
RETRY_AFTER_MIN = 15     # espera antes de reintentar una conversación cuyo análisis falló


@dataclass
class Job:
    conversation_id: int
    transcript: str
    last_direction: str       # "in" (cliente) u "out" (negocio)
    analyzed_at: datetime     # momento en que se leyeron los mensajes


def build_transcript(messages: list[Message], tz) -> str:
    lines = []
    for m in messages:
        who = "Negocio" if m.direction == "out" else "Cliente"
        stamp = m.sent_at.astimezone(tz).strftime("%d/%m %H:%M")
        lines.append(f"[{stamp}] {who}: {m.text}")
    text = "\n".join(lines)
    if len(text) > config.MAX_TRANSCRIPT_CHARS:
        text = "[...conversación recortada...]\n" + text[-config.MAX_TRANSCRIPT_CHARS:]
    return text


def pending_ids(session: Session, silence_cutoff: datetime, limit: int = BATCH_SIZE,
                account_id: Optional[int] = None) -> list[int]:
    """Conversaciones con mensajes nuevos desde el último análisis y con silencio suficiente."""
    query = select(Conversation.id)
    if account_id is not None:
        query = query.join(Connection, Connection.id == Conversation.connection_id).where(Connection.account_id == account_id)
    return list(
        session.scalars(
            query
            .where(
                Conversation.ignored.is_(False),
                Conversation.last_message_at.is_not(None),
                Conversation.last_message_at <= silence_cutoff,
                or_(
                    Conversation.last_analyzed_at.is_(None),
                    Conversation.last_analyzed_at < Conversation.last_message_at,
                ),
            )
            .order_by(Conversation.last_message_at)
            .limit(limit)
        )
    )


def load_job(session: Session, conversation_id: int) -> Optional[Job]:
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.ignored:
        return None
    started = now_utc()  # los mensajes que lleguen después de este instante volverán a poner la conversación en cola
    messages = list(
        session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.sent_at, Message.id)
        )
    )
    if not messages:
        return None
    return Job(conv.id, build_transcript(messages, config.LOCAL_TZ), messages[-1].direction, started)


def _parse_follow_up(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def save_result(session: Session, job: Job, result: llm.ChatAnalysis) -> None:
    conv = session.get(Conversation, job.conversation_id)
    if conv is None:
        return
    conv.summary = result.resumen
    conv.suggested_status = result.estado
    conv.next_action = result.accion_siguiente
    conv.amount = result.monto
    conv.follow_up_at = _parse_follow_up(result.fecha_seguimiento)
    # Solo puede haber una pregunta sin responder si el último mensaje es del cliente.
    conv.unanswered = bool(result.pregunta_sin_responder) and job.last_direction == "in"
    if conv.customer_name is None and result.cliente and result.cliente.lower() != "cliente":
        conv.customer_name = result.cliente
    conv.last_analyzed_at = job.analyzed_at


async def analyze_conversation(factory: sessionmaker[Session], conversation_id: int,
                               analyze: AnalyzeFn = llm.analyze) -> bool:
    """Analiza una conversación y guarda la sugerencia. Devuelve False si no se pudo."""
    def _load():
        with session_scope(factory) as s:
            return load_job(s, conversation_id)

    job = await asyncio.to_thread(_load)
    if job is None:
        return False
    result = await analyze(job.transcript, job.analyzed_at.astimezone(config.LOCAL_TZ))

    def _save():
        with session_scope(factory) as s:
            save_result(s, job, result)

    await asyncio.to_thread(_save)
    return True


async def analysis_cycle(factory: sessionmaker[Session], analyze: AnalyzeFn = llm.analyze,
                         failures: Optional[dict[int, datetime]] = None,
                         silence_minutes: Optional[int] = None) -> int:
    """Un ciclo de análisis. `failures` recuerda cuándo reintentar las conversaciones que fallaron."""
    failures = failures if failures is not None else {}
    cutoff = now_utc() - timedelta(minutes=config.SILENCE_MINUTES if silence_minutes is None else silence_minutes)

    def _pending():
        with session_scope(factory) as s:
            return pending_ids(s, cutoff, BATCH_SIZE * 3)

    done = 0
    for cid in await asyncio.to_thread(_pending):
        if done >= BATCH_SIZE:
            break
        retry_at = failures.get(cid)
        if retry_at and retry_at > now_utc():
            continue
        try:
            if await analyze_conversation(factory, cid, analyze):
                done += 1
                failures.pop(cid, None)
        except Exception as exc:  # noqa: BLE001 - un fallo no debe detener al resto
            log.warning("No se pudo analizar la conversación %s: %s", cid, exc)
            failures[cid] = now_utc() + timedelta(minutes=RETRY_AFTER_MIN)
    return done


async def run_loop(factory: sessionmaker[Session], interval_seconds: int = 60) -> None:
    failures: dict[int, datetime] = {}
    while True:
        try:
            n = await analysis_cycle(factory, failures=failures)
            if n:
                log.info("Conversaciones analizadas: %d", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Falló el ciclo de análisis")
        await asyncio.sleep(interval_seconds)
