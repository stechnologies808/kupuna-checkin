"""Settings, read from environment variables (or a .env file next to this one)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader so the project needs no extra package for it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(HERE / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Twilio account (from console.twilio.com)
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

    # The public https address Twilio uses to reach this app (your ngrok or Render URL)
    # On Render this is filled in automatically from RENDER_EXTERNAL_URL.
    PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL")
                       or os.environ.get("RENDER_EXTERNAL_URL")
                       or "http://localhost:5000").rstrip("/")

    # DRY_RUN=1 prints calls and texts instead of sending them. Leave on until you're ready.
    DRY_RUN = _bool("DRY_RUN", True)

    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
    DATABASE_PATH = os.environ.get("DATABASE_PATH", str(HERE / "checkin.db"))
    TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Pacific/Honolulu"))
    SERVICE_NAME = os.environ.get("SERVICE_NAME", "Kupuna Check-In")

    # Rules for a morning (minutes)
    MAX_TRIES = int(os.environ.get("MAX_TRIES", 3))
    RETRY_GAP_MIN = int(os.environ.get("RETRY_GAP_MIN", 5))
    NO_RESULT_TIMEOUT_MIN = int(os.environ.get("NO_RESULT_TIMEOUT_MIN", 4))  # treat a call as missed if Twilio never reports back
    FAMILY_REPLY_WINDOW_MIN = int(os.environ.get("FAMILY_REPLY_WINDOW_MIN", 15))
    LATE_START_WINDOW_MIN = int(os.environ.get("LATE_START_WINDOW_MIN", 90))  # don't start a "morning" call hours late
    QUIET_BEFORE_HOUR = int(os.environ.get("QUIET_BEFORE_HOUR", 7))
    QUIET_AFTER_HOUR = int(os.environ.get("QUIET_AFTER_HOUR", 21))
    WEEKLY_SUMMARY_WEEKDAY = int(os.environ.get("WEEKLY_SUMMARY_WEEKDAY", 6))  # Monday=0 ... Sunday=6
    WEEKLY_SUMMARY_HOUR = int(os.environ.get("WEEKLY_SUMMARY_HOUR", 18))

    RING_SECONDS = int(os.environ.get("RING_SECONDS", 25))
