"""Datos de demostración para ver el tablero. SOLO desarrollo local: crea una base SQLite aparte.

    python scripts/seed_demo.py            ->  data/demo.db  (cuenta demo@demo.local / demo-demo-1)

No toca la base de producción: ignora DATABASE_URL a propósito.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Account, Connection, Conversation, Message, init_db, make_engine, make_session_factory, now_utc, session_scope  # noqa: E402
from app.web.auth import hash_password  # noqa: E402

DB = os.getenv("DEMO_DB", "sqlite:///./data/demo.db")
MIN, H, D = 1, 60, 1440

# (nombre, teléfono, mensajes[(quién, texto, hace_min)], estado_confirmado, estado_sugerido, resumen, acción, seguimiento_en_min, monto, sin_responder)
CHATS = [
    ("Marta Quispe", "59170011001", [("in", "Hola, ¿tienen el sistema de riego para media hectárea?", 9), ("in", "Necesito el precio con instalación", 8)], None, None, None, None, None, None, True),
    ("Luis Rojas", "59170011002", [("in", "Buenas tardes, vi su anuncio de las zapatillas", 14), ("out", "¡Hola Luis! Sí, tenemos en talla 40 y 41", 12), ("in", "Perfecto, ¿cuánto cuestan?", 5)], None, None, None, None, None, None, True),
    ("Carla Soria", "59170011003", [("out", "Te confirmo que mañana salen tus 3 cajas", 6), ("in", "Genial, muchas gracias", 4)], None, None, None, None, None, None, False),

    ("Ana Mamani", "59170011004", [("in", "Quisiera cotizar una página web para mi restaurante", 2 * H + 40), ("out", "Claro, te envío una propuesta hoy", 2 * H + 30), ("in", "Ok, quedo atenta", 2 * H + 20)], None, "pendiente_seguimiento", "Pidió cotización de una web para su restaurante; quedó en recibir la propuesta.", "Enviar la propuesta de la página web", -90, None, False),
    ("Diego Vargas", "59170011005", [("in", "¿Me hacen descuento si compro 10 unidades?", 5 * H), ("out", "Lo consulto con mi jefe y te respondo", 5 * H - 5)], "pendiente_seguimiento", "pendiente_seguimiento", "Pide descuento por 10 unidades; el negocio debe confirmarlo.", "Responder con el descuento aprobado", -300, None, False),
    ("Rosa Choque", "59170011006", [("in", "Mi pedido no llegó y ya pasaron 5 días", 3 * H), ("in", "¿Alguien me puede decir algo?", 2 * H + 50)], "pendiente_seguimiento", "pendiente_seguimiento", "Reclama un pedido que no llegó en 5 días.", "Revisar con logística y avisarle", 120, None, True),
    ("Jorge Álvarez", "59170011007", [("out", "El plan básico cuesta 120 Bs al mes", 1 * D + 2 * H), ("in", "Lo voy a pensar y te aviso", 1 * D + H)], None, "pendiente_seguimiento", "Evalúa el plan básico de 120 Bs al mes; decidirá y avisará.", "Escribirle en dos días si no responde", 2 * D, None, False),
    ("Elena Cruz", "59170011008", [("in", "Mañana te transfiero los 450 por las 3 sesiones", 1 * D + 40), ("out", "Ok, te espero", 1 * D + 30)], "pendiente_seguimiento", "pendiente_seguimiento", "Quedó en transferir 450 Bs por 3 sesiones mañana.", "Confirmar el pago cuando llegue", 20 * H, 450, False),

    ("Paola Rivas", "59170011009", [("in", "Quiero una limpieza dental", 1 * D), ("out", "Tenemos el martes a las 15:00", 1 * D - 5), ("in", "Perfecto, ahí estaré", 1 * D - 10)], "cita_agendada", "cita_agendada", "Limpieza dental agendada para el martes a las 15:00.", "Recordarle la cita el día anterior", 2 * D + 6 * H, None, False),
    ("Tomás Ibáñez", "59170011010", [("in", "Me interesa el departamento de la calle Bolívar", 6 * H), ("out", "Puede verlo el sábado a las 10:00", 5 * H), ("in", "Sí, nos vemos ese día", 4 * H + 30)], None, "cita_agendada", "Visita al departamento de la calle Bolívar el sábado a las 10:00.", "Confirmar la visita el viernes", 4 * D, None, False),
    ("Sofía Medina", "59170011011", [("in", "Necesito revisar los frenos de mi auto", 20 * H), ("out", "Puede traerlo hoy a las 17:00", 19 * H), ("in", "Voy a esa hora", 19 * H - 10)], "cita_agendada", "cita_agendada", "Revisión de frenos hoy a las 17:00.", "Tener listo el diagnóstico", 3 * H, None, False),

    ("Daniela Flores", "59170011012", [("in", "Quiero 2 cajas de miel de 500g", 3 * D), ("out", "Son 160 Bs en total", 3 * D - 10), ("in", "Listo, ya hice la transferencia", 3 * D - 20), ("out", "Recibido, mañana te las despachamos", 3 * D - 30)], "vendida", "vendida", "Compró 2 cajas de miel y pagó por transferencia.", None, None, 160, False),
    ("Pablo Suárez", "59170011013", [("in", "Me inscribo al curso de Excel", 4 * D), ("out", "Son 300 Bs", 4 * D - 5), ("in", "Pagué por QR", 4 * D - 15), ("out", "Inscrito, bienvenido", 4 * D - 20)], "vendida", "vendida", "Se inscribió al curso de Excel y pagó 300 Bs por QR.", None, None, 300, False),
    ("Mónica Salas", "59170011014", [("in", "Quiero una torta de chocolate para el sábado", 2 * D + 3 * H), ("out", "Son 220 Bs con adelanto del 50%", 2 * D + 2 * H), ("in", "Te deposité 110 Bs", 2 * D + H), ("out", "Pedido confirmado", 2 * D)], None, "vendida", "Torta de chocolate para el sábado; pagó el adelanto de 110 Bs.", "Entregar el sábado y cobrar el resto", None, 220, False),
    ("Iván Pinto", "59170011015", [("in", "¿Tienen filtro de aceite para Honda 150?", 5 * D), ("out", "Sí, 45 Bs cada uno", 5 * D - 3), ("in", "Dame dos, ya pagué por Tigo Money", 5 * D - 9)], "vendida", "vendida", "Compró 2 filtros de aceite y pagó 90 Bs por Tigo Money.", None, None, 90, False),

    ("Karen Torrez", "59170011016", [("in", "¿A qué hora abren los sábados?", 6 * D), ("out", "De 9:00 a 13:00", 6 * D - 2), ("in", "Gracias", 6 * D - 4)], "resuelta", "resuelta", "Consultó el horario de los sábados y quedó atendida.", None, None, None, False),
    ("Andrés Poma", "59170011017", [("in", "No me abre la aplicación", 7 * D), ("out", "Pruebe desinstalarla y volver a instalarla", 7 * D - 3), ("in", "Ya funcionó, gracias", 7 * D - 9)], None, "resuelta", "Problema con la app resuelto al reinstalarla.", None, None, None, False),
    ("Lucía Antezana", "59170011018", [("in", "¿Hacen envíos a Cochabamba?", 8 * D), ("out", "Sí, cuesta 25 Bs", 8 * D - 2), ("in", "Entendido, gracias por la información", 8 * D - 6)], "resuelta", "resuelta", "Preguntó por envíos a Cochabamba (25 Bs).", None, None, None, False),

    ("Fabián Limachi", "59170011019", [("out", "El servicio completo cuesta 900 Bs", 9 * D), ("in", "Es muy caro, ya no me interesa", 9 * D - 20)], "perdida", "perdida", "Rechazó el servicio de 900 Bs por el precio.", None, None, None, False),
    ("Verónica Lara", "59170011020", [("out", "Te mando la propuesta final", 10 * D), ("in", "Encontré otra empresa más barata, gracias", 10 * D - 15)], None, "perdida", "Eligió a otra empresa con un precio menor.", None, None, None, False),
]


def main() -> None:
    engine = make_engine(DB)
    init_db(engine)
    factory = make_session_factory(engine)
    now = now_utc()
    with session_scope(factory) as s:
        for model in (Message, Conversation, Connection, Account):
            s.execute(model.__table__.delete())
        acc = Account(email="demo@demo.local", password_hash=hash_password("demo-demo-1"), name="Kevin")
        s.add(acc)
        s.flush()
        conn = Connection(account_id=acc.id, instance_name="demo", phone="59160259580", state="open", connected_at=now)
        s.add(conn)
        s.flush()
        for name, phone, msgs, status, suggested, summary, action, follow, amount, waiting in CHATS:
            last_min = min(m[2] for m in msgs)
            conv = Conversation(
                connection_id=conn.id, remote_jid=f"{phone}@s.whatsapp.net", customer_name=name,
                summary=summary, status=status, suggested_status=suggested, next_action=action, amount=amount,
                follow_up_at=(now + timedelta(minutes=follow)) if follow is not None else None,
                unanswered=waiting, done=bool(status in ("vendida", "resuelta", "perdida") and not waiting),
                last_message_at=now - timedelta(minutes=last_min),
                last_analyzed_at=(now - timedelta(minutes=max(last_min - 12, 0))) if suggested else None,
            )
            s.add(conv)
            s.flush()
            for i, (who, text, ago) in enumerate(sorted(msgs, key=lambda m: -m[2])):
                s.add(Message(conversation_id=conv.id, wa_message_id=f"demo-{conv.id}-{i}", direction=who,
                              text=text, type="text", sent_at=now - timedelta(minutes=ago)))
    print(f"Datos de demostración creados en {DB}. Cuenta: demo@demo.local / demo-demo-1")


if __name__ == "__main__":
    main()
