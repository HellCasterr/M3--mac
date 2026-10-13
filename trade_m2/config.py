from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    upstox_api_key: str
    upstox_api_secret: str
    upstox_redirect_url: str
    host: str
    port: int
    database_path: Path
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_ids: tuple[str, ...] = ()
    telegram_enabled: bool = True

    @property
    def upstox_configured(self) -> bool:
        return bool(self.upstox_api_key and self.upstox_api_secret)

    @property
    def telegram_error(self) -> str | None:
        if not self.telegram_enabled:
            return "Telegram is disabled in .env."
        if not self.telegram_bot_token:
            return "Add TELEGRAM_BOT_TOKEN to .env and restart Trade M3."
        if not self.telegram_chat_ids:
            return "Add two comma-separated chat IDs to TELEGRAM_CHAT_IDS and restart Trade M3."
        if any(
            not re.fullmatch(r"-?[1-9][0-9]{0,18}", chat_id)
            for chat_id in self.telegram_chat_ids
        ):
            return "Use numeric Telegram chat IDs, not phone numbers or personal @usernames."
        return None


def get_settings() -> Settings:
    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        upstox_api_key=os.getenv("UPSTOX_API_KEY", "").strip(),
        upstox_api_secret=os.getenv("UPSTOX_API_SECRET", "").strip(),
        upstox_redirect_url=os.getenv(
            "UPSTOX_REDIRECT_URL", "http://127.0.0.1:8000/auth/upstox/callback"
        ).strip(),
        host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        port=int(os.getenv("APP_PORT", "8000")),
        database_path=Path(
            os.getenv("DATABASE_PATH", str(data_dir / "trade_m3.db"))
        ),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_ids=tuple(dict.fromkeys(
            value.strip()
            for value in (
                os.getenv("TELEGRAM_CHAT_IDS", "").strip()
                or os.getenv("TELEGRAM_CHAT_ID", "")
            ).split(",")
            if value.strip()
        )),
        telegram_enabled=os.getenv("TELEGRAM_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    )
