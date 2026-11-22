"""Modelos y sesión de SQLAlchemy. Las fechas se guardan en UTC."""
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy import false as sa_false
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

STATUSES = ("vendida", "cita_agendada", "pendiente_seguimiento", "resuelta", "perdida")
CLOSED_STATUSES = ("vendida", "resuelta", "perdida")
PERSONAL = "personal"  # chat que no es de negocio: no se guarda ni se analiza
CONNECTION_STATES = ("creating", "qr", "connecting", "open", "closed")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Guarda siempre en UTC y devuelve fechas con zona horaria, también en SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(String(255))
    telegram_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True)
    link_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    tz: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)

    connections: Mapped[list["Connection"]] = relationship(back_populates="account")


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    instance_name: Mapped[str] = mapped_column(String(100), unique=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), default="creating")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    connected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)

    account: Mapped[Account] = relationship(back_populates="connections")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="connection")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("connection_id", "remote_jid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), index=True)
    remote_jid: Mapped[str] = mapped_column(String(128))
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    suggested_status: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    next_action: Mapped[Optional[str]] = mapped_column(Text)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    follow_up_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    unanswered: Mapped[bool] = mapped_column(Boolean, default=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    # Chat personal marcado a mano: se descartan sus mensajes y no se analiza.
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    last_message_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)

    connection: Mapped[Connection] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.sent_at"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "wa_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    wa_message_id: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(3))  # "in" (cliente) u "out" (el negocio)
    text: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(32), default="text")
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    suggested_status: Mapped[Optional[str]] = mapped_column(String(32))
    final_status: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


def normalize_url(url: str) -> str:
    """Railway entrega postgresql://; SQLAlchemy necesita indicar el driver psycopg 3."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_url(url)
    if url.startswith("sqlite"):
        import os

        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    """create_all no modifica tablas que ya existen: añade las columnas nuevas que falten."""
    columns = {c["name"] for c in inspect(engine).get_columns("conversations")}
    if "ignored" not in columns:
        default = "0" if engine.dialect.name == "sqlite" else "FALSE"
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE conversations ADD COLUMN ignored BOOLEAN NOT NULL DEFAULT {default}"))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
