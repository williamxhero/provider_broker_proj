import hashlib
import hmac
import json
import sqlite3
from importlib.metadata import PackageNotFoundError, version
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog import blended_price


TELEMETRY_SCHEMA_VERSION = 1
ROUTING_POLICY_VERSION = "v1"
DELIVERY_MODES = {"non_stream", "plain_stream", "validated_stream"}


def broker_release_version() -> str:
    """Return an installed release when packaged, with a stable source fallback."""
    try:
        return version("provider-broker")
    except PackageNotFoundError:
        return "0.2.0-dev"


def _bucket(value: object, bounds: tuple[tuple[int, str], ...], unknown: str = "unknown") -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return unknown
    for maximum, label in bounds:
        if value <= maximum:
            return label
    return bounds[-1][1]


def request_shape(body: dict | None) -> dict[str, str]:
    """Return only bounded cohorts.  Request content never leaves this function."""
    body = body or {}
    prompt = body.get("prompt")
    length = len(prompt) if isinstance(prompt, str) else None
    schema = body.get("output_schema")
    schema_family = schema.get("type", "unknown") if isinstance(schema, dict) else "none"
    if not isinstance(schema_family, str) or len(schema_family) > 32:
        schema_family = "unknown"
    return {
        "input_bucket": _bucket(length, ((0, "empty"), (256, "1-256"), (2048, "257-2048"), (8192, "2049-8192"), (10**12, "8193+"))),
        "output_budget_bucket": _bucket(body.get("output_token_limit"), ((0, "0"), (256, "1-256"), (1024, "257-1024"), (4096, "1025-4096"), (10**12, "4097+"))),
        "deadline_bucket": _bucket(body.get("deadline_ms"), ((999, "under-1s"), (4999, "1s-5s"), (29999, "5s-30s"), (119999, "30s-120s"), (10**12, "120s+"))),
        "schema_family": schema_family,
    }


@dataclass(frozen=True)
class Provider:
    id: int
    fingerprint: str
    name: str
    base_url: str
    api_key: str
    provider_type: str
    request_headers: dict[str, str]
    models: list[str]
    pricing: dict[str, object]
    price_group: int
    max_parallel: int
    enabled: bool
    multiplier: float
    site_id: str = "default"
    wire_model: str | None = None
    model_aliases: dict[str, str] | None = None


