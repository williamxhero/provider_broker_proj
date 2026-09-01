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
    health_stale_seconds: int = 30 * 60
    probe_timeout_ms: int = 15_000
    probe_concurrency: int = 2
    health_scheduler_seconds: int = 60
    first_event_timeout_ms: int = 20_000
    route_attempt_budget: int = 32
    balance_scheduler_seconds: int = 15 * 60

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
            health_stale_seconds=int(os.getenv("BROKER_HEALTH_STALE_SECONDS", str(30 * 60))),
            probe_timeout_ms=int(os.getenv("BROKER_PROBE_TIMEOUT_MS", "15000")),
            probe_concurrency=int(os.getenv("BROKER_PROBE_CONCURRENCY", "2")),
            health_scheduler_seconds=int(os.getenv("BROKER_HEALTH_SCHEDULER_SECONDS", "60")),
            first_event_timeout_ms=int(os.getenv("BROKER_FIRST_EVENT_TIMEOUT_MS", "20000")),
            route_attempt_budget=int(os.getenv("BROKER_ROUTE_ATTEMPT_BUDGET", "32")),
            balance_scheduler_seconds=int(os.getenv("BROKER_BALANCE_SCHEDULER_SECONDS", str(15 * 60))),
        )

    def key_bytes(self) -> bytes:
        value = base64.b64decode(self.encryption_key)
        if len(value) not in (16, 24, 32):
            raise ValueError("BROKER_ENCRYPTION_KEY must decode to an AES key")
        return value
