from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import config, llm
from app.channels.evolution import IncomingMessage
from app.core import analysis, ingest
from app.db import Connection, Conversation, now_utc, session_scope

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
JID = "59170000001@s.whatsapp.net"


def msg(id_, text, at, from_me=False, name="Ana"):
    return IncomingMessage("inst1", JID, id_, from_me, text, "text", at, name)


def result(**kw):
    base = dict(cliente="Ana", resumen="Pidió precio y se le cotizó.", estado="pendiente_seguimiento",
                pregunta_sin_responder=False, monto=None, fecha_seguimiento=None, accion_siguiente="Enviar la cotización")
    base.update(kw)
    return llm.ChatAnalysis(**base)


class Fake:
    """Reemplaza a llm.analyze y registra lo que recibe."""
    def __init__(self, res=None, error=None):
        self.res, self.error, self.calls = res or result(), error, []

    async def __call__(self, transcript, now_local):
        self.calls.append((transcript, now_local))
        if self.error:
            raise self.error
        return self.res


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "LOCAL_TZ", ZoneInfo("America/La_Paz"))


def seed(factory, connection, messages):
    with session_scope(factory) as s:
        conn = s.get(Connection, connection)
        for m in messages:
            ingest.ingest_message(s, conn, m)


def conv_of(factory):
    with session_scope(factory) as s:
        return s.scalar(select(Conversation))


@pytest.mark.asyncio
async def test_analiza_una_conversacion_con_silencio(factory, connection):
    seed(factory, connection, [msg("A", "Hola, ¿precio del plan?", T0), msg("B", "Son 120 Bs", T0 + timedelta(minutes=1), True)])
    fake = Fake(result(estado="cita_agendada", monto=120.0, fecha_seguimiento="2026-11-03T15:00"))
    assert await analysis.analysis_cycle(factory, fake) == 1
    c = conv_of(factory)
    assert c.suggested_status == "cita_agendada" and c.status is None  # la IA solo sugiere
    assert c.summary and c.amount == 120.0 and c.last_analyzed_at is not None
    assert c.follow_up_at == datetime(2026, 11, 3, 19, 0, tzinfo=timezone.utc)  # 15:00 hora de La Paz (UTC-4)


@pytest.mark.asyncio
async def test_la_transcripcion_marca_quien_habla_y_la_hora_local(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0), msg("B", "Buenas, dime", T0 + timedelta(minutes=1), True)])
    fake = Fake()
    await analysis.analysis_cycle(factory, fake)
    transcript, now_local = fake.calls[0]
    assert transcript.splitlines() == ["[01/11 06:00] Cliente: Hola", "[01/11 06:01] Negocio: Buenas, dime"]
    assert now_local.tzinfo is not None


@pytest.mark.asyncio
async def test_no_analiza_mientras_hay_actividad_reciente(factory, connection):
    seed(factory, connection, [msg("A", "Hola", now_utc() - timedelta(minutes=2))])
    fake = Fake()
    assert await analysis.analysis_cycle(factory, fake) == 0 and fake.calls == []


@pytest.mark.asyncio
async def test_no_repite_si_no_hay_mensajes_nuevos(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0)])
    fake = Fake()
    await analysis.analysis_cycle(factory, fake)
    assert await analysis.analysis_cycle(factory, fake) == 0 and len(fake.calls) == 1


@pytest.mark.asyncio
async def test_un_mensaje_nuevo_vuelve_a_ponerla_en_cola(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0)])
    fake = Fake()
    await analysis.analysis_cycle(factory, fake)
    with session_scope(factory) as s:  # el análisis ocurrió hace una hora
        s.scalar(select(Conversation)).last_analyzed_at = now_utc() - timedelta(hours=1)
    seed(factory, connection, [msg("B", "¿Sigue disponible?", now_utc() - timedelta(minutes=30))])
    assert await analysis.analysis_cycle(factory, fake) == 1 and len(fake.calls) == 2
    assert "¿Sigue disponible?" in fake.calls[1][0]


@pytest.mark.asyncio
async def test_pregunta_sin_responder_exige_que_el_cliente_hablara_ultimo(factory, connection):
    seed(factory, connection, [msg("A", "¿Tienen talla M?", T0), msg("B", "Sí, hay", T0 + timedelta(minutes=1), True)])
    await analysis.analysis_cycle(factory, Fake(result(pregunta_sin_responder=True)))
    assert conv_of(factory).unanswered is False  # el negocio ya respondió, aunque el modelo diga lo contrario


@pytest.mark.asyncio
async def test_el_cliente_que_agradece_no_deja_pendiente(factory, connection):
    seed(factory, connection, [msg("A", "¿Horario?", T0), msg("B", "9 a 13", T0 + timedelta(minutes=1), True),
                               msg("C", "Gracias", T0 + timedelta(minutes=2))])
    assert conv_of(factory).unanswered is True  # la regla simple del guardado diría pendiente
    await analysis.analysis_cycle(factory, Fake(result(estado="resuelta", pregunta_sin_responder=False)))
    assert conv_of(factory).unanswered is False


@pytest.mark.asyncio
async def test_un_fallo_no_detiene_a_las_demas_y_se_reintenta_mas_tarde(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0)])
    failures: dict = {}
    fake = Fake(error=RuntimeError("cuota agotada"))
    assert await analysis.analysis_cycle(factory, fake, failures) == 0
    assert len(failures) == 1 and conv_of(factory).last_analyzed_at is None
    await analysis.analysis_cycle(factory, fake, failures)
    assert len(fake.calls) == 1  # en espera: no se vuelve a intentar enseguida
    failures[next(iter(failures))] = now_utc() - timedelta(seconds=1)
    ok = Fake()
    assert await analysis.analysis_cycle(factory, ok, failures) == 1 and failures == {}


@pytest.mark.asyncio
async def test_el_nombre_del_modelo_completa_el_del_cliente_solo_si_falta(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0, name=None)])
    await analysis.analysis_cycle(factory, Fake(result(cliente="Marta Pérez")))
    assert conv_of(factory).customer_name == "Marta Pérez"


def test_transcripcion_larga_conserva_el_final(monkeypatch):
    monkeypatch.setattr(config, "MAX_TRANSCRIPT_CHARS", 60)
    from app.db import Message
    from zoneinfo import ZoneInfo
    msgs = [Message(direction="in", text=f"mensaje número {i}", sent_at=T0) for i in range(20)]
    text = analysis.build_transcript(msgs, ZoneInfo("UTC"))
    assert text.startswith("[...conversación recortada...]") and "número 19" in text and "número 0" not in text


@pytest.mark.asyncio
async def test_no_analiza_chats_personales(factory, connection):
    seed(factory, connection, [msg("A", "Hola", T0)])
    with session_scope(factory) as s:
        s.scalar(select(Conversation)).ignored = True
    fake = Fake()
    assert await analysis.analysis_cycle(factory, fake) == 0 and fake.calls == []
    assert await analysis.analyze_conversation(factory, 1, fake) is False