class Store:
    def __init__(self, path: Path, encryption_key: bytes, default_race_parallel_cap: int = 3):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.aes = AESGCM(encryption_key)
        self._inflight: dict[str, int] = {}
        self._site_inflight: dict[str, int] = {}
        self._global_inflight = 0
        self.default_race_parallel_cap = default_race_parallel_cap
        self._migrate()

    def _migrate(self):
        catalog_exists = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_catalog'").fetchone() is not None
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS source_provider (
          id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          base_url TEXT NOT NULL, api_key BLOB NOT NULL, provider_type TEXT NOT NULL,
          request_headers BLOB, api_key_mask TEXT NOT NULL DEFAULT '***',
          models_json TEXT NOT NULL, source_json TEXT NOT NULL, synced_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy (
          fingerprint TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
          price_group INTEGER NOT NULL DEFAULT 100, multiplier REAL NOT NULL DEFAULT 1.0, calibrated INTEGER NOT NULL DEFAULT 0, tiers_json TEXT NOT NULL DEFAULT '["standard","smart","expert"]'
        );
        CREATE TABLE IF NOT EXISTS observation (
          id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, requested_model TEXT NOT NULL,
          actual_model TEXT, tier TEXT NOT NULL, effort TEXT, success INTEGER NOT NULL,
          latency_ms REAL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS model_catalog (
          model TEXT PRIMARY KEY, family TEXT NOT NULL, intellect TEXT NOT NULL,
          input_price REAL NOT NULL, cache_price REAL NOT NULL, output_price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS route_block (fingerprint TEXT NOT NULL, model TEXT NOT NULL, blocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(fingerprint,model));
        CREATE TABLE IF NOT EXISTS broker_setting (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_health (
          fingerprint TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unknown',
          consecutive_failures INTEGER NOT NULL DEFAULT 0, backoff_level INTEGER NOT NULL DEFAULT 0,
          last_real_attempt TEXT, last_real_success TEXT, last_probe_at TEXT, next_probe_at TEXT,
          last_route_recovery_at TEXT,
          smoothed_success REAL, smoothed_ttft_ms REAL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(fingerprint, model)
        );
        CREATE TABLE IF NOT EXISTS probe_event (
          id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, model TEXT NOT NULL, tier TEXT NOT NULL,
          mode TEXT NOT NULL, reachable INTEGER NOT NULL, responded INTEGER NOT NULL,
          first_token INTEGER NOT NULL, model_matched INTEGER NOT NULL, ttfb_ms REAL,
          ttft_ms REAL, duration_ms REAL, error_type TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS provider_health_due ON provider_health(next_probe_at);
        CREATE INDEX IF NOT EXISTS probe_event_target ON probe_event(fingerprint, model, id DESC);
        CREATE TABLE IF NOT EXISTS balance_site (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, adapter TEXT NOT NULL, base_url TEXT NOT NULL,
          currency TEXT NOT NULL, low_threshold REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          credential BLOB, last_balance REAL, last_checked_at TEXT, last_error TEXT,
          low_alert_active INTEGER NOT NULL DEFAULT 0, last_notification_at TEXT, notification_error TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_snapshot (
          id INTEGER PRIMARY KEY, site_id TEXT NOT NULL REFERENCES balance_site(id), balance REAL NOT NULL,
          checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS balance_setting (name TEXT PRIMARY KEY, value BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS route_run (
          route_id TEXT PRIMARY KEY, tier TEXT NOT NULL, effort TEXT, request_id TEXT NOT NULL,
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
          outcome TEXT, first_delta_ms REAL, completed_ms REAL, selected_fingerprint TEXT,
          selected_model TEXT, selected_site_id TEXT, terminal_reason TEXT,
          telemetry_version INTEGER, delivery_mode TEXT, release_version TEXT,
          routing_policy_version TEXT, configuration_fingerprint TEXT,
          shape_version INTEGER, input_bucket TEXT, output_budget_bucket TEXT,
          deadline_bucket TEXT, schema_family TEXT, experiment_id TEXT, experiment_arm TEXT,
          first_attempt_ms REAL, capacity_wait_ms REAL, first_response_header_ms REAL,
          first_text_ms REAL, validation_completed_ms REAL
        );
        CREATE INDEX IF NOT EXISTS route_run_started ON route_run(started_at DESC);
        CREATE TABLE IF NOT EXISTS route_candidate (
          route_id TEXT NOT NULL REFERENCES route_run(route_id), ordinal INTEGER NOT NULL,
          fingerprint TEXT, model TEXT, site_id TEXT, eligible INTEGER NOT NULL,
          exclusion_reason TEXT, initial_rank INTEGER, launched INTEGER NOT NULL DEFAULT 0,
          role TEXT, PRIMARY KEY(route_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS route_candidate_route ON route_candidate(route_id, ordinal);
        CREATE TABLE IF NOT EXISTS route_attempt (
          route_id TEXT NOT NULL REFERENCES route_run(route_id), attempt_number INTEGER NOT NULL,
          fingerprint TEXT, model TEXT, site_id TEXT, role TEXT NOT NULL, status TEXT,
          failure_class TEXT, started_ms REAL, elapsed_ms REAL, PRIMARY KEY(route_id, attempt_number)
        );
        CREATE INDEX IF NOT EXISTS route_attempt_route ON route_attempt(route_id, attempt_number);
        CREATE TABLE IF NOT EXISTS configuration_event (
          id INTEGER PRIMARY KEY, setting TEXT NOT NULL, before_value TEXT, after_value TEXT NOT NULL,
          source TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS configuration_event_time ON configuration_event(created_at DESC);
        CREATE TABLE IF NOT EXISTS client_telemetry (
          route_id TEXT NOT NULL REFERENCES route_run(route_id), metric_type TEXT NOT NULL,
          elapsed_ms REAL NOT NULL, client_family TEXT NOT NULL, client_version TEXT NOT NULL,
          telemetry_version INTEGER NOT NULL, received_at TEXT NOT NULL,
          PRIMARY KEY(route_id, metric_type, client_family, client_version)
        );
        CREATE TABLE IF NOT EXISTS route_rollup (
          granularity TEXT NOT NULL, bucket_start TEXT NOT NULL, telemetry_version INTEGER NOT NULL,
          route_count INTEGER NOT NULL, completed_count INTEGER NOT NULL, known_count INTEGER NOT NULL,
          payload_json TEXT NOT NULL, built_at TEXT NOT NULL,
          PRIMARY KEY(granularity, bucket_start, telemetry_version)
        );
        CREATE TABLE IF NOT EXISTS telemetry_maintenance (
          name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alert_state (
          rule_name TEXT PRIMARY KEY, status TEXT NOT NULL, last_evaluated_at TEXT,
          last_triggered_at TEXT, detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS site_policy (
          site_id TEXT PRIMARY KEY, max_parallel INTEGER NOT NULL DEFAULT 8,
          enabled INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS provider_capability (
          fingerprint TEXT NOT NULL, model TEXT NOT NULL, contract TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'unknown', last_failure_class TEXT,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(fingerprint, model, contract)
        );
        CREATE INDEX IF NOT EXISTS balance_snapshot_site_time ON balance_snapshot(site_id, id DESC);
        """)
        try: self.conn.execute('ALTER TABLE policy ADD COLUMN calibrated INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE policy ADD COLUMN multiplier REAL NOT NULL DEFAULT 1.0')
        except sqlite3.OperationalError: pass
        for name, definition in [('note',"TEXT NOT NULL DEFAULT ''"),('preference','INTEGER NOT NULL DEFAULT 0'),('max_parallel','INTEGER NOT NULL DEFAULT 3')]:
            try: self.conn.execute(f'ALTER TABLE policy ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE observation ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
        except sqlite3.OperationalError: pass
        for name, definition in [
            ('input_tokens','INTEGER'),('output_tokens','INTEGER'),('cost','REAL'),('request_id','TEXT'),
            ('diagnostic_json','TEXT'),('route_id','TEXT'),('attempt_number','INTEGER'),
            ('started_ms','REAL'),('elapsed_ms','REAL'),
        ]:
            try: self.conn.execute(f'ALTER TABLE observation ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE source_provider ADD COLUMN request_headers BLOB')
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN api_key_mask TEXT NOT NULL DEFAULT '***'")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN site_id TEXT NOT NULL DEFAULT 'default'")
        except sqlite3.OperationalError: pass
        # Existing inventories predate explicit site fault domains.  Derive a
        # stable non-secret domain before routing starts so upgrade does not
        # collapse every old key into the synthetic "default" site.
        for row in self.conn.execute("SELECT fingerprint,base_url,source_json,site_id FROM source_provider"):
            try:
                source = json.loads(row["source_json"])
            except (TypeError, ValueError):
                source = {}
            site = row["site_id"]
            if not site or site == "default":
                site = self.site_id(source.get("site_name"), row["base_url"])
                self.conn.execute("UPDATE source_provider SET site_id=? WHERE fingerprint=?", (site, row["fingerprint"]))
            self.conn.execute("INSERT OR IGNORE INTO site_policy(site_id) VALUES(?)", (site,))
        try: self.conn.execute('ALTER TABLE provider_health ADD COLUMN last_route_recovery_at TEXT')
        except sqlite3.OperationalError: pass
        for name, definition in [
            ("telemetry_version", "INTEGER"), ("delivery_mode", "TEXT"), ("release_version", "TEXT"),
            ("routing_policy_version", "TEXT"), ("configuration_fingerprint", "TEXT"),
            ("shape_version", "INTEGER"), ("input_bucket", "TEXT"), ("output_budget_bucket", "TEXT"),
            ("deadline_bucket", "TEXT"), ("schema_family", "TEXT"), ("experiment_id", "TEXT"),
            ("experiment_arm", "TEXT"), ("first_attempt_ms", "REAL"), ("capacity_wait_ms", "REAL"),
            ("first_response_header_ms", "REAL"), ("first_text_ms", "REAL"), ("validation_completed_ms", "REAL"),
        ]:
            try: self.conn.execute(f"ALTER TABLE route_run ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError: pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS route_run_telemetry_window ON route_run(telemetry_version, started_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS route_run_delivery_window ON route_run(delivery_mode, started_at DESC)")
        from .catalog import CATALOG, CATALOG_SEED_VERSION, CATALOG_V2_MODELS
        seed_row = self.conn.execute("SELECT value FROM broker_setting WHERE name='catalog_seed_version'").fetchone()
        seed_version = int(seed_row[0]) if seed_row else (1 if catalog_exists else 0)
        seed_models = CATALOG if not catalog_exists else {
            model: CATALOG[model] for model in CATALOG_V2_MODELS
        } if seed_version < CATALOG_SEED_VERSION else {}
        self.conn.executemany(
            "INSERT OR IGNORE INTO model_catalog VALUES(?,?,?,?,?,?)",
            [(model, item['family'], item['intellect'], item['official_input_price'], item['official_cache_price'], item['official_output_price']) for model, item in seed_models.items()],
        )
        if seed_version < 4:
            self.conn.executemany("DELETE FROM model_catalog WHERE model=?", [("deepseek-v4-flash-0731",), ("deepseek-v4.1-flash",)])
        self.conn.execute(
            "INSERT INTO broker_setting(name,value) VALUES('catalog_seed_version',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (str(CATALOG_SEED_VERSION),),
        )
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('race_parallel_cap',?)", (str(self.default_race_parallel_cap),))
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('hedge_delay_ms','750')")
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('global_parallel_cap',?)", (str(max(4, self.default_race_parallel_cap * 4)),))
        from .balances import SITES
        self.conn.executemany(
            """INSERT OR IGNORE INTO balance_site(id,name,adapter,base_url,currency,low_threshold)
               VALUES(?,?,?,?,?,?)""",
            [(site.id, site.name, site.adapter, site.base_url, site.currency, site.default_threshold) for site in SITES],
        )
        self.conn.commit()

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).astimezone(UTC).isoformat()

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            return None

    def ensure_health_targets(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Create health rows for newly discovered usable Provider/model pairs."""
        stamp = self._timestamp(now)
        catalog = set(self.catalog())
        rows = self.conn.execute("SELECT fingerprint,models_json FROM source_provider").fetchall()
        created = []
        with self.conn:
            for row in rows:
                for model in json.loads(row["models_json"]):
                    if model not in catalog:
                        continue
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO provider_health(fingerprint,model,next_probe_at,updated_at) VALUES(?,?,?,?)",
                        (row["fingerprint"], model, stamp, stamp),
                    ).rowcount
                    if inserted:
                        created.append((row["fingerprint"], model))
        return created

    def health(self, fingerprint: str, model: str) -> dict:
        row = self.conn.execute("SELECT * FROM provider_health WHERE fingerprint=? AND model=?", (fingerprint, model)).fetchone()
        if row is None:
            return {"state": "unknown", "consecutive_failures": 0, "backoff_level": 0}
        return dict(row)

    def health_allows_route(self, fingerprint: str, model: str) -> bool:
        return self.health(fingerprint, model)["state"] != "open"

    def record_health(self, fingerprint: str, model: str, *, success: bool, real: bool,
                      ttft_ms: float | None = None, immediate_open: bool = False,
                      now: datetime | None = None) -> dict:
        """Apply passive or probe evidence without touching ordinary call statistics."""
        stamp = self._timestamp(now)
        current = self.health(fingerprint, model)
        if not real and current.get("last_real_attempt") and current["last_real_attempt"] > stamp:
            return current
        state = current["state"]
        failures = int(current.get("consecutive_failures") or 0)
        level = int(current.get("backoff_level") or 0)
        last_real_attempt = stamp if real else current.get("last_real_attempt")
        last_real_success = stamp if real and success else current.get("last_real_success")
        last_probe_at = stamp if not real else current.get("last_probe_at")
        smooth_success = current.get("smoothed_success")
        smooth_ttft = current.get("smoothed_ttft_ms")
        if real:
            smooth_success = (float(smooth_success) * .8 + (1.0 if success else 0.0) * .2) if smooth_success is not None else float(success)
        if success and ttft_ms is not None:
            smooth_ttft = (float(smooth_ttft) * .8 + float(ttft_ms) * .2) if smooth_ttft is not None else float(ttft_ms)
        if success:
            failures, level, next_probe = 0, 0, None
            # Recovery probes deliberately require one real request before full health.
            state = "half_open" if not real and state in {"open", "suspect"} else "healthy"
        else:
            failures += 1
            if immediate_open or state == "half_open" or failures >= 3:
                state = "open"
                level = min(level + 1, 4)
                delay = (2, 5, 15, 30, 60)[level]
                next_probe = self._timestamp((now or datetime.now(UTC)) + timedelta(minutes=delay))
            else:
                state, next_probe = "suspect", stamp
        with self.conn:
            self.conn.execute("""INSERT INTO provider_health(fingerprint,model,state,consecutive_failures,backoff_level,last_real_attempt,last_real_success,last_probe_at,next_probe_at,smoothed_success,smoothed_ttft_ms,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint,model) DO UPDATE SET state=excluded.state,consecutive_failures=excluded.consecutive_failures,backoff_level=excluded.backoff_level,last_real_attempt=excluded.last_real_attempt,last_real_success=excluded.last_real_success,last_probe_at=excluded.last_probe_at,next_probe_at=excluded.next_probe_at,smoothed_success=excluded.smoothed_success,smoothed_ttft_ms=excluded.smoothed_ttft_ms,updated_at=excluded.updated_at""",
                (fingerprint, model, state, failures, level, last_real_attempt, last_real_success, last_probe_at, next_probe, smooth_success, smooth_ttft, stamp))
        return self.health(fingerprint, model)

    def due_health_targets(self, now: datetime | None = None, stale_seconds: int = 1800) -> list[tuple[str, str]]:
        now = now or datetime.now(UTC)
        stamp = self._timestamp(now)
        stale = self._timestamp(now - timedelta(seconds=stale_seconds))
        rows = self.conn.execute("""SELECT h.fingerprint,h.model FROM provider_health h
            JOIN source_provider s USING(fingerprint) JOIN policy p USING(fingerprint)
            WHERE p.enabled=1 AND p.calibrated=1 AND (
              (h.state='open' AND h.next_probe_at IS NOT NULL AND h.next_probe_at<=?) OR
              (h.state!='open' AND (h.next_probe_at IS NOT NULL AND h.next_probe_at<=? OR (h.last_real_attempt IS NULL OR h.last_real_attempt<?) AND (h.last_probe_at IS NULL OR h.last_probe_at<?)))
            ) ORDER BY h.next_probe_at, h.updated_at""", (stamp, stamp, stale, stale)).fetchall()
        return [(row["fingerprint"], row["model"]) for row in rows]

    def record_probe(self, *, fingerprint: str, model: str, tier: str, mode: str, reachable: bool,
                     responded: bool, first_token: bool, model_matched: bool, ttfb_ms: float | None,
                     ttft_ms: float | None, duration_ms: float | None, error_type: str | None,
                     error: str | None, now: datetime | None = None) -> None:
        with self.conn:
            self.conn.execute("""INSERT INTO probe_event(fingerprint,model,tier,mode,reachable,responded,first_token,model_matched,ttfb_ms,ttft_ms,duration_ms,error_type,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (fingerprint, model, tier, mode, int(reachable), int(responded), int(first_token), int(model_matched), ttfb_ms, ttft_ms, duration_ms, error_type, error, self._timestamp(now)))

    def health_results(self, tier: str, fingerprint: str | None = None, model: str | None = None) -> list[dict]:
        clauses = ["c.intellect=?"]; params: list[object] = [tier]
        if fingerprint: clauses.append("h.fingerprint=?"); params.append(fingerprint)
        if model: clauses.append("h.model=?"); params.append(model)
        rows = self.conn.execute("""SELECT h.*,s.name,p.note,e.id probe_id,e.ttft_ms probe_ttft_ms,e.error_type probe_error_type,e.created_at probe_at
            FROM provider_health h JOIN source_provider s USING(fingerprint) JOIN policy p USING(fingerprint)
            JOIN model_catalog c ON c.model=h.model
            LEFT JOIN probe_event e ON e.id=(SELECT id FROM probe_event WHERE fingerprint=h.fingerprint AND model=h.model ORDER BY id DESC LIMIT 1)
            WHERE """ + " AND ".join(clauses) + " ORDER BY s.name,h.model", params).fetchall()
        return [{"fingerprint": r["fingerprint"], "provider": r["name"], "note": r["note"], "model": r["model"], "state": r["state"], "consecutive_failures": r["consecutive_failures"], "backoff_level": r["backoff_level"], "last_real_attempt": r["last_real_attempt"], "last_real_success": r["last_real_success"], "last_probe_at": r["probe_at"] or r["last_probe_at"], "next_probe_at": r["next_probe_at"], "ttft_ms": r["probe_ttft_ms"], "error_type": r["probe_error_type"]} for r in rows]

    def catalog(self):
        return {r['model']:{'family':r['family'],'intellect':r['intellect'],'official_input_price':r['input_price'],'official_cache_price':r['cache_price'],'official_output_price':r['output_price']} for r in self.conn.execute('SELECT * FROM model_catalog ORDER BY model')}
    def create_catalog(self, model, body):
        try:
            with self.conn:
                self.conn.execute("INSERT INTO model_catalog VALUES(?,?,?,?,?,?)", (model, body['family'], body['intellect'], body['official_input_price'], body['official_cache_price'], body['official_output_price']))
        except sqlite3.IntegrityError:
            return False
        return True
    def update_catalog(self, model, body):
        with self.conn:
            updated = self.conn.execute(
                "UPDATE model_catalog SET family=?,intellect=?,input_price=?,cache_price=?,output_price=? WHERE model=?",
                (body['family'], body['intellect'], body['official_input_price'], body['official_cache_price'], body['official_output_price'], model),
            ).rowcount
        return bool(updated)
    def delete_catalog(self, model):
        with self.conn:
            deleted = self.conn.execute("DELETE FROM model_catalog WHERE model=?", (model,)).rowcount
        return bool(deleted)
    def apply_catalog_to_inventory(self):
        catalog = set(self.catalog())
        rows = self.conn.execute('SELECT fingerprint,models_json FROM source_provider').fetchall()
        removed = retained = 0
        with self.conn:
            for row in rows:
                models = json.loads(row['models_json'])
                kept = [model for model in models if model in catalog]
                removed += len(models) - len(kept)
                retained += len(kept)
                self.conn.execute('UPDATE source_provider SET models_json=? WHERE fingerprint=?', (json.dumps(kept), row['fingerprint']))
                self.conn.execute('UPDATE policy SET calibrated=? WHERE fingerprint=?', (int(bool(kept)), row['fingerprint']))
            self.conn.execute('DELETE FROM route_block WHERE model NOT IN (SELECT model FROM model_catalog)')
        return {'providers': len(rows), 'removed_models': removed, 'retained_models': retained}
    def race_parallel_cap(self):
        value = self.conn.execute("SELECT value FROM broker_setting WHERE name='race_parallel_cap'").fetchone()[0]
        return int(value)
    def update_race_parallel_cap(self, value):
        with self.conn:
            self.conn.execute("UPDATE broker_setting SET value=? WHERE name='race_parallel_cap'", (str(value),))
    def hedge_delay_ms(self):
        return int(self.conn.execute("SELECT value FROM broker_setting WHERE name='hedge_delay_ms'").fetchone()[0])
    def update_routing(self, *, race_parallel_cap=None, hedge_delay_ms=None):
        with self.conn:
            if race_parallel_cap is not None:
                self.conn.execute("UPDATE broker_setting SET value=? WHERE name='race_parallel_cap'", (str(race_parallel_cap),))
            if hedge_delay_ms is not None:
                self.conn.execute("UPDATE broker_setting SET value=? WHERE name='hedge_delay_ms'", (str(hedge_delay_ms),))

    def global_parallel_cap(self) -> int:
        return int(self.conn.execute("SELECT value FROM broker_setting WHERE name='global_parallel_cap'").fetchone()[0])

    def update_global_parallel_cap(self, value: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE broker_setting SET value=? WHERE name='global_parallel_cap'", (str(value),))

    def sites(self) -> list[dict]:
        rows = self.conn.execute("""SELECT sp.site_id,sp.max_parallel,sp.enabled,sp.note,
            count(s.fingerprint) keys FROM site_policy sp LEFT JOIN source_provider s USING(site_id)
            GROUP BY sp.site_id ORDER BY sp.site_id""").fetchall()
        return [{"site_id": r["site_id"], "max_parallel": r["max_parallel"],
                 "enabled": bool(r["enabled"]), "note": r["note"], "keys": r["keys"],
                 "inflight": self._site_inflight.get(r["site_id"], 0)} for r in rows]

    def update_site_policy(self, site_id: str, body: dict) -> bool:
        current = self.conn.execute("SELECT * FROM site_policy WHERE site_id=?", (site_id,)).fetchone()
        if current is None:
            return False
        with self.conn:
            self.conn.execute("UPDATE site_policy SET max_parallel=?,enabled=?,note=? WHERE site_id=?", (
                int(body.get("max_parallel", current["max_parallel"])), int(body.get("enabled", current["enabled"])),
                str(body.get("note", current["note"])), site_id,
            ))
        return True

    def routing_context(self) -> dict[str, str]:
        values = {
            "race_parallel_cap": self.race_parallel_cap(),
            "hedge_delay_ms": self.hedge_delay_ms(),
            "global_parallel_cap": self.global_parallel_cap(),
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {
            "release_version": broker_release_version(),
            "routing_policy_version": ROUTING_POLICY_VERSION,
            "configuration_fingerprint": hashlib.sha256(encoded).hexdigest()[:16],
        }

    def record_configuration_change(self, setting: str, before: object, after: object, *, source: str = "admin") -> None:
        safe_settings = {"race_parallel_cap", "hedge_delay_ms", "global_parallel_cap", "site_policy"}
        if setting not in safe_settings or before == after:
            return
        with self.conn:
            self.conn.execute("INSERT INTO configuration_event(setting,before_value,after_value,source,created_at) VALUES(?,?,?,?,?)",
                (setting, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), source[:32], self._timestamp()))

    def configuration_events(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.conn.execute("SELECT setting,before_value,after_value,source,created_at FROM configuration_event ORDER BY id DESC LIMIT ?", (limit,))]

    def record_client_telemetry(self, *, route_id: str, metric_type: str, elapsed_ms: float,
                                client_family: str, client_version: str, telemetry_version: int) -> bool:
        if metric_type != "client_first_delta" or not 0 <= elapsed_ms <= 3_600_000 or not client_family or not client_version:
            raise ValueError("invalid telemetry")
        route = self.conn.execute("SELECT delivery_mode,outcome,started_at,completed_ms FROM route_run WHERE route_id=?", (route_id,)).fetchone()
        started = self._parse_timestamp(route["started_at"]) if route else None
        too_old = started is None or started < datetime.now(UTC) - timedelta(hours=24)
        impossible = route and route["completed_ms"] is not None and elapsed_ms > float(route["completed_ms"]) + 60_000
        if route is None or route["delivery_mode"] != "plain_stream" or route["outcome"] not in (None, "completed") or too_old or impossible or telemetry_version != TELEMETRY_SCHEMA_VERSION:
            raise LookupError("unknown or ineligible route")
        with self.conn:
            inserted = self.conn.execute("""INSERT OR IGNORE INTO client_telemetry(route_id,metric_type,elapsed_ms,
                client_family,client_version,telemetry_version,received_at) VALUES(?,?,?,?,?,?,?)""",
                (route_id, metric_type, elapsed_ms, client_family[:64], client_version[:64], telemetry_version, self._timestamp())).rowcount
        return bool(inserted)

    def route_started(self, route_id: str, tier: str, request_id: str, effort: str | None = None,
                      *, body: dict | None = None, delivery_mode: str = "non_stream",
                      experiment_id: str | None = None, experiment_arm: str | None = None) -> None:
        if delivery_mode not in DELIVERY_MODES:
            delivery_mode = "non_stream"
        shape = request_shape(body)
        context = self.routing_context()
        with self.conn:
            self.conn.execute("""INSERT INTO route_run(
                route_id,tier,effort,request_id,telemetry_version,delivery_mode,release_version,
                routing_policy_version,configuration_fingerprint,shape_version,input_bucket,
                output_budget_bucket,deadline_bucket,schema_family,experiment_id,experiment_arm
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(route_id) DO NOTHING""", (
                route_id, tier, effort, request_id, TELEMETRY_SCHEMA_VERSION, delivery_mode,
                context["release_version"], context["routing_policy_version"], context["configuration_fingerprint"],
                TELEMETRY_SCHEMA_VERSION, shape["input_bucket"], shape["output_budget_bucket"],
                shape["deadline_bucket"], shape["schema_family"], experiment_id, experiment_arm,
            ))

    def route_finished(self, route_id: str, *, outcome: str, first_delta_ms: float | None = None,
                       completed_ms: float | None = None, selected_fingerprint: str | None = None,
                       selected_model: str | None = None, selected_site_id: str | None = None,
                       terminal_reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""UPDATE route_run SET completed_at=?,outcome=?,first_delta_ms=?,completed_ms=?,
                selected_fingerprint=?,selected_model=?,selected_site_id=?,terminal_reason=? WHERE route_id=? AND outcome IS NULL""", (
                self._timestamp(), outcome, first_delta_ms, completed_ms, selected_fingerprint,
                selected_model, selected_site_id, terminal_reason, route_id,
            ))

    def route_milestone(self, route_id: str, **milestones: float | None) -> None:
        allowed = {"first_attempt_ms", "capacity_wait_ms", "first_response_header_ms", "first_text_ms",
                   "validation_completed_ms", "first_forwarded_delta_ms"}
        values = {key: value for key, value in milestones.items() if key in allowed and value is not None}
        if not values:
            return
        if "first_forwarded_delta_ms" in values:
            values["first_delta_ms"] = values.pop("first_forwarded_delta_ms")
        assignments = ",".join(f"{key}=coalesce({key},?)" for key in values)
        with self.conn:
            self.conn.execute(f"UPDATE route_run SET {assignments} WHERE route_id=?", [*values.values(), route_id])

    def record_candidate(self, route_id: str, *, fingerprint: str | None, model: str | None,
                         site_id: str | None, eligible: bool, initial_rank: int | None = None,
                         exclusion_reason: str | None = None, launched: bool = False,
                         role: str | None = None) -> None:
        reasons = {"policy_disabled", "inventory_mismatch", "capability_unsupported", "health_open",
                   "route_blocked", "site_disabled", "key_capacity", "site_capacity", "global_capacity",
                   "deadline_budget"}
        if exclusion_reason not in reasons:
            exclusion_reason = None if eligible else "inventory_mismatch"
        ordinal = self.conn.execute("SELECT coalesce(max(ordinal), -1)+1 FROM route_candidate WHERE route_id=?", (route_id,)).fetchone()[0]
        with self.conn:
            self.conn.execute("""INSERT INTO route_candidate(route_id,ordinal,fingerprint,model,site_id,eligible,
                exclusion_reason,initial_rank,launched,role) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                route_id, ordinal, fingerprint, model, site_id, int(eligible), exclusion_reason,
                initial_rank, int(launched), role,
            ))

    def record_attempt(self, route_id: str, *, attempt_number: int, fingerprint: str | None,
                       model: str | None, site_id: str | None, role: str, status: str | None,
                       failure_class: str | None = None, started_ms: float | None = None,
                       elapsed_ms: float | None = None) -> None:
        if role not in {"primary", "hedge", "retry", "repair", "exploration", "recovery"}:
            role = "retry"
        with self.conn:
            self.conn.execute("""INSERT INTO route_attempt(route_id,attempt_number,fingerprint,model,site_id,role,
                status,failure_class,started_ms,elapsed_ms) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(route_id,attempt_number) DO UPDATE SET status=excluded.status,
                failure_class=excluded.failure_class,elapsed_ms=excluded.elapsed_ms""", (
                route_id, attempt_number, fingerprint, model, site_id, role, status, failure_class,
                started_ms, elapsed_ms,
            ))

    def mark_candidate(self, route_id: str, *, fingerprint: str, model: str, role: str,
                       exclusion_reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""UPDATE route_candidate SET launched=?,role=?,exclusion_reason=?
                WHERE route_id=? AND ordinal=(SELECT ordinal FROM route_candidate WHERE route_id=?
                AND fingerprint=? AND model=? AND launched=0 ORDER BY ordinal LIMIT 1)""", (
                int(exclusion_reason is None), role, exclusion_reason, route_id, route_id, fingerprint, model,
            ))

    def reconcile_open_routes(self, *, grace_seconds: int = 300, now: datetime | None = None) -> int:
        cutoff = self._timestamp((now or datetime.now(UTC)) - timedelta(seconds=max(0, grace_seconds)))
        with self.conn:
            return self.conn.execute("""UPDATE route_run SET outcome='unknown', completed_at=?,
                terminal_reason='reconciled_after_restart' WHERE outcome IS NULL AND started_at<?""",
                (self._timestamp(now), cutoff)).rowcount

    def data_health(self) -> dict[str, int]:
        row = self.conn.execute("""SELECT
            sum(outcome IS NULL) in_progress,
            sum(outcome='unknown' AND terminal_reason='reconciled_after_restart') reconciled_unknown,
            sum(telemetry_version IS NULL) legacy_records,
            sum(outcome='unknown') unknown
            FROM route_run""").fetchone()
        maintenance = {item["name"]: item["value"] for item in self.conn.execute("SELECT name,value FROM telemetry_maintenance")}
        return {name: int(row[name] or 0) for name in ("in_progress", "reconciled_unknown", "legacy_records", "unknown")} | {
            "collection_errors": 0, "latest_rollup_at": maintenance.get("latest_rollup_at"),
            "rollup_watermark": maintenance.get("rollup_watermark"), "retention_watermark": maintenance.get("retention_watermark"),
        }

    def run_rollups(self, *, now: datetime | None = None) -> dict[str, int | str | None]:
        now = now or datetime.now(UTC)
        cutoff = self._timestamp(now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1))
        rows = self.conn.execute("""SELECT strftime('%Y-%m-%dT%H:00:00Z', started_at) bucket,
            telemetry_version,count(*) routes,sum(outcome='completed') completed,
            sum(outcome IN ('completed','failed','timed_out')) known FROM route_run
            WHERE started_at<? GROUP BY bucket,telemetry_version""", (cutoff,)).fetchall()
        built = 0
        with self.conn:
            for row in rows:
                payload = {"success_rate": row["completed"] / row["known"] if row["known"] else None,
                           "unknown": int(row["routes"] or 0) - int(row["known"] or 0)}
                self.conn.execute("""INSERT INTO route_rollup(granularity,bucket_start,telemetry_version,route_count,
                    completed_count,known_count,payload_json,built_at) VALUES('hour',?,?,?,?,?,?,?)
                    ON CONFLICT(granularity,bucket_start,telemetry_version) DO UPDATE SET route_count=excluded.route_count,
                    completed_count=excluded.completed_count,known_count=excluded.known_count,payload_json=excluded.payload_json,built_at=excluded.built_at""",
                    (row["bucket"], row["telemetry_version"] or 0, row["routes"], row["completed"] or 0,
                     row["known"] or 0, json.dumps(payload, sort_keys=True), self._timestamp(now)))
                built += 1
            stamp = self._timestamp(now)
            for name, value in (("latest_rollup_at", stamp), ("rollup_watermark", cutoff)):
                self.conn.execute("INSERT INTO telemetry_maintenance(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (name, value, stamp))
        return {"built": built, "watermark": cutoff}

    def apply_retention(self, *, raw_days: int = 90, batch_size: int = 500, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        raw_days = min(365, max(7, int(raw_days)))
        cutoff = self._timestamp(now - timedelta(days=raw_days))
        watermark = self.conn.execute("SELECT value FROM telemetry_maintenance WHERE name='rollup_watermark'").fetchone()
        if watermark is None:
            return 0
        safe_before = min(cutoff, watermark[0])
        with self.conn:
            route_ids = [row[0] for row in self.conn.execute("SELECT route_id FROM route_run WHERE started_at<? LIMIT ?", (safe_before, max(1, batch_size))).fetchall()]
            if not route_ids:
                return 0
            marks = ",".join("?" for _ in route_ids)
            self.conn.execute(f"DELETE FROM route_candidate WHERE route_id IN ({marks})", route_ids)
            self.conn.execute(f"DELETE FROM route_attempt WHERE route_id IN ({marks})", route_ids)
            self.conn.execute(f"DELETE FROM client_telemetry WHERE route_id IN ({marks})", route_ids)
            self.conn.execute(f"DELETE FROM observation WHERE route_id IN ({marks})", route_ids)
            deleted = self.conn.execute(f"DELETE FROM route_run WHERE route_id IN ({marks})", route_ids).rowcount
            self.conn.execute("INSERT INTO telemetry_maintenance(name,value,updated_at) VALUES('retention_watermark',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (safe_before, self._timestamp(now)))
        return deleted

    def rollup_status(self) -> dict:
        return self.data_health() | {"hourly_count": self.conn.execute("SELECT count(*) FROM route_rollup WHERE granularity='hour'").fetchone()[0]}

    def routes(self, *, window: str = "24h", limit: int = 50, cursor: str | None = None) -> list[dict]:
        modifier = {"1h": "-1 hour", "24h": "-24 hours", "7d": "-7 days", "30d": "-30 days"}[window]
        clauses, params = ["started_at >= datetime('now', ? )"], [modifier]
        if cursor:
            clauses.append("rowid < ?")
            params.append(int(cursor))
        rows = self.conn.execute("SELECT rowid,* FROM route_run WHERE " + " AND ".join(clauses) +
            " ORDER BY rowid DESC LIMIT ?", [*params, limit]).fetchall()
        return [{
            "cursor": row["rowid"], "route_id": row["route_id"], "started_at": row["started_at"],
            "outcome": row["outcome"], "delivery_mode": row["delivery_mode"], "tier": row["tier"],
            "selected_model": row["selected_model"], "selected_site_id": row["selected_site_id"],
            "terminal_reason": row["terminal_reason"], "telemetry_version": row["telemetry_version"],
        } for row in rows]

    def analytics(self, *, window: str = "24h", group_by: str | None = None,
                  filters: dict[str, str] | None = None) -> dict:
        columns = {
            "provider": "selected_fingerprint", "site": "selected_site_id", "actual_model": "selected_model",
            "intellect": "tier", "effort": "effort", "delivery_mode": "delivery_mode",
            "outcome": "outcome", "release_version": "release_version", "policy_version": "routing_policy_version",
            "configuration": "configuration_fingerprint", "experiment_arm": "experiment_arm", "input_bucket": "input_bucket",
            "output_budget_bucket": "output_budget_bucket", "deadline_bucket": "deadline_bucket", "schema_family": "schema_family",
        }
        if group_by and group_by not in columns:
            raise ValueError("invalid group")
        filters = filters or {}
        if any(key not in columns for key in filters):
            raise ValueError("invalid filter")
        modifier = {"1h": "-1 hour", "24h": "-24 hours", "7d": "-7 days", "30d": "-30 days"}[window]
        clauses, params = ["started_at >= datetime('now', ?)"], [modifier]
        for key, value in filters.items():
            clauses.append(f"{columns[key]}=?")
            params.append(value)
        field = columns.get(group_by or "", "'all'")
        rows = self.conn.execute(f"""SELECT {field} group_value, count(*) sample_count,
            sum(outcome='completed') successes, sum(outcome IN ('completed','failed','timed_out')) known,
            sum(outcome='unknown') unknown, sum(outcome IS NULL) in_progress,
            avg(completed_ms) completion_mean_ms FROM route_run WHERE {' AND '.join(clauses)}
            GROUP BY {field} ORDER BY sample_count DESC""", params).fetchall()
        groups = []
        for row in rows:
            known = int(row["known"] or 0)
            success = int(row["successes"] or 0)
            # Wilson 95% interval, defined even for an empty denominator as unavailable.
            if known:
                z2, center = 1.96 ** 2, success / known
                delta = 1.96 * ((center * (1 - center) / known + z2 / (4 * known ** 2)) ** .5)
                denominator = 1 + z2 / known
                interval = [max(0, (center + z2 / (2 * known) - delta) / denominator), min(1, (center + z2 / (2 * known) + delta) / denominator)]
            else:
                interval = None
            groups.append({
                "group": row["group_value"] if row["group_value"] is not None else "unknown",
                "sample_count": int(row["sample_count"]), "success_numerator": success,
                "success_denominator": known, "success_rate": success / known if known else None,
                "confidence_interval_95": interval, "unknown_count": int(row["unknown"] or 0),
                "in_progress_count": int(row["in_progress"] or 0), "completion_mean_ms": row["completion_mean_ms"],
                "insufficient": known < 200,
            })
        return {"metric_version": TELEMETRY_SCHEMA_VERSION, "window": window, "group_by": group_by,
                "filters": filters, "minimum_sample": 200, "groups": groups}

    def route_detail(self, route_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM route_run WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            return None
        allowed = (
            "route_id", "tier", "effort", "started_at", "completed_at", "outcome", "first_delta_ms",
            "completed_ms", "selected_fingerprint", "selected_model", "selected_site_id", "terminal_reason",
            "telemetry_version", "delivery_mode", "release_version", "routing_policy_version",
            "configuration_fingerprint", "shape_version", "input_bucket", "output_budget_bucket",
            "deadline_bucket", "schema_family", "experiment_id", "experiment_arm",
            "first_attempt_ms", "capacity_wait_ms", "first_response_header_ms", "first_text_ms",
            "validation_completed_ms",
        )
        detail = {name: row[name] for name in allowed}
        detail["candidates"] = [dict(candidate) for candidate in self.conn.execute("""SELECT fingerprint,model,site_id,
            eligible,exclusion_reason,initial_rank,launched,role FROM route_candidate WHERE route_id=? ORDER BY ordinal""", (route_id,))]
        detail["attempts"] = [dict(attempt) for attempt in self.conn.execute("""SELECT attempt_number,fingerprint,model,
            site_id,role,status,failure_class,started_ms,elapsed_ms FROM route_attempt WHERE route_id=? ORDER BY attempt_number""", (route_id,))]
        detail["client_telemetry"] = [dict(item) for item in self.conn.execute("""SELECT metric_type,elapsed_ms,
            client_family,client_version,telemetry_version,received_at FROM client_telemetry WHERE route_id=?""", (route_id,))]
        detail["amplification"] = self.route_amplification(route_id, detail["attempts"])
        return detail

    def route_audit_items(self, route_id: str, kind: str, *, limit: int = 100,
                          cursor: int | None = None) -> tuple[list[dict], int | None]:
        """Return bounded, deterministic audit facts for a route subresource."""
        if kind == "candidates":
            table, key, columns = "route_candidate", "ordinal", (
                "ordinal,fingerprint,model,site_id,eligible,exclusion_reason,initial_rank,launched,role")
        elif kind == "attempts":
            table, key, columns = "route_attempt", "attempt_number", (
                "attempt_number,fingerprint,model,site_id,role,status,failure_class,started_ms,elapsed_ms")
        else:
            raise ValueError("invalid audit resource")
        limit = max(1, min(100, int(limit)))
        clauses = ["route_id=?"]
        params: list[object] = [route_id]
        if cursor is not None:
            clauses.append(f"{key} > ?")
            params.append(cursor)
        rows = self.conn.execute(
            f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {key} LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return [dict(row) for row in rows], (rows[-1][key] if has_more and rows else None)

    def route_amplification(self, route_id: str, attempts: list[dict] | None = None) -> dict:
        attempts = attempts if attempts is not None else [dict(row) for row in self.conn.execute(
            "SELECT * FROM route_attempt WHERE route_id=? ORDER BY attempt_number", (route_id,))]
        usage = self.conn.execute("""SELECT fingerprint,status,input_tokens,output_tokens,cost FROM observation
            WHERE route_id=? AND attempt_number IS NOT NULL AND attempt_number>0""", (route_id,)).fetchall()
        known_costs = [row["cost"] for row in usage if row["cost"] is not None]
        known_tokens = [int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) for row in usage
                        if row["input_tokens"] is not None and row["output_tokens"] is not None]
        winner = next((row for row in usage if row["status"] == "completed"), None)
        roles = {role: sum(item.get("role") == role for item in attempts) for role in ("primary", "hedge", "retry", "repair", "exploration", "recovery")}
        hedge_winner = any(item.get("role") == "hedge" and item.get("status") == "completed" for item in attempts)
        primary_delivered = any(item.get("role") == "primary" and item.get("status") == "completed" for item in attempts)
        return {
            "attempts_started": len(attempts), "attempts_completed": sum(item.get("status") == "completed" for item in attempts),
            "attempts_cancelled": sum(item.get("status") == "cancelled" for item in attempts), "roles": roles,
            "distinct_sites": len({item.get("site_id") for item in attempts if item.get("site_id")}),
            "total_elapsed_ms": sum(item.get("elapsed_ms") or 0 for item in attempts),
            "known_cost": sum(known_costs) if known_costs else None,
            "cost_known_attempts": len(known_costs), "cost_attempts": len(usage),
            "known_tokens": sum(known_tokens) if known_tokens else None,
            "winner_cost": winner["cost"] if winner and winner["cost"] is not None else None,
            "winner_tokens": (int(winner["input_tokens"] or 0) + int(winner["output_tokens"] or 0)) if winner and winner["input_tokens"] is not None and winner["output_tokens"] is not None else None,
            "hedge_started": roles["hedge"] > 0,
            "hedge_rescue": hedge_winner and not primary_delivered,
        }

    def record_capability(self, fingerprint: str, model: str, contract: str, state: str,
                          failure_class: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""INSERT INTO provider_capability(fingerprint,model,contract,state,last_failure_class,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint,model,contract) DO UPDATE SET
                state=excluded.state,last_failure_class=excluded.last_failure_class,updated_at=excluded.updated_at""",
                (fingerprint, model, contract, state, failure_class, self._timestamp()))
    def catalog_counts(self):
        rows=self.conn.execute('SELECT s.fingerprint,s.models_json,s.source_json,p.enabled,p.calibrated FROM source_provider s JOIN policy p USING(fingerprint)').fetchall(); counts={name:0 for name in self.catalog()}
        for name in counts:
            counts[name]=len({r['fingerprint'] for r in rows if r['enabled'] and r['calibrated'] and json.loads(r['source_json']).get('inventory_status') == 'available' and name in json.loads(r['models_json'])})
        return counts

    def _encrypt(self, value: str) -> bytes:
        nonce = __import__('os').urandom(12)
        return nonce + self.aes.encrypt(nonce, value.encode(), None)

    def _decrypt(self, value: bytes) -> str:
        return self.aes.decrypt(value[:12], value[12:], None).decode()

    def balance_sites(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM balance_site ORDER BY rowid").fetchall()
        return [{
            "id": row["id"], "name": row["name"], "currency": row["currency"], "low_threshold": row["low_threshold"],
            "enabled": bool(row["enabled"]), "configured": row["credential"] is not None, "last_balance": row["last_balance"],
            "last_checked_at": row["last_checked_at"], "last_error": row["last_error"], "low": bool(row["low_alert_active"]),
            "last_notification_at": row["last_notification_at"], "notification_error": row["notification_error"],
        } for row in rows]

    def balance_site_secret(self, site_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM balance_site WHERE id=?", (site_id,)).fetchone()
        if row is None:
            return None
        credential = None
        if row["credential"] is not None:
            try:
                credential = json.loads(self._decrypt(row["credential"]))
            except Exception:
                credential = None
        return {"id": row["id"], "name": row["name"], "adapter": row["adapter"], "base_url": row["base_url"],
                "currency": row["currency"], "low_threshold": row["low_threshold"], "credential": credential}

    def update_balance_site(self, site_id: str, *, low_threshold: float | None = None, enabled: bool | None = None) -> bool:
        if low_threshold is None and enabled is None:
            return False
        assignments, params = [], []
        if low_threshold is not None:
            assignments.append("low_threshold=?"); params.append(low_threshold)
        if enabled is not None:
            assignments.append("enabled=?"); params.append(int(enabled))
        params.append(site_id)
        with self.conn:
            return bool(self.conn.execute(f"UPDATE balance_site SET {','.join(assignments)} WHERE id=?", params).rowcount)

    def save_balance_login(self, site_id: str, credential: dict) -> bool:
        encoded = self._encrypt(json.dumps(credential, separators=(",", ":")))
        with self.conn:
            return bool(self.conn.execute("UPDATE balance_site SET credential=?,last_error=NULL WHERE id=?", (encoded, site_id)).rowcount)

    def record_balance(self, site_id: str, balance: float, credential: dict) -> dict:
        row = self.conn.execute("SELECT * FROM balance_site WHERE id=?", (site_id,)).fetchone()
        if row is None:
            raise ValueError("unknown balance site")
        low = balance < float(row["low_threshold"])
        entered_low = low and not bool(row["low_alert_active"])
        stamp = self._timestamp()
        with self.conn:
            self.conn.execute("""UPDATE balance_site SET credential=?,last_balance=?,last_checked_at=?,last_error=NULL,
                low_alert_active=?,notification_error=CASE WHEN ? THEN NULL ELSE notification_error END WHERE id=?""",
                (self._encrypt(json.dumps(credential, separators=(",", ":"))), balance, stamp, int(low), int(entered_low), site_id))
            self.conn.execute("INSERT INTO balance_snapshot(site_id,balance,checked_at) VALUES(?,?,?)", (site_id, balance, stamp))
        return {"site": site_id, "name": row["name"], "currency": row["currency"], "balance": balance,
                "threshold": float(row["low_threshold"]), "low": low, "entered_low": entered_low}

    def record_balance_error(self, site_id: str, error: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE balance_site SET last_checked_at=?,last_error=? WHERE id=?", (self._timestamp(), error[:200], site_id))

    def balance_webhook(self) -> str | None:
        row = self.conn.execute("SELECT value FROM balance_setting WHERE name='webhook_url'").fetchone()
        if row is None:
            return None
        try:
            return self._decrypt(row["value"])
        except Exception:
            return None

    def update_balance_webhook(self, webhook: str | None) -> None:
        with self.conn:
            if webhook:
                self.conn.execute("INSERT INTO balance_setting(name,value) VALUES('webhook_url',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (self._encrypt(webhook),))
            else:
                self.conn.execute("DELETE FROM balance_setting WHERE name='webhook_url'")

    def balance_configuration(self) -> dict:
        return {"webhook_configured": self.balance_webhook() is not None}

    def record_balance_notification_sent(self, site_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE balance_site SET last_notification_at=?,notification_error=NULL WHERE id=?", (self._timestamp(), site_id))

    def record_balance_notification_error(self, site_id: str, error: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE balance_site SET notification_error=? WHERE id=?", (error[:200], site_id))

    @staticmethod
    def fingerprint(base_url: str, api_key: str, model: str | None = None) -> str:
        """Stable identity for one endpoint/key; model inventory is mutable evidence."""
        del model
        return hmac.new(b"provider-broker-source-v1", f"{base_url.rstrip('/')}\0{api_key}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def api_key_mask(api_key: str) -> str:
        if not isinstance(api_key, str) or len(api_key) < 7:
            return "***"
        return api_key[:3] + "***" + api_key[-3:]

    def replace_source_snapshot(self, entries: list[dict], synced_at: str):
        rows = []
        site_notes = []
        catalog = self.catalog()
        existing_rows = self.conn.execute("SELECT fingerprint,base_url,api_key,models_json FROM source_provider").fetchall()
        existing = {row["fingerprint"]: row for row in existing_rows}
        existing_policies = {row[0] for row in self.conn.execute("SELECT fingerprint FROM policy")}
        for entry in entries:
            base_url, api_key = entry["base_url"].rstrip("/"), entry["api_key"]
            from .catalog import PUBLIC_MODEL_IDS, canonicalize
            source_models = list(dict.fromkeys(canonicalize(model) for model in (entry.get("models") or [entry.get("model", "unavailable")])))
            host = (urlsplit(base_url).hostname or "").lower()
            for domain, public_models in PUBLIC_MODEL_IDS.items():
                if host == domain or host.endswith("." + domain):
                    source_models = list(dict.fromkeys(source_models + list(public_models)))
                    break
            models = [model for model in source_models if model in catalog]
            # Stable fingerprints avoid decrypting credentials during ordinary
            # refreshes.  The fallback only supports one-time adoption of old
            # databases whose fingerprint included the previous model list.
            prior = existing.get(self.fingerprint(base_url, api_key))
            if prior is None:
                for candidate in existing_rows:
                    if candidate["base_url"].rstrip("/") != base_url:
                        continue
                    try:
                        if self._decrypt(candidate["api_key"]) == api_key:
                            prior = candidate
                            break
                    except Exception:
                        continue
            unavailable = entry.get("inventory_status") == "unavailable" or source_models == ["unavailable"]
            # Model discovery is auxiliary evidence.  Never erase a known-good
            # route merely because a CPA /models refresh transiently fails.
            if unavailable and prior is not None:
                prior_models = json.loads(prior["models_json"])
                if prior_models:
                    source_models = prior_models
                    models = [model for model in source_models if model in catalog]
                    entry = entry | {"inventory_status": "stale"}
                    fp = prior["fingerprint"]
                else:
                    fp = self.fingerprint(base_url, api_key)
            else:
                fp = self.fingerprint(base_url, api_key)
            site_id = self.site_id(entry.get("site_name"), base_url)
            if isinstance(entry.get('site_name'), str) and entry['site_name'].strip():
                note = entry['site_name'].strip()
                if api_key not in note:
                    site_notes.append((note, fp))
            source = {
                key: str(entry.get("source", {}).get(key))[:160]
                for key in ("section", "site_name", "provider_type")
                if isinstance(entry.get("source"), dict) and isinstance(entry["source"].get(key), (str, int, float, bool))
            } | {"inventory_status": entry.get("inventory_status", "unavailable")}
            model_aliases = entry.get("model_aliases")
            if isinstance(model_aliases, dict):
                source["model_aliases"] = {
                    str(alias)[:160]: str(wire)[:240]
                    for alias, wire in model_aliases.items()
                    if isinstance(alias, str) and isinstance(wire, str)
                }
            request_headers = json.dumps(entry.get('request_headers') or {}, sort_keys=True)
            name = str(entry.get("name") or (models[0] if models else "unavailable"))[:160]
            if api_key in name:
                name = entry.get("provider_type", "openai")
            rows.append((fp, name, base_url, self._encrypt(api_key), entry.get("provider_type", "openai"), self._encrypt(request_headers), self.api_key_mask(api_key), json.dumps(models), json.dumps(source), synced_at, site_id))
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE incoming AS SELECT * FROM source_provider WHERE 0")
            self.conn.executemany("INSERT INTO incoming(fingerprint,name,base_url,api_key,provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.execute("DELETE FROM source_provider")
            self.conn.execute("INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id) SELECT fingerprint,name,base_url,api_key,provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id FROM incoming")
            self.conn.execute("DELETE FROM route_block WHERE fingerprint NOT IN (SELECT fingerprint FROM incoming)")
            self.conn.execute("DROP TABLE incoming")
            self.conn.executemany("INSERT OR IGNORE INTO policy(fingerprint) VALUES(?)", [(r[0],) for r in rows])
            self.conn.executemany("INSERT OR IGNORE INTO site_policy(site_id) VALUES(?)", [(r[10],) for r in rows])
            self.conn.executemany(
                "UPDATE policy SET calibrated=? WHERE fingerprint=?",
                [(int(any(model in catalog for model in json.loads(r[7]))), r[0]) for r in rows if r[0] not in existing_policies],
            )
            # A site name is a useful first-run label, but never overwrite an
            # operator's policy note during a later CPA refresh.
            self.conn.executemany("UPDATE policy SET note=? WHERE fingerprint=? AND note=''", site_notes)

    @staticmethod
    def site_id(site_name: object, base_url: str) -> str:
        """Stable, non-secret fault-domain identifier from CPA site metadata."""
        value = str(site_name or "").strip().lower()
        if not value:
            from urllib.parse import urlsplit
            value = urlsplit(base_url).hostname or base_url
        return "".join(char if char.isalnum() or char in "._-" else "-" for char in value)[:80] or "default"

    def capability_allows_route(self, fingerprint: str, model: str, contract: str) -> bool:
        """Gate only the contract known to be unsupported for this key/model."""
        row = self.conn.execute(
            "SELECT state FROM provider_capability WHERE fingerprint=? AND model=? AND contract=?",
            (fingerprint, model, contract),
        ).fetchone()
        return row is None or row["state"] != "unsupported"

    def providers(self, tier: str, *, contract: str | None = None) -> list[Provider]:
        rows = self.conn.execute("""SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.tiers_json,p.max_parallel FROM source_provider s JOIN policy p USING(fingerprint)
        WHERE p.enabled=1 AND p.calibrated=1 ORDER BY p.price_group, s.id""").fetchall()
        catalog = self.catalog()
        result=[]
        for r in rows:
            blocked={row[0] for row in self.conn.execute('SELECT model FROM route_block WHERE fingerprint=?',(r['fingerprint'],))}
            models=[m for m in json.loads(r['models_json']) if m in catalog and catalog[m]['intellect'] == tier and m not in blocked]
            if models and tier in json.loads(r['tiers_json']):
                header_blob = r['request_headers']
                headers = json.loads(self._decrypt(header_blob)) if header_blob else {}
                # A key can expose several catalog models in the same stage.  Health is
                # per model, so make each routing candidate explicit rather than letting
                # an open model hide behind the first item in a shared list.
                for model in models:
                    if contract and not self.capability_allows_route(r['fingerprint'], model, contract):
                        continue
                    if not self.health_allows_route(r['fingerprint'], model):
                        continue
                    pricing = catalog[model]
                    source = json.loads(r['source_json']) if r['source_json'] else {}
                    model_aliases = source.get('model_aliases') or {}
                    wire_model = model_aliases.get(model)
                    reverse_aliases = {str(wire).casefold(): str(alias) for alias, wire in model_aliases.items()}
                    result.append(Provider(r['id'],r['fingerprint'],r['name'],r['base_url'],self._decrypt(r['api_key']),r['provider_type'],headers,[model],pricing,int(blended_price(pricing)*r['multiplier']*100000),int(r['max_parallel']),bool(r['enabled']),float(r['multiplier']),r['site_id'],wire_model,reverse_aliases))
        return result

    def recovery_providers(self, tier: str, *, excluded_endpoints: set[tuple[str, str]], limit: int,
                           cooldown_seconds: int = 120, allow_before_due: bool = False,
                           now: datetime | None = None) -> list[Provider]:
        if limit <= 0:
            return []
        current_time = now or datetime.now(UTC)
        stamp = self._timestamp(current_time)
        cooldown_before = self._timestamp(current_time - timedelta(seconds=max(1, cooldown_seconds)))
        rows = self.conn.execute("""SELECT h.fingerprint,h.model FROM provider_health h
            JOIN source_provider s USING(fingerprint) JOIN policy p USING(fingerprint)
            JOIN model_catalog c ON c.model=h.model
            WHERE h.state='open' AND p.enabled=1 AND p.calibrated=1 AND c.intellect=?
              AND (? OR h.next_probe_at IS NOT NULL AND h.next_probe_at<=?)
              AND (h.last_route_recovery_at IS NULL OR h.last_route_recovery_at<=?)
              AND NOT EXISTS(SELECT 1 FROM route_block b WHERE b.fingerprint=h.fingerprint AND b.model=h.model)
            ORDER BY h.last_real_success IS NULL,h.last_real_success DESC,h.updated_at DESC""", (tier, int(allow_before_due), stamp, cooldown_before)).fetchall()
        result = []
        endpoints = set(excluded_endpoints)
        with self.conn:
            for row in rows:
                provider = self.probe_provider(row["fingerprint"], row["model"])
                if provider is None:
                    continue
                endpoint = (provider.provider_type, provider.base_url.rstrip("/"))
                if endpoint in endpoints:
                    continue
                claimed = self.conn.execute("""UPDATE provider_health SET last_route_recovery_at=?,updated_at=?
                    WHERE fingerprint=? AND model=? AND state='open'
                      AND (? OR next_probe_at IS NOT NULL AND next_probe_at<=?)
                      AND (last_route_recovery_at IS NULL OR last_route_recovery_at<=?)""",
                    (stamp, stamp, provider.fingerprint, provider.models[0], int(allow_before_due), stamp, cooldown_before),
                ).rowcount
                if not claimed:
                    continue
                endpoints.add(endpoint)
                result.append(provider)
                if len(result) >= limit:
                    break
        return result

    def probe_provider(self, fingerprint: str, model: str) -> Provider | None:
        """Return one enabled inventory target, including open targets for recovery probes."""
        row = self.conn.execute("""SELECT s.*,p.enabled,p.multiplier,p.calibrated,p.tiers_json,p.max_parallel
            FROM source_provider s JOIN policy p USING(fingerprint) WHERE s.fingerprint=?""", (fingerprint,)).fetchone()
        catalog = self.catalog()
        if row is None or not row['enabled'] or not row['calibrated'] or model not in json.loads(row['models_json']) or model not in catalog:
            return None
        tier = catalog[model]['intellect']
        if tier not in json.loads(row['tiers_json']):
            return None
        headers = json.loads(self._decrypt(row['request_headers'])) if row['request_headers'] else {}
        pricing = catalog[model]
        source = json.loads(row['source_json']) if row['source_json'] else {}
        model_aliases = source.get('model_aliases') or {}
        wire_model = model_aliases.get(model)
        reverse_aliases = {str(wire).casefold(): str(alias) for alias, wire in model_aliases.items()}
        return Provider(row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']), row['provider_type'], headers, [model], pricing, int(blended_price(pricing) * row['multiplier'] * 100000), int(row['max_parallel']), bool(row['enabled']), float(row['multiplier']), row['site_id'], wire_model, reverse_aliases)

    def key_test_providers(self) -> list[Provider]:
        """Return every enabled, calibrated API-key/model probe target.

        A key can expose several Codex models, and a successful probe of one
        model does not establish that the other models work.  Keep each model
        as its own Provider value so health and structured-contract evidence
        remains scoped to the provider/model pair.
        """
        rows = self.conn.execute("""SELECT s.*,p.enabled,p.calibrated,p.tiers_json,p.multiplier,p.max_parallel
            FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id""").fetchall()
        catalog = self.catalog()
        result = []
        for row in rows:
            headers = json.loads(self._decrypt(row['request_headers'])) if row['request_headers'] else {}
            if not row['enabled'] or not row['calibrated']:
                continue
            tiers = set(json.loads(row['tiers_json']))
            for model in json.loads(row['models_json']):
                if model not in catalog or catalog[model]['intellect'] not in tiers:
                    continue
                pricing = catalog[model]
                source = json.loads(row['source_json']) if row['source_json'] else {}
                model_aliases = source.get('model_aliases') or {}
                wire_model = model_aliases.get(model)
                reverse_aliases = {str(wire).casefold(): str(alias) for alias, wire in model_aliases.items()}
                result.append(Provider(
                    row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']),
                    row['provider_type'], headers, [model], pricing,
                    int(blended_price(pricing) * row['multiplier'] * 100000), int(row['max_parallel']),
                    bool(row['enabled']), float(row['multiplier']), row['site_id'], wire_model, reverse_aliases,
                ))
        return result

    def try_acquire(self, provider: Provider) -> bool:
        active = self._inflight.get(provider.fingerprint, 0)
        site = getattr(provider, "site_id", "default")
        site_row = self.conn.execute("SELECT max_parallel,enabled FROM site_policy WHERE site_id=?", (site,)).fetchone()
        site_limit = int(site_row["max_parallel"]) if site_row else 8
        global_limit = int(self.conn.execute("SELECT value FROM broker_setting WHERE name='global_parallel_cap'").fetchone()[0])
        if active >= provider.max_parallel or self._site_inflight.get(site, 0) >= site_limit or self._global_inflight >= global_limit or site_row and not site_row["enabled"]:
            return False
        self._inflight[provider.fingerprint] = active + 1
        self._site_inflight[site] = self._site_inflight.get(site, 0) + 1
        self._global_inflight += 1
        return True

    def has_capacity(self, provider: Provider) -> bool:
        site = getattr(provider, "site_id", "default")
        row = self.conn.execute("SELECT max_parallel,enabled FROM site_policy WHERE site_id=?", (site,)).fetchone()
        site_limit = int(row["max_parallel"]) if row else 8
        global_limit = int(self.conn.execute("SELECT value FROM broker_setting WHERE name='global_parallel_cap'").fetchone()[0])
        return self._inflight.get(provider.fingerprint, 0) < provider.max_parallel and self._site_inflight.get(site, 0) < site_limit and self._global_inflight < global_limit and (row is None or bool(row["enabled"]))

    def release(self, provider: Provider):
        active = self._inflight.get(provider.fingerprint, 0)
        if active <= 1:
            self._inflight.pop(provider.fingerprint, None)
        else:
            self._inflight[provider.fingerprint] = active - 1
        site = getattr(provider, "site_id", "default")
        site_active = self._site_inflight.get(site, 0)
        if site_active <= 1:
            self._site_inflight.pop(site, None)
        else:
            self._site_inflight[site] = site_active - 1
        self._global_inflight = max(0, self._global_inflight - 1)

    def block_route(self, fingerprint: str, model: str):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO route_block(fingerprint,model) VALUES(?,?)',(fingerprint,model))

    def route_score(self, provider: Provider, requested_model: str, body: dict) -> int:
        """Rank same-band candidates using recent, safe request-shape outcomes."""
        prompt = body.get('prompt') if isinstance(body.get('prompt'), str) else ''
        schema = body.get('output_schema') if isinstance(body.get('output_schema'), dict) else None
        if schema is None:
            try:
                envelope = json.loads(prompt)
            except (TypeError, ValueError):
                envelope = None
            embedded = envelope.get('output_schema') if isinstance(envelope, dict) else None
            schema = embedded if isinstance(embedded, dict) else None
        prompt_sha256 = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        schema_sha256 = None
        if schema is not None:
            encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            schema_sha256 = hashlib.sha256(encoded).hexdigest()
        rows = self.conn.execute("""
            SELECT actual_model,status,latency_ms,diagnostic_json FROM observation
            WHERE fingerprint=? AND requested_model=? AND created_at>=datetime('now','-24 hours')
            ORDER BY id DESC LIMIT 100
        """, (provider.fingerprint, requested_model)).fetchall()
        score = 0
        health = self.health(provider.fingerprint, requested_model)
        if health.get('state') in {'healthy', 'half_open'}:
            score += 100
        elif health.get('state') == 'suspect':
            score -= 10
        for row in rows:
            try:
                diagnostic = json.loads(row['diagnostic_json']) if row['diagnostic_json'] else {}
            except (TypeError, ValueError):
                diagnostic = {}
            exact_shape = diagnostic.get('prompt_sha256') == prompt_sha256 and diagnostic.get('schema_sha256') == schema_sha256
            fulfilled = row['status'] == 'completed' and row['actual_model'] == requested_model
            # Hedge cancellations and client disconnects say nothing about a
            # provider's capability; including them was the main source of the
            # misleading attempt-level success score.
            neutral = row['status'] in {'cancelled', 'client_cancelled'}
            if exact_shape:
                score += 1000 if fulfilled else 0 if neutral else -100 if row['status'] == 'structured_output_invalid' else -20
            else:
                score += 10 if fulfilled else 0 if neutral else -1
            if not exact_shape and fulfilled and row['latency_ms'] is not None:
                score += max(-20, min(20, int((2500 - float(row['latency_ms'])) / 125)))
        return score

    def inventory(self, window='24h') -> list[dict]:
        modifier = {'1h': '-1 hour', '24h': '-24 hours', '7d': '-7 days', '30d': '-30 days'}[window]
        rows = self.conn.execute("SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.note,p.max_parallel,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id").fetchall()
        inventory = []
        for row in rows:
            stats = self.conn.execute(
                "SELECT avg(success) rate, avg(latency_ms) ttft, sum(cost) cost, sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN input_tokens + output_tokens END) total_tokens FROM observation WHERE fingerprint=? AND created_at>=datetime('now',?)",
                (row['fingerprint'], modifier),
            ).fetchone()
            latest = self.conn.execute("""SELECT evidence_at,ttft_ms,status FROM (
                    SELECT created_at evidence_at,latency_ms ttft_ms,status,1 source_priority
                    FROM observation WHERE fingerprint=? AND latency_ms IS NOT NULL
                    UNION ALL
                    SELECT created_at evidence_at,ttft_ms,COALESCE(error_type,'completed') status,0 source_priority
                    FROM probe_event WHERE fingerprint=?
                ) ORDER BY julianday(evidence_at) DESC,source_priority DESC LIMIT 1""",
                (row['fingerprint'], row['fingerprint']),
            ).fetchone()
            inventory.append({
                'fingerprint': row['fingerprint'], 'name': row['name'], 'base_url': row['base_url'], 'family': row['provider_type'],
                'api_key_mask': row['api_key_mask'] or '***', 'models': json.loads(row['models_json']),
                'inventory_status': json.loads(row['source_json']).get('inventory_status'), 'enabled': bool(row['enabled']),
                'calibrated': bool(row['calibrated']), 'note': row['note'], 'max_parallel': row['max_parallel'],
                'multiplier': row['multiplier'], 'technical_success_rate': stats['rate'], 'avg_ttft_ms': stats['ttft'],
                'cost_24h': stats['cost'], 'total_tokens': stats['total_tokens'], 'tiers': json.loads(row['tiers_json']), 'synced_at': row['synced_at'],
                'last_test_at': latest['evidence_at'] if latest else None,
                'last_test_ttft_ms': latest['ttft_ms'] if latest else None,
                'last_test_status': latest['status'] if latest else None,
            })
        return inventory

    def update_policy(self, fingerprint: str, body: dict):
        with self.conn:
            current=self.conn.execute('SELECT * FROM policy WHERE fingerprint=?',(fingerprint,)).fetchone()
            if current is None: return False
            self.conn.execute("UPDATE policy SET enabled=?,multiplier=?,calibrated=?,note=?,max_parallel=?,tiers_json=? WHERE fingerprint=?", (int(body.get("enabled",current['enabled'])),float(body.get('multiplier',current['multiplier'])),int(body.get('calibrated',current['calibrated'])),str(body.get('note',current['note'])),int(body.get('max_parallel',current['max_parallel'])),json.dumps(body.get("tiers",json.loads(current['tiers_json']))),fingerprint))
        return True

    def observe(self, **data):
        payload = {
            'diagnostic_json': None, 'route_id': None, 'attempt_number': None,
            'started_ms': None, 'elapsed_ms': None,
        } | data
        diagnostic = payload.pop("diagnostic", None)
        if diagnostic:
            payload["diagnostic_json"] = json.dumps(diagnostic, sort_keys=True)
        with self.conn:
            self.conn.execute("""INSERT INTO observation(
                fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,
                input_tokens,output_tokens,cost,request_id,diagnostic_json,route_id,attempt_number,started_ms,elapsed_ms
            ) VALUES(
                :fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error,:status,
                :input_tokens,:output_tokens,:cost,:request_id,:diagnostic_json,:route_id,:attempt_number,:started_ms,:elapsed_ms
            )""", payload)

    def quality(self, window='24h'):
        modifier={'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]
        where="created_at >= datetime('now', ?)"; params=(modifier,)
        row=self.conn.execute(f'SELECT count(*) calls, avg(success) rate, avg(latency_ms) ttft, sum(cost) total_cost FROM observation WHERE {where}',params).fetchone()
        values=[r[0] for r in self.conn.execute(f'SELECT latency_ms FROM observation WHERE {where} AND latency_ms IS NOT NULL ORDER BY latency_ms',params).fetchall()]
        p95=values[max(0, int(len(values)*.95)-1)] if values else None
        fulfillment=self.conn.execute(f'SELECT avg(actual_model=requested_model) FROM observation WHERE {where}',params).fetchone()[0]
        failures={s:self.conn.execute(f'SELECT count(*) FROM observation WHERE {where} AND status=?',params+(s,)).fetchone()[0] for s in ('cancelled','timed_out','transport_failed','protocol_failed','stream_incomplete')}
        route_where = "started_at >= datetime('now', ?)"
        route_row = self.conn.execute(f"""SELECT count(*) calls,
            avg(outcome='completed') request_success_rate, avg(first_delta_ms) client_first_delta_avg_ms,
            avg(completed_ms) request_completed_avg_ms
            FROM route_run WHERE {route_where} AND outcome IN ('completed','failed','timed_out')""", params).fetchone()
        deltas = [r[0] for r in self.conn.execute(f"SELECT first_delta_ms FROM route_run WHERE {route_where} AND first_delta_ms IS NOT NULL ORDER BY first_delta_ms", params).fetchall()]
        complete = [r[0] for r in self.conn.execute(f"SELECT completed_ms FROM route_run WHERE {route_where} AND completed_ms IS NOT NULL ORDER BY completed_ms", params).fetchall()]
        cancelled = self.conn.execute(f"SELECT count(*) FROM observation WHERE {where} AND status IN ('cancelled','client_cancelled')", params).fetchone()[0]
        percentile = lambda values, fraction: values[max(0, int(len(values) * fraction) - 1)] if values else None
        route_rows = self.conn.execute(f"SELECT outcome,telemetry_version,delivery_mode,first_delta_ms FROM route_run WHERE {route_where}", params).fetchall()
        outcomes = {name: 0 for name in ("completed", "failed", "timed_out", "client_cancelled", "validation_rejected", "in_progress", "unknown")}
        complete_telemetry = []
        plain_stream_deltas = []
        plain_stream_applicable = 0
        for route in route_rows:
            outcome = route["outcome"]
            outcomes[outcome if outcome in outcomes else "in_progress" if outcome is None else "unknown"] += 1
            if route["telemetry_version"] == TELEMETRY_SCHEMA_VERSION:
                complete_telemetry.append(route)
            if route["delivery_mode"] == "plain_stream":
                plain_stream_applicable += 1
                if route["first_delta_ms"] is not None:
                    plain_stream_deltas.append(route["first_delta_ms"])
        known_denominator = outcomes["completed"] + outcomes["failed"] + outcomes["timed_out"]
        earliest = self.conn.execute("SELECT min(started_at) FROM route_run WHERE telemetry_version=?", (TELEMETRY_SCHEMA_VERSION,)).fetchone()[0]
        telemetry_total = len(route_rows)
        first_forwarded = sorted(plain_stream_deltas)
        client_deltas = [row[0] for row in self.conn.execute(f"""SELECT t.elapsed_ms FROM client_telemetry t
            JOIN route_run r USING(route_id) WHERE r.{route_where} AND t.metric_type='client_first_delta'
            ORDER BY t.elapsed_ms""", params).fetchall()]
        delivery_latency = {}
        for mode in DELIVERY_MODES:
            mode_rows = [route for route in route_rows if route["delivery_mode"] == mode]
            forwarded = sorted(route["first_delta_ms"] for route in mode_rows if route["first_delta_ms"] is not None)
            completion = sorted(
                row[0] for row in self.conn.execute(
                    f"SELECT coalesce(validation_completed_ms,completed_ms) FROM route_run WHERE {route_where} "
                    "AND delivery_mode=? AND coalesce(validation_completed_ms,completed_ms) IS NOT NULL "
                    "ORDER BY coalesce(validation_completed_ms,completed_ms)", (*params, mode)
                ).fetchall()
            )
            delivery_latency[mode] = {
                "first_forwarded_delta": {
                    "applicable_count": len(mode_rows) if mode == "plain_stream" else 0,
                    "sample_count": len(forwarded) if mode == "plain_stream" else 0,
                    "p50_ms": percentile(forwarded, .5) if mode == "plain_stream" else None,
                    "p95_ms": percentile(forwarded, .95) if mode == "plain_stream" else None,
                },
                "valid_completion": {
                    "applicable_count": len(mode_rows) if mode != "plain_stream" else 0,
                    "sample_count": len(completion) if mode != "plain_stream" else 0,
                    "p50_ms": percentile(completion, .5) if mode != "plain_stream" else None,
                    "p95_ms": percentile(completion, .95) if mode != "plain_stream" else None,
                },
            }
        route_ids = [row[0] for row in self.conn.execute(f"SELECT route_id FROM route_run WHERE {route_where}", params).fetchall()]
        amplifications = [self.route_amplification(route_id) for route_id in route_ids]
        attempts = sorted(item["attempts_started"] for item in amplifications)
        hedges = [item for item in amplifications if item["hedge_started"]]
        costs_known = [item for item in amplifications if item["cost_attempts"]]
        return {
            'calls': row['calls'], 'technical_success_rate': row['rate'], 'avg_ttft_ms': row['ttft'], 'p95_ttft_ms': p95,
            'total_cost': row['total_cost'], 'model_fulfillment_rate': fulfillment, 'failures': failures,
            'request_calls': route_row['calls'], 'request_success_rate': route_row['request_success_rate'],
            'client_first_delta_avg_ms': route_row['client_first_delta_avg_ms'],
            'client_first_delta_p50_ms': percentile(deltas, .5), 'client_first_delta_p95_ms': percentile(deltas, .95),
            'request_completed_avg_ms': route_row['request_completed_avg_ms'],
            'request_completed_p95_ms': percentile(complete, .95), 'cancellation_neutral_attempts': cancelled,
            'telemetry_schema_version': TELEMETRY_SCHEMA_VERSION,
            'collection_started_at': earliest,
            'request_outcomes': outcomes,
            'request_success_numerator': outcomes['completed'],
            'request_success_denominator': known_denominator,
            'request_excluded_count': telemetry_total - known_denominator,
            'request_coverage': len(complete_telemetry) / telemetry_total if telemetry_total else None,
            'first_forwarded_delta': {
                'metric_version': TELEMETRY_SCHEMA_VERSION,
                'applicable_count': plain_stream_applicable,
                'sample_count': len(first_forwarded),
                'excluded_count': telemetry_total - plain_stream_applicable,
                'p50_ms': percentile(first_forwarded, .5),
                'p95_ms': percentile(first_forwarded, .95),
            },
            'client_first_delta': {
                'metric_version': TELEMETRY_SCHEMA_VERSION, 'applicable_count': plain_stream_applicable,
                'sample_count': len(client_deltas), 'coverage': len(client_deltas) / plain_stream_applicable if plain_stream_applicable else None,
                'p50_ms': percentile(client_deltas, .5), 'p95_ms': percentile(client_deltas, .95),
            },
            'delivery_latency': delivery_latency,
            'amplification': {
                'sample_count': len(amplifications), 'attempts_p50': percentile(attempts, .5),
                'attempts_p95': percentile(attempts, .95), 'hedge_started_numerator': len(hedges),
                'hedge_started_denominator': len(amplifications),
                'hedge_rescue_numerator': sum(item['hedge_rescue'] for item in hedges),
                'hedge_rescue_denominator': len(hedges),
                'cross_site_count': sum(item['distinct_sites'] > 1 for item in amplifications),
                'cost_coverage': len(costs_known) / len(amplifications) if amplifications else None,
            },
        }
    def calls(self, limit, cursor=None, provider=None, status=None, window='24h', sort='time', direction='desc', offset=None):
        clauses=["o.created_at >= datetime('now', ?)"]; params=[{'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]]
        if cursor: clauses.append('o.id < ?'); params.append(int(cursor))
        if provider: clauses.append('(o.fingerprint=? OR s.name=?)'); params.extend((provider,provider))
        if status: clauses.append('o.status=?'); params.append(status)
        order_columns={'time':'o.created_at','note':'p.note','provider':'COALESCE(s.name,o.fingerprint)','requested_model':'o.requested_model','actual_model':'o.actual_model','intellect':'o.tier','effort':'o.effort','ttft':'o.latency_ms','status':'o.status','input_tokens':'o.input_tokens','output_tokens':'o.output_tokens','cost':'o.cost','request_id':'o.request_id'}
        query='SELECT o.*,s.name provider_name,p.note FROM observation o LEFT JOIN source_provider s ON s.fingerprint=o.fingerprint LEFT JOIN policy p ON p.fingerprint=o.fingerprint WHERE '+ ' AND '.join(clauses)+f' ORDER BY {order_columns[sort]} {direction.upper()}, o.id DESC LIMIT ?'
        values=[*params,limit]
        if offset is not None: query += ' OFFSET ?'; values.append(offset)
        rows=self.conn.execute(query,values).fetchall()
        return [{
            'id':r['id'],'time':r['created_at'],'provider':r['provider_name'] or r['fingerprint'],
            'note':r['note'],'requested_model':r['requested_model'],'actual_model':r['actual_model'],
            'intellect':r['tier'],'effort':r['effort'],'ttft_ms':r['latency_ms'],'status':r['status'],
            'input_tokens':r['input_tokens'],'output_tokens':r['output_tokens'],'cost':r['cost'],
            'request_id':r['request_id'],'route_id':r['route_id'],'attempt_number':r['attempt_number'],
            'started_ms':r['started_ms'],'elapsed_ms':r['elapsed_ms'],
            'diagnostic':json.loads(r['diagnostic_json']) if r['diagnostic_json'] else {},
        } for r in rows]
