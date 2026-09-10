"""Central configuration.

All values are overridable via environment variables or a local `.env` file.
Nothing here should require code changes to move between dev, paper, and live.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "atr"
    env: str = Field(default="dev", pattern="^(dev|paper|live)$")
    log_level: str = "INFO"

    # ---------------- IIFL Capital ----------------
    iifl_app_key: str = ""
    iifl_app_secret: str = ""
    iifl_redirect_url: str | None = None
    iifl_base_url: str = "https://api.iiflcapital.com/v1"
    iifl_client_id: str = ""
    iifl_timeout: float = 15.0
    iifl_session_cache: str = ".cache/iifl_session.json"
    iifl_default_product: str = "INTRADAY"
    iifl_api_order_source: str = "atr"

    # Market data bridge (MQTT)
    bridge_host: str = "bridge.iiflcapital.com"
    bridge_port: int = 8883

    # ---------------- Database (Postgres + TimescaleDB) ----------------
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "algodb"
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_pool_size: int = 10
    db_echo: bool = False

    # ---------------- Risk ----------------
    max_gross_exposure: float = 5_000_000.0
    max_position_notional: float = 1_000_000.0
    max_daily_loss: float = 50_000.0
    max_orders_per_day: int = 500
    max_open_positions: int = 20
    allow_short: bool = True
    kill_switch_enabled: bool = False
    square_off_time: str = "15:15"

    # ---------------- Backtest defaults ----------------
    bt_initial_cash: float = 1_000_000.0
    bt_commission_per_share: float = 0.005
    bt_commission_per_contract: float = 0.85
    bt_min_commission: float = 1.0
    bt_slippage_bps: float = 5.0
    bt_risk_free_rate: float = 0.05
    bt_fill_on_next_open: bool = True
    bt_equity_margin_ratio: float = 1.0

    # ---------------- API ----------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: str = "change-me"

    # ---------------- Alerts (notify-only) ----------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    fast2sms_api_key: str = ""  # SMS slot: paste key to activate real SMS
    alerts_poll_sec: int = 300

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def is_live(self) -> bool:
        return self.env == "live"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
