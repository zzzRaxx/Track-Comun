"""Pantallas: registro, inicio de sesión, tablero de chats y conexión de números de WhatsApp."""
import logging
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.core import analysis, board as boards
from app.core import connections as conns
from app.db import STATUSES, Account, Connection, Conversation, Message, now_utc, session_scope
from app.web import auth

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATE_LABELS = {
    "creating": "Preparando…",
    "qr": "Esperando que escanees el código",
    "connecting": "Conectando…",
    "open": "Conectado",
    "closed": "Desconectado",
}
templates.env.globals["STATE_LABELS"] = STATE_LABELS
templates.env.globals["STATUS_LABELS"] = boards.LABELS
templates.env.globals["STATUSES"] = STATUSES
templates.env.globals["SILENCE_MINUTES"] = config.SILENCE_MINUTES


class NotLoggedIn(Exception):
    pass


def get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def current_account(request: Request, session: Session = Depends(get_session)) -> Account:
    account_id = request.session.get("account_id")
    account = session.get(Account, account_id) if account_id else None
    if account is None:
        request.session.clear()
        raise NotLoggedIn()
    return account


def _render(request: Request, template: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _owned(session: Session, account: Account, connection_id: int) -> Connection:
    conn = conns.get_owned(session, account.id, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Conexión no encontrada")
    return conn


def _owned_conversation(session: Session, account: Account, conversation_id: int) -> Conversation:
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.connection.account_id != account.id:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return conv


# --------------------------------------------------------------------------- cuentas
@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return _render(request, "register.html", error=None, email="", name="")


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    name: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        account = auth.register(session, email, password, name)
    except ValueError as exc:
        return _render(request, "register.html", status_code=400, error=str(exc), email=email, name=name)
    request.session.clear()
    request.session["account_id"] = account.id
    return RedirectResponse("/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _render(request, "login.html", error=None, email="")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    account = auth.authenticate(session, email, password)
    if account is None:
        return _render(request, "login.html", status_code=400, error="Correo o contraseña incorrectos.", email=email)
    request.session.clear()  # evita reutilizar una sesión previa
    request.session["account_id"] = account.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- tablero
@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    account_id = request.session.get("account_id")
    account = session.get(Account, account_id) if account_id else None
    if account is None:
        return RedirectResponse("/login", status_code=303)
    has_numbers = bool(conns.list_connections(session, account.id))
    return _render(request, "board.html", account=account, active="board", board=boards.build_board(session, account.id),
                   has_numbers=has_numbers, now=now_utc())


@router.get("/board", response_class=HTMLResponse)
def board_fragment(request: Request, account: Account = Depends(current_account), session: Session = Depends(get_session)):
    """Solo las columnas: el tablero se refresca con esto cada pocos segundos."""
    return _render(request, "_board.html", board=boards.build_board(session, account.id),
                   has_numbers=bool(conns.list_connections(session, account.id)))


@router.get("/conversations/{conversation_id}/card", response_class=HTMLResponse)
def conversation_card(request: Request, conversation_id: int, account: Account = Depends(current_account),
                      session: Session = Depends(get_session)):
    conv = _owned_conversation(session, account, conversation_id)
    card = boards.load_cards(session, [conv], now_utc())[0]
    return _render(request, "_card.html", c=card)


@router.get("/conversations/{conversation_id}/drawer", response_class=HTMLResponse)
def conversation_drawer(request: Request, conversation_id: int, account: Account = Depends(current_account),
                        session: Session = Depends(get_session)):
    conv = _owned_conversation(session, account, conversation_id)
    now = now_utc()
    card = boards.load_cards(session, [conv], now)[0]
    messages = [
        {"mine": m.direction == "out", "text": m.text, "at": boards.when(m.sent_at, now)}
        for m in boards.thread(session, conv.id)
    ]
    return _render(request, "_drawer.html", c=card, messages=messages, current=card.column)


@router.post("/conversations/{conversation_id}/status")
def conversation_status(conversation_id: int, status: str = Form(""), account: Account = Depends(current_account),
                        session: Session = Depends(get_session)):
    conv = _owned_conversation(session, account, conversation_id)
    try:
        boards.set_status(session, conv, status)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "column": boards.column_of(conv)}


@router.post("/conversations/{conversation_id}/include")
def conversation_include(conversation_id: int, account: Account = Depends(current_account),
                         session: Session = Depends(get_session)):
    """Saca un chat de «Personal»."""
    conv = _owned_conversation(session, account, conversation_id)
    boards.include_again(session, conv)
    return {"ok": True}


@router.post("/ignored")
def ignore_a_number(phone: str = Form(""), account: Account = Depends(current_account), session: Session = Depends(get_session)):
    """Marca un número como personal: no se guardará ni se analizará nada de él."""
    try:
        digits = conns.normalize_phone(phone)
    except conns.UserError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    count = boards.ignore_number(session, account.id, digits)
    if count == 0:
        return JSONResponse({"ok": False, "error": "Primero conecta un número de WhatsApp."}, status_code=400)
    return {"ok": True, "number": digits}


MANUAL_BATCH = 8  # chats por pulsación, para no tardar demasiado ni agotar la cuota del modelo


@router.post("/board/analyze")
async def board_analyze(request: Request, account: Account = Depends(current_account), session: Session = Depends(get_session)):
    """Analiza ya los chats pendientes de la cuenta, sin esperar los minutos de silencio."""
    if not config.GEMINI_API_KEY:
        return JSONResponse({"ok": False, "error": "El análisis con IA no está configurado."}, status_code=503)
    ids = analysis.pending_ids(session, now_utc(), MANUAL_BATCH + 1, account_id=account.id)
    session.commit()  # libera la transacción antes de las llamadas largas al modelo
    more = len(ids) > MANUAL_BATCH
    analyzed = failed = 0
    for conversation_id in ids[:MANUAL_BATCH]:
        try:
            if await analysis.analyze_conversation(request.app.state.session_factory, conversation_id):
                analyzed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("Análisis manual fallido (%s): %s", conversation_id, exc)
    return {"ok": True, "analyzed": analyzed, "failed": failed, "more": more}


@router.post("/conversations/{conversation_id}/analyze")
async def conversation_analyze(request: Request, conversation_id: int, account: Account = Depends(current_account),
                               session: Session = Depends(get_session)):
    _owned_conversation(session, account, conversation_id)
    if not config.GEMINI_API_KEY:
        return JSONResponse({"ok": False, "error": "El análisis con IA no está configurado."}, status_code=503)
    session.commit()  # libera la transacción antes de la llamada larga al modelo
    try:
        done = await analysis.analyze_conversation(request.app.state.session_factory, conversation_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Análisis manual fallido (%s): %s", conversation_id, exc)
        return JSONResponse({"ok": False, "error": "No se pudo analizar ahora. Inténtalo en un momento."}, status_code=502)
    return {"ok": done}


# --------------------------------------------------------------------------- números
@router.get("/numbers", response_class=HTMLResponse)
def numbers(request: Request, account: Account = Depends(current_account), session: Session = Depends(get_session)):
    rows = []
    for conn in conns.list_connections(session, account.id):
        n_conv = session.scalar(select(func.count()).select_from(Conversation).where(Conversation.connection_id == conn.id))
        n_msg = session.scalar(
            select(func.count()).select_from(Message).join(Conversation).where(Conversation.connection_id == conn.id)
        )
        rows.append({"conn": conn, "conversations": n_conv, "messages": n_msg})
    return _render(request, "numbers.html", account=account, active="numbers", rows=rows,
                   max_connections=conns.MAX_CONNECTIONS_PER_ACCOUNT)


# --------------------------------------------------------------------------- conexiones
@router.get("/connections/new", response_class=HTMLResponse)
def connection_form(request: Request, account: Account = Depends(current_account)):
    return _render(request, "connection_new.html", account=account, active="numbers", error=None, phone="")


@router.post("/connections", response_class=HTMLResponse)
async def connection_create(
    request: Request,
    phone: str = Form(""),
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    try:
        conn = await conns.create_connection(session, request.app.state.evolution, account.id, phone)
    except conns.UserError as exc:
        return _render(request, "connection_new.html", status_code=400, account=account, active="numbers",
                       error=str(exc), phone=phone)
    return RedirectResponse(f"/connections/{conn.id}", status_code=303)


@router.get("/connections/{connection_id}", response_class=HTMLResponse)
def connection_page(
    request: Request,
    connection_id: int,
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    conn = _owned(session, account, connection_id)
    return _render(request, "connection.html", account=account, active="numbers", conn=conn)


@router.get("/connections/{connection_id}/panel", response_class=HTMLResponse)
async def connection_panel(
    request: Request,
    connection_id: int,
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    """Fragmento que la página consulta cada pocos segundos para mostrar el estado."""
    conn = _owned(session, account, connection_id)
    if conn.state != "open":
        await conns.sync_state(request.app.state.evolution, conn)
    return _render(request, "_panel.html", conn=conn)


@router.get("/connections/{connection_id}/qr", response_class=HTMLResponse)
async def connection_qr(
    request: Request,
    connection_id: int,
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    """QR y código de emparejamiento actuales. La página lo renueva antes de que caduque."""
    conn = _owned(session, account, connection_id)
    if conn.state == "open":
        return HTMLResponse("")
    return await _qr_fragment(request, session, conn, retry=False)


@router.post("/connections/{connection_id}/retry", response_class=HTMLResponse)
async def connection_retry(
    request: Request,
    connection_id: int,
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    conn = _owned(session, account, connection_id)
    return await _qr_fragment(request, session, conn, retry=True)


async def _qr_fragment(request: Request, session: Session, conn: Connection, retry: bool):
    evo = request.app.state.evolution
    try:
        data = await (conns.retry(session, evo, conn) if retry else conns.fresh_qr(evo, conn))
    except Exception as exc:  # noqa: BLE001 - se muestra un mensaje amable en lugar de un error
        log.warning("QR no disponible para %s: %s", conn.instance_name, exc)
        return _render(request, "_qr.html", conn=conn, qr=None, code=None, error=True)
    code = data.get("pairingCode")
    if code and len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    return _render(request, "_qr.html", conn=conn, qr=conns.qr_image_src(data), code=code, error=False)


@router.post("/connections/{connection_id}/disconnect")
async def connection_disconnect(
    request: Request,
    connection_id: int,
    account: Account = Depends(current_account),
    session: Session = Depends(get_session),
):
    conn = _owned(session, account, connection_id)
    await conns.disconnect(session, request.app.state.evolution, conn)
    return RedirectResponse("/numbers", status_code=303)
