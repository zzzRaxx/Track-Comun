from sqlalchemy import inspect, text

from app.db import init_db, make_engine


def test_una_base_existente_recibe_la_columna_ignored(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'vieja.db'}")
    with engine.begin() as conn:  # una tabla creada antes de existir la columna
        conn.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, connection_id INTEGER, remote_jid TEXT)"))
        conn.execute(text("INSERT INTO conversations (id, connection_id, remote_jid) VALUES (1, 1, 'x@s.whatsapp.net')"))
    init_db(engine)
    assert "ignored" in {c["name"] for c in inspect(engine).get_columns("conversations")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT ignored FROM conversations WHERE id = 1")).scalar() in (0, False)
    init_db(engine)  # repetirlo no falla
    engine.dispose()
