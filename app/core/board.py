"""Datos del tablero kanban: qué tarjeta va en qué columna y cómo se describe cada una."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import config
from app.db import CLOSED_STATUSES, PERSONAL, STATUSES, Connection, Conversation, Correction, Message, now_utc

UNSORTED = "sin_clasificar"
COLUMN_LIMIT = 30  # tarjetas visibles por columna; el resto se cuenta

COLUMNS = [
    (UNSORTED, "Sin clasificar", "En curso o por analizar"),
    ("pendiente_seguimiento", "Pendiente", "Falta una respuesta o una acción"),
    ("cita_agendada", "Cita agendada", "Hay fecha acordada"),
    ("vendida", "Vendida", "Compra o pago confirmado"),
    ("resuelta", "Resuelta", "Atendida, sin nada pendiente"),
    ("perdida", "Perdida", "El cliente no sigue"),
    (PERSONAL, "Personal", "Chats que no son de negocio: no se guardan ni se analizan"),
]
LABELS = {key: label for key, label, _ in COLUMNS}

_DAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def ago(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"hace {seconds // 60} min"
    if seconds < 86400:
        return f"hace {seconds // 3600} h"
    return f"hace {seconds // 86400} d"


def when(moment: datetime, now: datetime) -> str:
    """«hoy 15:00», «mañana 09:30», «ayer 18:00» o «vie 28 10:00», en hora local."""
    local, today = moment.astimezone(config.LOCAL_TZ), now.astimezone(config.LOCAL_TZ).date()
    delta = (local.date() - today).days
    clock = local.strftime("%H:%M")
    if delta == 0:
        return f"hoy {clock}"
    if delta == 1:
        return f"mañana {clock}"
    if delta == -1:
        return f"ayer {clock}"
    return f"{_DAYS[local.weekday()]} {local.day} {clock}"


def column_of(conv: Conversation) -> str:
    if conv.ignored:
        return PERSONAL
    return conv.status or conv.suggested_status or UNSORTED


@dataclass
class Card:
    id: int
    column: str
    name: str
    phone: Optional[str]
    chat_url: Optional[str]
    last_text: str
    last_from_client: bool
    last_ago: str
    summary: Optional[str]
    next_action: Optional[str]
    follow_up: Optional[str]
    overdue: bool
    amount: Optional[str]
    unanswered: bool
    confirmed: bool
    suggested: Optional[str]
    suggested_label: Optional[str]
    done: bool
    analyzed: bool
    ignored: bool = False


@dataclass
class Column:
    key: str
    label: str
    hint: str
    cards: list[Card] = field(default_factory=list)
    total: int = 0

    @property
    def hidden(self) -> int:
        return max(0, self.total - len(self.cards))


@dataclass
class Board:
    columns: list[Column]
    waiting: int       # clientes que esperan respuesta
    overdue: int       # seguimientos vencidos
    attention: int     # conversaciones que necesitan acción (sin repetir)
    sales_total: float
    total: int

    @property
    def sales_text(self) -> Optional[str]:
        return money(self.sales_total) if self.sales_total else None

    @property
    def finding(self) -> str:
        parts = []
        if self.waiting:
            parts.append(f"{self.waiting} {'cliente espera' if self.waiting == 1 else 'clientes esperan'} respuesta")
        if self.overdue:
            parts.append(f"{self.overdue} {'seguimiento vencido' if self.overdue == 1 else 'seguimientos vencidos'}")
        return " · ".join(parts) if parts else "Todo al día: nadie espera respuesta ni tiene un seguimiento vencido."


def money(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    text = f"{value:,.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value):,}"
    return f"{config.CURRENCY} {text}"


def build_card(conv: Conversation, last: Optional[Message], phone: Optional[str], now: datetime) -> Card:
    overdue = bool(conv.follow_up_at and conv.follow_up_at < now and not conv.done)
    sugg = conv.suggested_status
    return Card(
        id=conv.id,
        column=column_of(conv),
        name=conv.customer_name or phone or "Cliente sin nombre",
        phone=phone,
        chat_url=chat_url(phone),
        last_text=last.text if last else "",
        last_from_client=bool(last and last.direction == "in"),
        last_ago=ago(last.sent_at, now) if last else "",
        summary=conv.summary,
        next_action=conv.next_action,
        follow_up=when(conv.follow_up_at, now) if conv.follow_up_at else None,
        overdue=overdue,
        amount=money(conv.amount),
        unanswered=bool(conv.unanswered and not conv.done),
        confirmed=conv.status is not None,
        suggested=sugg,
        suggested_label=LABELS.get(sugg) if sugg else None,
        done=conv.done,
        analyzed=conv.last_analyzed_at is not None,
        ignored=bool(conv.ignored),
    )


def chat_url(phone: Optional[str]) -> Optional[str]:
    """Enlace que abre la conversación en WhatsApp. Sin número visible (contactos @lid) no hay enlace."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"https://wa.me/{digits}" if 7 <= len(digits) <= 15 else None


