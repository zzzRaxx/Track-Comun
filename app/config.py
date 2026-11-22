import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Sin DATABASE_URL solo se usa SQLite en desarrollo; validate() la exige al arrancar el servicio.
DATABASE_URL_DEFINED = bool(os.getenv("DATABASE_URL"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

# Evolution API (solo accesible por la red privada de Railway)
EVOLUTION_URL = os.getenv("EVOLUTION_URL", "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
# Token que Evolution envía en la URL del webhook para identificarse ante el backend
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
# URL pública del backend, usada para armar enlaces (por ejemplo en los avisos de Telegram)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
# URL con la que Evolution llega al backend (red privada de Railway). Por defecto, PUBLIC_URL.
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or PUBLIC_URL).rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# Modelo al que se recurre si el principal está saturado (503) o limitado (429). Vacío = sin respaldo.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")

TZ_NAME = os.getenv("TZ", "UTC")
# Moneda que se muestra junto a los montos de las ventas
CURRENCY = os.getenv("CURRENCY", "Bs")
SILENCE_MINUTES = int(os.getenv("SILENCE_MINUTES", "10"))
REMINDER_INTERVAL_MIN = int(os.getenv("REMINDER_INTERVAL_MIN", "15"))
REMINDER_COOLDOWN_MIN = int(os.getenv("REMINDER_COOLDOWN_MIN", "120"))
MAX_TRANSCRIPT_CHARS = 24000

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("UTC")


def validate() -> None:
    """Comprueba las variables imprescindibles para arrancar el servicio."""
    missing = [
        n
        for n, v in (
            ("DATABASE_URL", "x" if DATABASE_URL_DEFINED else ""),
            ("EVOLUTION_URL", EVOLUTION_URL),
            ("EVOLUTION_API_KEY", EVOLUTION_API_KEY),
            ("WEBHOOK_TOKEN", WEBHOOK_TOKEN),
            ("SESSION_SECRET", SESSION_SECRET),
        )
        if not v
    ]
    if missing:
        raise RuntimeError("Faltan variables de entorno: " + ", ".join(missing))
