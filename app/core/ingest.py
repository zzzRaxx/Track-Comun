"""Guarda los mensajes entrantes y mantiene el estado de cada conversación."""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.evolution import ConnectionUpdate, IncomingMessage
from app.db import Connection, Conversation, Message, now_utc

log = logging.getLogger(__name__)

# Estado de Evolution -> estado de la conexión en la base de datos
_STATE_MAP = {"open": "open", "connecting": "connecting", "close": "closed", "closed": "closed"}


def get_connection(session: Session, instance_name: str) -> Connection | None:
    return session.scalar(select(Connection).where(Connection.instance_name == instance_name))


def _get_or_create_conversation(session: Session, connection: Connection, msg: IncomingMessage) -> Conversation:
    conv = session.scalar(
        select(Conversation).where(
            Conversation.connection_id == connection.id,
            Conversation.remote_jid == msg.remote_jid,
        )
    )
    if conv is None:
        conv = Conversation(connection_id=connection.id, remote_jid=msg.remote_jid)
        session.add(conv)
        session.flush()
    return conv


def ingest_message(session: Session, connection: Connection, msg: IncomingMessage) -> bool:
    """Guarda el mensaje. Devuelve False si ya existía (entrega repetida del webhook)."""
    conv = _get_or_create_conversation(session, connection, msg)
    if conv.ignored:  # chat personal: el mensaje se descarta sin guardarse
        return False
    exists = session.scalar(
        select(Message.id).where(
            Message.conversation_id == conv.id, Message.wa_message_id == msg.wa_message_id
        )
    )
    if exists:
        return False

    try:
        with session.begin_nested():
            session.add(
                Message(
                    conversation_id=conv.id,
                    wa_message_id=msg.wa_message_id,
                    direction="out" if msg.from_me else "in",
                    text=msg.text,
                    type=msg.type,
                    sent_at=msg.sent_at,
                )
            )
    except IntegrityError:  # dos entregas simultáneas del mismo mensaje
        return False

    if not msg.from_me and msg.push_name:
        conv.customer_name = msg.push_name

    # Solo el mensaje más reciente decide quién habló último.
    if conv.last_message_at is None or msg.sent_at >= conv.last_message_at:
        conv.last_message_at = msg.sent_at
        conv.unanswered = not msg.from_me
        if not msg.from_me:
            conv.done = False  # el cliente volvió a escribir: la conversación se reabre
    session.flush()
    return True


def apply_connection_update(session: Session, update: ConnectionUpdate) -> Connection | None:
    connection = get_connection(session, update.instance)
    if connection is None:
        return None
    new_state = _STATE_MAP.get(update.state.lower())
    if new_state is None:
        return connection
    connection.state = new_state
    if new_state == "open" and connection.connected_at is None:
        connection.connected_at = now_utc()
    return connection


def needs_analysis(conv: Conversation, silence_cutoff: datetime) -> bool:
    """Conversación con mensajes nuevos desde el último análisis y silencio suficiente."""
    if conv.last_message_at is None or conv.last_message_at > silence_cutoff:
        return False
    return conv.last_analyzed_at is None or conv.last_analyzed_at < conv.last_message_at