def _phone(jid: str) -> Optional[str]:
    return "+" + jid.split("@", 1)[0] if jid.endswith("@s.whatsapp.net") else None


def load_cards(session: Session, conversations: list[Conversation], now: datetime) -> list[Card]:
    if not conversations:
        return []
    ids = [c.id for c in conversations]
    last_ids = select(func.max(Message.id)).where(Message.conversation_id.in_(ids)).group_by(Message.conversation_id)
    last = {m.conversation_id: m for m in session.scalars(select(Message).where(Message.id.in_(last_ids)))}
    return [build_card(c, last.get(c.id), _phone(c.remote_jid), now) for c in conversations]


def build_board(session: Session, account_id: int, now: Optional[datetime] = None) -> Board:
    now = now or now_utc()
    convs = list(
        session.scalars(
            select(Conversation)
            .join(Connection, Connection.id == Conversation.connection_id)
            .where(Connection.account_id == account_id)
            .order_by(Conversation.last_message_at.desc().nulls_last())
        )
    )
    cards = load_cards(session, convs, now)
    by_key = {key: Column(key, label, hint) for key, label, hint in COLUMNS}
    sales = 0.0
    amounts = {c.id: c.amount for c in convs}
    for card in cards:
        col = by_key[card.column]
        col.total += 1
        col.cards.append(card)
        if card.column == "vendida" and amounts.get(card.id):
            sales += amounts[card.id]
    for col in by_key.values():
        # primero lo que espera respuesta o está vencido; el resto, por actividad reciente (ya viene ordenado)
        col.cards.sort(key=lambda c: (not (c.unanswered or c.overdue)))
        col.cards = col.cards[:COLUMN_LIMIT]
    waiting = sum(1 for c in cards if c.unanswered)
    overdue = sum(1 for c in cards if c.overdue)
    attention = sum(1 for c in cards if c.unanswered or c.overdue)
    return Board(list(by_key.values()), waiting, overdue, attention, sales, len(cards))


def mark_personal(session: Session, conv: Conversation) -> None:
    """Chat personal: se borran los mensajes guardados y no se guardará ni analizará nada más de este contacto."""
    session.execute(delete(Message).where(Message.conversation_id == conv.id))
    conv.ignored = True
    conv.status = PERSONAL
    conv.suggested_status = None
    conv.summary = conv.next_action = conv.amount = conv.follow_up_at = None
    conv.unanswered = conv.done = False
    conv.last_analyzed_at = conv.last_reminded_at = None


def set_status(session: Session, conv: Conversation, status: str) -> None:
    """La persona confirma o cambia el estado. Se guarda la corrección para medir el acierto de la IA."""
    if status == PERSONAL:
        mark_personal(session, conv)
        return
    if status not in STATUSES:
        raise ValueError("Estado no válido")
    if conv.ignored:
        raise ValueError("Este chat es personal: usa «Volver a incluir» para tratarlo como un chat de negocio.")
    session.add(Correction(conversation_id=conv.id, suggested_status=conv.suggested_status, final_status=status))
    conv.status = status
    conv.done = status in CLOSED_STATUSES and not conv.unanswered


def include_again(session: Session, conv: Conversation) -> None:
    """Saca el chat de «Personal». Se borra su marca: el próximo mensaje lo crea como un chat nuevo."""
    session.execute(delete(Correction).where(Correction.conversation_id == conv.id))
    session.execute(delete(Message).where(Message.conversation_id == conv.id))
    session.delete(conv)


def ignore_number(session: Session, account_id: int, digits: str) -> int:
    """Ignora un número en todas las conexiones de la cuenta, aunque aún no haya escrito."""
    jid = f"{digits}@s.whatsapp.net"
    count = 0
    for conn in session.scalars(select(Connection).where(Connection.account_id == account_id)):
        conv = session.scalar(select(Conversation).where(Conversation.connection_id == conn.id, Conversation.remote_jid == jid))
        if conv is None:
            conv = Conversation(connection_id=conn.id, remote_jid=jid, last_message_at=now_utc())
            session.add(conv)
            session.flush()
        mark_personal(session, conv)
        count += 1
    return count


def thread(session: Session, conversation_id: int, limit: int = 80) -> list[Message]:
    rows = list(
        session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.sent_at.desc(), Message.id.desc()).limit(limit)
        )
    )
    return list(reversed(rows))
