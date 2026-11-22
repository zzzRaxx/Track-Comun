import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.channels.evolution import EvolutionError
from app.db import Connection, Message, session_scope
from app.main import create_app

TOKEN = "token-de-prueba"


class FakeEvolution:
    def __init__(self):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.fail_create = False
        self.missing: set[str] = set()  # instancias que Evolution "ya no conoce"
        self._state = "connecting"

    async def create_instance(self, name, webhook_url, number=None, token=None):
        if self.fail_create:
            raise EvolutionError("caído")
        self.created.append({"name": name, "url": webhook_url, "number": number, "token": token})
        self.missing.discard(name)
        return {"qrcode": {"pairingCode": "ABCD1234"}}

    async def connect(self, name, number=None):
        if name in self.missing:
            raise EvolutionError("404")
        return {"pairingCode": "ABCD1234", "code": "2@x", "base64": "iVBORw0KGgo=", "count": 1}

    async def state(self, name):
        return self._state

    async def delete_instance(self, name):
        self.deleted.append(name)
        self.missing.add(name)
        return {}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture
def evo():
    return FakeEvolution()


@pytest.fixture
def make_client(tmp_path, evo):
    clients = []

    def _make():
        app = create_app(f"sqlite:///{tmp_path / 'w.db'}", validate=False, evolution=evo, run_analysis=False)
        c = TestClient(app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def signup(c, email="ana@ejemplo.com", password="clave-segura-1", name="Ana"):
    return c.post("/register", data={"email": email, "password": password, "name": name})


def connect(c, phone="+591 7000 0001"):
    r = c.post("/connections", data={"phone": phone})
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


# --------------------------------------------------------------------------- cuentas
def test_sin_sesion_redirige_al_login(client):
    assert client.get("/").headers["location"] == "/login"
    assert client.get("/connections/new").headers["location"] == "/login"
    assert client.post("/connections", data={"phone": "59170000001"}).headers["location"] == "/login"


def test_registro_y_sesion(client):
    r = signup(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/")
    assert page.status_code == 200 and "Chats" in page.text and "Ana" in page.text and "Conecta tu primer número" in page.text


def test_registro_valida_datos(client):
    assert "correo válido" in signup(client, email="no-es-correo").text
    assert "al menos 8" in signup(client, password="corta").text
    signup(client)
    client.post("/logout")
    r = signup(client)
    assert r.status_code == 400 and "Ya existe" in r.text


def test_correo_no_distingue_mayusculas(client):
    signup(client, email="Ana@Ejemplo.com")
    client.post("/logout")
    r = client.post("/login", data={"email": "ANA@ejemplo.COM", "password": "clave-segura-1"})
    assert r.status_code == 303


def test_login_y_logout(client):
    signup(client)
    client.post("/logout")
    assert client.get("/").headers["location"] == "/login"
    bad = client.post("/login", data={"email": "ana@ejemplo.com", "password": "otra"})
    assert bad.status_code == 400 and "incorrectos" in bad.text
    unknown = client.post("/login", data={"email": "nadie@ejemplo.com", "password": "x"})
    assert unknown.status_code == 400 and "incorrectos" in unknown.text
    ok = client.post("/login", data={"email": "ana@ejemplo.com", "password": "clave-segura-1"})
    assert ok.status_code == 303 and client.get("/").status_code == 200


def test_la_contrasena_no_se_guarda_en_claro(client):
    signup(client, password="clave-segura-1")
    with session_scope(client.app.state.session_factory) as s:
        from app.db import Account

        stored = s.scalar(select(Account.password_hash))
    assert "clave-segura-1" not in stored and stored.startswith("scrypt$")


# --------------------------------------------------------------------------- conexiones
def test_crear_conexion_envia_numero_normalizado_y_token(client, evo):
    signup(client)
    cid = connect(client, "+591 7000-0001")
    assert evo.created[0]["number"] == "59170000001"
    assert evo.created[0]["token"] == TOKEN
    assert evo.created[0]["url"] == "http://127.0.0.1:8000/webhook/evolution"
    assert evo.created[0]["name"].startswith("acc1-")
    page = client.get(f"/connections/{cid}")
    assert page.status_code == 200 and "+59170000001" in page.text


def test_numero_invalido_no_crea_nada(client, evo):
    signup(client)
    r = client.post("/connections", data={"phone": "123"})
    assert r.status_code == 400 and "código de país" in r.text and evo.created == []


def test_error_de_evolution_no_deja_conexion_a_medias(client, evo):
    signup(client)
    evo.fail_create = True
    r = client.post("/connections", data={"phone": "59170000001"})
    assert r.status_code == 400 and "No pudimos iniciar" in r.text
    with session_scope(client.app.state.session_factory) as s:
        assert s.scalar(select(func.count()).select_from(Connection)) == 0


def test_numero_repetido_y_limite(client):
    signup(client)
    connect(client, "59170000001")
    r = client.post("/connections", data={"phone": "59170000001"})
    assert r.status_code == 400 and "ya está conectado" in r.text
    for i in range(2, 6):
        connect(client, f"5917000000{i}")
    r = client.post("/connections", data={"phone": "59170000009"})
    assert r.status_code == 400 and "hasta 5" in r.text


def test_qr_muestra_codigo_e_imagen(client):
    signup(client)
    cid = connect(client)
    html = client.get(f"/connections/{cid}/qr").text
    assert "ABCD-1234" in html and "data:image/png;base64,iVBORw0KGgo=" in html


def test_reintentar_reutiliza_la_instancia(client, evo):
    signup(client)
    cid = connect(client)
    html = client.post(f"/connections/{cid}/retry").text
    assert "ABCD-1234" in html and len(evo.created) == 1


def test_reintentar_recrea_la_instancia_si_evolution_la_perdio(client, evo):
    signup(client)
    cid = connect(client)
    evo.missing.add(evo.created[0]["name"])
    html = client.post(f"/connections/{cid}/retry").text
    assert "ABCD-1234" in html
    assert len(evo.created) == 2 and evo.created[1]["name"] == evo.created[0]["name"]


def test_panel_sigue_el_estado(client, evo):
    signup(client)
    cid = connect(client)
    assert 'data-state="connecting"' in client.get(f"/connections/{cid}/panel").text
    evo._state = "open"
    panel = client.get(f"/connections/{cid}/panel").text
    assert 'data-state="open"' in panel and "Conectado" in panel
    assert client.get(f"/connections/{cid}/qr").text == ""


def test_el_webhook_actualiza_lo_que_ve_el_usuario(client):
    signup(client)
    cid = connect(client)
    with session_scope(client.app.state.session_factory) as s:
        name = s.get(Connection, cid).instance_name
    client.post("/webhook/evolution", headers={"x-webhook-token": config.WEBHOOK_TOKEN},
                json={"event": "connection.update", "instance": name, "data": {"state": "open"}})
    client.post("/webhook/evolution", headers={"x-webhook-token": config.WEBHOOK_TOKEN},
                json={"event": "messages.upsert", "instance": name, "data": {
                    "key": {"remoteJid": "59170000009@s.whatsapp.net", "fromMe": False, "id": "X1"},
                    "message": {"conversation": "hola"}, "messageTimestamp": 1717689097}})
    home = client.get("/numbers").text
    assert "Conectado" in home and "1 conversaciones · 1 mensajes" in home


def test_desconectar_borra_la_instancia_y_conserva_los_mensajes(client, evo):
    signup(client)
    cid = connect(client)
    with session_scope(client.app.state.session_factory) as s:
        name = s.get(Connection, cid).instance_name
    client.post("/webhook/evolution", headers={"x-webhook-token": config.WEBHOOK_TOKEN},
                json={"event": "messages.upsert", "instance": name, "data": {
                    "key": {"remoteJid": "59170000009@s.whatsapp.net", "fromMe": False, "id": "X1"},
                    "message": {"conversation": "hola"}, "messageTimestamp": 1717689097}})
    r = client.post(f"/connections/{cid}/disconnect")
    assert r.status_code == 303 and evo.deleted == [name]
    with session_scope(client.app.state.session_factory) as s:
        assert s.get(Connection, cid).state == "closed"
        assert s.scalar(select(func.count()).select_from(Message)) == 1


def test_una_cuenta_no_ve_las_conexiones_de_otra(make_client):
    a = make_client()
    signup(a, email="a@ejemplo.com")
    cid = connect(a)
    b = make_client()
    signup(b, email="b@ejemplo.com")
    for path in (f"/connections/{cid}", f"/connections/{cid}/panel", f"/connections/{cid}/qr"):
        assert b.get(path).status_code == 404
    assert b.post(f"/connections/{cid}/retry").status_code == 404
    assert b.post(f"/connections/{cid}/disconnect").status_code == 404
    assert "59170000001" not in b.get("/numbers").text


# --------------------------------------------------------------------------- tablero
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.core import board as boards  # noqa: E402
from app.db import Conversation, Correction  # noqa: E402

NOW = datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)


def add_chat(client, conn_id, jid="59170000009@s.whatsapp.net", name="Marta", text="Hola", **fields):
    with session_scope(client.app.state.session_factory) as s:
        conv = Conversation(connection_id=conn_id, remote_jid=jid, customer_name=name, last_message_at=NOW, **fields)
        s.add(conv)
        s.flush()
        s.add(Message(conversation_id=conv.id, wa_message_id=f"w{jid}", direction="in", text=text, type="text", sent_at=NOW))
        return conv.id


def test_tiempos_en_espanol(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "LOCAL_TZ", ZoneInfo("UTC"))  # no depender del TZ del .env
    assert boards.ago(NOW - timedelta(seconds=20), NOW) == "ahora"
    assert boards.ago(NOW - timedelta(minutes=5), NOW) == "hace 5 min"
    assert boards.ago(NOW - timedelta(hours=3), NOW) == "hace 3 h"
    assert boards.ago(NOW - timedelta(days=2), NOW) == "hace 2 d"
    assert boards.when(NOW + timedelta(hours=1), NOW) == "hoy 16:00"
    assert boards.when(NOW + timedelta(days=1), NOW) == "mañana 15:00"
    assert boards.when(NOW - timedelta(days=1), NOW) == "ayer 15:00"
    assert boards.when(NOW + timedelta(days=3), NOW) == "mié 25 15:00"  # 22/11/2026 es domingo


def test_la_columna_sale_del_estado_confirmado_o_del_sugerido():
    c = Conversation(status=None, suggested_status=None)
    assert boards.column_of(c) == "sin_clasificar"
    c.suggested_status = "cita_agendada"
    assert boards.column_of(c) == "cita_agendada"
    c.status = "vendida"
    assert boards.column_of(c) == "vendida"


def test_tablero_agrupa_y_cuenta(client):
    signup(client)
    cid = connect(client)
    add_chat(client, cid, "59170000001@s.whatsapp.net", "Ana", unanswered=True)
    add_chat(client, cid, "59170000002@s.whatsapp.net", "Beto", suggested_status="vendida", amount=160.0)
    add_chat(client, cid, "59170000003@s.whatsapp.net", "Cora", status="resuelta", suggested_status="resuelta", done=True)
    add_chat(client, cid, "59170000004@s.whatsapp.net", "Dani", suggested_status="pendiente_seguimiento",
             follow_up_at=datetime(2026, 11, 20, tzinfo=timezone.utc))
    page = client.get("/").text
    assert "1 cliente espera respuesta" in page and "1 seguimiento vencido" in page
    assert "Bs 160" in page
    with session_scope(client.app.state.session_factory) as s:
        from app.db import Account
        b = boards.build_board(s, s.scalar(select(Account.id)))
    assert {c.key: c.total for c in b.columns} == {
        "sin_clasificar": 1, "pendiente_seguimiento": 1, "cita_agendada": 0, "vendida": 1, "resuelta": 1, "perdida": 0,
        "personal": 0}
    assert b.sales_total == 160.0 and b.waiting == 1 and b.overdue == 1


def test_confirmar_guarda_la_correccion_y_mueve_la_tarjeta(client):
    signup(client)
    cid = connect(client)
    conv_id = add_chat(client, cid, suggested_status="vendida", amount=90.0)
    r = client.post(f"/conversations/{conv_id}/status", data={"status": "perdida"})
    assert r.json() == {"ok": True, "column": "perdida"}
    with session_scope(client.app.state.session_factory) as s:
        conv = s.get(Conversation, conv_id)
        assert conv.status == "perdida" and conv.done is True
        fix = s.scalar(select(Correction))
        assert (fix.suggested_status, fix.final_status) == ("vendida", "perdida")


def test_estado_invalido_se_rechaza(client):
    signup(client)
    cid = connect(client)
    conv_id = add_chat(client, cid)
    assert client.post(f"/conversations/{conv_id}/status", data={"status": "inventado"}).status_code == 400
    assert client.post(f"/conversations/{conv_id}/status", data={"status": "sin_clasificar"}).status_code == 400


def test_detalle_y_tarjeta(client):
    signup(client)
    cid = connect(client)
    conv_id = add_chat(client, cid, text="¿Cuánto cuesta?", summary="Pregunta el precio", suggested_status="pendiente_seguimiento")
    drawer = client.get(f"/conversations/{conv_id}/drawer").text
    assert "¿Cuánto cuesta?" in drawer and "Pregunta el precio" in drawer and "sugerido" in drawer
    assert "Marta" in client.get(f"/conversations/{conv_id}/card").text


def test_analizar_sin_clave_responde_503(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    signup(client)
    conv_id = add_chat(client, connect(client))
    assert client.post(f"/conversations/{conv_id}/analyze").status_code == 503


def test_una_cuenta_no_toca_las_conversaciones_de_otra(make_client):
    a = make_client()
    signup(a, email="a@ejemplo.com")
    conv_id = add_chat(a, connect(a))
    b = make_client()
    signup(b, email="b@ejemplo.com")
    assert b.post(f"/conversations/{conv_id}/status", data={"status": "vendida"}).status_code == 404
    assert b.get(f"/conversations/{conv_id}/drawer").status_code == 404
    assert b.get(f"/conversations/{conv_id}/card").status_code == 404
    assert b.post(f"/conversations/{conv_id}/analyze").status_code == 404
    assert "Marta" not in b.get("/board").text


def test_ir_al_chat_arma_el_enlace_de_whatsapp(client):
    signup(client)
    cid = connect(client)
    normal = add_chat(client, cid, "59170000009@s.whatsapp.net", "Marta")
    privado = add_chat(client, cid, "99887766@lid", "Contacto oculto")
    board = client.get("/").text
    assert 'href="https://wa.me/59170000009"' in board and 'target="_blank"' in board and "noopener" in board
    assert board.count("wa.me/") == 1  # el contacto @lid no tiene número, así que no hay enlace
    assert 'href="https://wa.me/59170000009"' in client.get(f"/conversations/{normal}/drawer").text
    drawer = client.get(f"/conversations/{privado}/drawer").text
    assert "wa.me" not in drawer and "Sin número visible" in drawer


def test_chat_url_solo_con_numero_valido():
    assert boards.chat_url("+59170000009") == "https://wa.me/59170000009"
    assert boards.chat_url(None) is None and boards.chat_url("") is None and boards.chat_url("+123") is None


def test_analizar_todo_ahora_solo_toca_los_chats_de_la_cuenta(make_client, monkeypatch):
    from app.core import analysis

    monkeypatch.setattr(config, "GEMINI_API_KEY", "x")
    called = []

    async def fake(factory, conversation_id, analyze=None):
        called.append(conversation_id)
        return True

    monkeypatch.setattr(analysis, "analyze_conversation", fake)
    a = make_client()
    signup(a, email="a@ejemplo.com")
    cid = connect(a)
    mine = [add_chat(a, cid, f"5917000010{i}@s.whatsapp.net", f"Cliente {i}") for i in range(3)]
    b = make_client()
    signup(b, email="b@ejemplo.com")
    assert b.post("/board/analyze").json() == {"ok": True, "analyzed": 0, "failed": 0, "more": False}
    assert called == []
    assert a.post("/board/analyze").json() == {"ok": True, "analyzed": 3, "failed": 0, "more": False}
    assert sorted(called) == sorted(mine)


def test_analizar_todo_cuenta_los_fallos_y_avisa_si_quedan_mas(client, monkeypatch):
    from app.core import analysis

    monkeypatch.setattr(config, "GEMINI_API_KEY", "x")

    async def flaky(factory, conversation_id, analyze=None):
        if conversation_id % 2 == 0:
            raise RuntimeError("503")
        return True

    monkeypatch.setattr(analysis, "analyze_conversation", flaky)
    monkeypatch.setattr("app.web.routes.MANUAL_BATCH", 2)
    signup(client)
    cid = connect(client)
    for i in range(4):
        add_chat(client, cid, f"5917000020{i}@s.whatsapp.net", f"C{i}")
    data = client.post("/board/analyze").json()
    assert data["ok"] and data["analyzed"] + data["failed"] == 2 and data["more"] is True


def test_analizar_todo_sin_clave_responde_503(client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    signup(client)
    assert client.post("/board/analyze").status_code == 503
    assert "Analizar ahora" in client.get("/").text


# --------------------------------------------------------------------------- chats personales
def instance_of(client, conn_id):
    with session_scope(client.app.state.session_factory) as s:
        return s.get(Connection, conn_id).instance_name


def hook(client, instance, jid, msg_id="P1", text="hola", from_me=False):
    return client.post("/webhook/evolution", headers={"x-webhook-token": config.WEBHOOK_TOKEN}, json={
        "event": "messages.upsert", "instance": instance,
        "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id}, "message": {"conversation": text},
                 "messageTimestamp": 1717689097}}).json()


def count(client, model):
    with session_scope(client.app.state.session_factory) as s:
        return s.scalar(select(func.count()).select_from(model))


def test_marcar_personal_borra_los_mensajes_y_descarta_los_siguientes(client):
    signup(client)
    cid = connect(client)
    jid = "59170000009@s.whatsapp.net"
    conv_id = add_chat(client, cid, jid, "Mamá", unanswered=True, summary="algo", suggested_status="resuelta")
    assert count(client, Message) == 1
    r = client.post(f"/conversations/{conv_id}/status", data={"status": "personal"})
    assert r.json() == {"ok": True, "column": "personal"}
    assert count(client, Message) == 0
    with session_scope(client.app.state.session_factory) as s:
        conv = s.get(Conversation, conv_id)
        assert conv.ignored is True and conv.status == "personal" and conv.summary is None and conv.unanswered is False
    assert hook(client, instance_of(client, cid), jid) == {"ok": True, "saved": 0}
    assert count(client, Message) == 0
    page = client.get("/").text
    assert "No se guarda" in page and "Mamá" in page and "Volver a incluir" in page


def test_un_chat_personal_no_se_mueve_a_otro_estado(client):
    signup(client)
    cid = connect(client)
    conv_id = add_chat(client, cid)
    client.post(f"/conversations/{conv_id}/status", data={"status": "personal"})
    r = client.post(f"/conversations/{conv_id}/status", data={"status": "vendida"})
    assert r.status_code == 400 and "Volver a incluir" in r.json()["error"]


def test_volver_a_incluir_permite_guardar_de_nuevo(client):
    signup(client)
    cid = connect(client)
    jid = "59170000009@s.whatsapp.net"
    conv_id = add_chat(client, cid, jid, "Marta")
    client.post(f"/conversations/{conv_id}/status", data={"status": "personal"})
    assert client.post(f"/conversations/{conv_id}/include").json() == {"ok": True}
    assert count(client, Conversation) == 0
    assert hook(client, instance_of(client, cid), jid, msg_id="N1", text="hola otra vez") == {"ok": True, "saved": 1}
    assert count(client, Message) == 1


def test_ignorar_un_numero_antes_de_que_escriba(client):
    signup(client)
    cid = connect(client)
    r = client.post("/ignored", data={"phone": "+591 7000 0777"})
    assert r.json() == {"ok": True, "number": "59170000777"}
    assert hook(client, instance_of(client, cid), "59170000777@s.whatsapp.net") == {"ok": True, "saved": 0}
    assert count(client, Message) == 0
    assert "59170000777" in client.get("/").text
    assert client.post("/ignored", data={"phone": "12"}).status_code == 400


def test_ignorar_un_numero_exige_tener_un_numero_conectado(client):
    signup(client)
    r = client.post("/ignored", data={"phone": "59170000777"})
    assert r.status_code == 400 and "conecta" in r.json()["error"].lower()


def test_la_tarjeta_tiene_el_menu_mover_y_el_detalle_ofrece_personal(client):
    signup(client)
    cid = connect(client)
    conv_id = add_chat(client, cid, suggested_status="vendida")
    assert "data-move" in client.get("/").text
    drawer = client.get(f"/conversations/{conv_id}/drawer").text
    assert 'data-set="personal"' in drawer and "Mover a" in drawer


def test_una_cuenta_no_puede_incluir_chats_de_otra(make_client):
    a = make_client()
    signup(a, email="a@ejemplo.com")
    conv_id = add_chat(a, connect(a))
    b = make_client()
    signup(b, email="b@ejemplo.com")
    assert b.post(f"/conversations/{conv_id}/include").status_code == 404
    assert count(a, Conversation) == 1
