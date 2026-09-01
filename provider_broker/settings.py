from dataclasses import dataclass
from pathlib import Path
import base64
import os


@dataclass(frozen=True)
class Settings:
    database_path: Path
    admin_token: str
    session_secret: str
    encryption_key: str
    cpa_url: str = "http://127.0.0.1:8317"
    cpa_token: str = ""
    parallel_cap: int = 3
    hedge_delay_ms: int = 750
    first_event_timeout_ms: int = 30_000
    stream_idle_timeout_ms: int = 90_000
    attempt_timeout_ms: int = 180_000
    route_attempt_budget: int = 32
    response_reserve_ms: int = 5_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.getenv("BROKER_DB_PATH", "/data/provider-broker/data/broker.sqlite3")),
            admin_token=os.environ["BROKER_ADMIN_TOKEN"],
            session_secret=os.environ["BROKER_SESSION_SECRET"],
            encryption_key=os.environ["BROKER_ENCRYPTION_KEY"],
            cpa_url=os.getenv("CPA_URL", "http://127.0.0.1:8317"),
            cpa_token=os.getenv("CPA_MANAGEMENT_KEY", ""),
            parallel_cap=int(os.getenv("BROKER_PARALLEL_CAP", "3")),
            hedge_delay_ms=int(os.getenv("BROKER_HEDGE_DELAY_MS", "750")),
            first_event_timeout_ms=int(os.getenv("BROKER_FIRST_EVENT_TIMEOUT_MS", "30000")),
            stream_idle_timeout_ms=int(os.getenv("BROKER_STREAM_IDLE_TIMEOUT_MS", "90000")),
            attempt_timeout_ms=int(os.getenv("BROKER_ATTEMPT_TIMEOUT_MS", "180000")),
            route_attempt_budget=int(os.getenv("BROKER_ROUTE_ATTEMPT_BUDGET", "32")),
            response_reserve_ms=int(os.getenv("BROKER_RESPONSE_RESERVE_MS", "5000")),
        )

    def key_bytes(self) -> bytes:
        value = base64.b64decode(self.encryption_key)
        if len(value) not in (16, 24, 32):
            raise ValueError("BROKER_ENCRYPTION_KEY must decode to an AES key")
        return value
