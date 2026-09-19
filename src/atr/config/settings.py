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
    #: Pin API egress to IPv4. IIFL whitelists an IPv4 address; on a dual-stack
    #: connection requests otherwise leave over IPv6 and are rejected with
    #: EC500 "IP address not authorized for trading".
    iifl_force_ipv4: bool = True
    #: Market-protection band applied to MARKET orders. SEBI requires a
    #: non-zero value on API market orders since 2026-04-01.
    iifl_market_protection_percent: float = 0.5

    # Market data bridge (MQTT)
    bridge_host: str = "bridge.iiflcapital.com"
    bridge_port: int = 8883
    #: Verify the bridge TLS certificate. The official IIFL BridgePy SDK
    #: bypasses verification, so the default is False for compatibility — but
    #: enabling it is recommended for any non-local deployment.
    bridge_tls_verify: bool = False

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

    # ---------------- Application store (control plane) ----------------
    #: Users, sessions, watchlists, audit. SQLite by default so a fresh clone
    #: can log in with nothing installed; point at Postgres to share state.
    app_db_url: str = "sqlite:///data/app.db"
    #: Where nightly backups go. Point it at another disk or a synced folder: a copy
    #: on the same drive does not survive that drive failing.
    backup_dir: str = "data/backups"
    #: How many of the newest backups to keep.
    backup_keep: int = 14
    #: Self-registration. Off by default — a trading platform should not accept
    #: strangers. The first account is created through the bootstrap route.
    allow_signup: bool = False
    #: Session lifetime. A trading session is long, so this is generous.
    session_ttl_hours: int = 24 * 14
    #: Require a session for the API. Loopback requests in ``dev`` are exempt
    #: while no account exists yet, so the single-operator workflow keeps working.
    auth_required: bool = True
    #: Key material for encrypting secrets at rest (broker credentials, TOTP
    #: seeds). Read from ``ATR_SECRET_KEY``. Empty means a local key file is
    #: generated under ``data/``.
    atr_secret_key: str = ""
    login_max_attempts: int = 8
    login_lockout_minutes: int = 15
    #: Require a second factor for admin/owner accounts. Off by default so a
    #: fresh install is usable; turn it on for anything reachable off-host.
    require_mfa_for_privileged: bool = False
    rate_limit_per_minute: int = 240
    rate_limit_login_per_minute: int = 10

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
