import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Connection, Account, Message, session_scope
from app.main import create_app

TOKEN = "token-de-prueba"  # el mismo que fija conftest.py


@pytest.fixture
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'w.db'}", validate=False, run_analysis=False)
    with TestClient(app) as c:
        with session_scope(app.state.session_factory) as s:
            acc = Account(email="a@b.c", password_hash="x")
            s.add(acc)
            s.flush()
            s.add(Connection(account_id=acc.id, instance_name="inst1", state="creating"))
        c.app_state = app.state
        yield c


def upsert(msg_id="ID1", text="hola", instance="inst1"):
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {"remoteJid": "59170000001@s.whatsapp.net", "fromMe": False, "id": msg_id},
            "pushName": "Ana",
            "message": {"conversation": text},
            "messageTimestamp": 1717689097,
        },
    }


def n_messages(client):
    with session_scope(client.app_state.session_factory) as s:
        return s.scalar(select(func.count()).select_from(Message))


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_rechaza_token_invalido(client):
    assert client.post("/webhook/evolution?token=mal", json=upsert()).status_code == 401
    assert client.post("/webhook/evolution", json=upsert()).status_code == 401


def test_evento_repetido_deja_una_sola_fila(client):
    url = f"/webhook/evolution?token={TOKEN}"
    assert client.post(url, json=upsert()).json() == {"ok": True, "saved": 1}
    assert client.post(url, json=upsert()).json() == {"ok": True, "saved": 0}
    assert n_messages(client) == 1


def test_token_en_cabecera(client):
    assert client.post("/webhook/evolution", json=upsert(), headers={"x-webhook-token": "mal"}).status_code == 401
    r = client.post("/webhook/evolution", json=upsert(), headers={"x-webhook-token": TOKEN})
    assert r.json() == {"ok": True, "saved": 1}


def test_instancia_desconocida_se_ignora_con_200(client):
    r = client.post(f"/webhook/evolution?token={TOKEN}", json=upsert(instance="nadie"))
    assert r.status_code == 200 and n_messages(client) == 0


def test_connection_update_y_qr(client):
    url = f"/webhook/evolution?token={TOKEN}"
    client.post(url, json={"event": "qrcode.updated", "instance": "inst1", "data": {}})
    client.post(url, json={"event": "CONNECTION_UPDATE", "instance": "inst1", "data": {"state": "open"}})
    with session_scope(client.app_state.session_factory) as s:
        assert s.scalar(select(Connection.state)) == "open"
