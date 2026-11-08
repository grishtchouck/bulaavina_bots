import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    db_path: str
    scenario_path: str
    # Prodamus
    prodamus_url: str          # https://yourshop.payform.ru
    prodamus_secret: str       # секретный ключ из настроек Prodamus
    prodamus_sign_links: bool  # подписывать ссылки на оплату (если включено в Prodamus)
    public_url: str            # https://bot.example.com — адрес, по которому доступен сервер
    web_host: str
    web_port: int


def load() -> Config:
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
        db_path=os.getenv("DB_PATH", str(ROOT / "bot.db")),
        scenario_path=os.getenv("SCENARIO_PATH", str(ROOT / "scenario.yaml")),
        prodamus_url=os.getenv("PRODAMUS_URL", "").rstrip("/"),
        prodamus_secret=os.getenv("PRODAMUS_SECRET", ""),
        prodamus_sign_links=os.getenv("PRODAMUS_SIGN_LINKS", "0") == "1",
        public_url=os.getenv("PUBLIC_URL", "").rstrip("/"),
        web_host=os.getenv("WEB_HOST", "0.0.0.0"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
    )
