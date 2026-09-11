import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog import blended_price


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
          request_headers BLOB, models_json TEXT NOT NULL, source_json TEXT NOT NULL, synced_at TEXT NOT NULL
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
          selected_model TEXT, selected_site_id TEXT, terminal_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS route_run_started ON route_run(started_at DESC);
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
        if not catalog_exists:
            from .catalog import CATALOG
            self.conn.executemany(
                "INSERT INTO model_catalog VALUES(?,?,?,?,?,?)",
                [(model, item['family'], item['intellect'], item['official_input_price'], item['official_cache_price'], item['official_output_price']) for model, item in CATALOG.items()],
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
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
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

    def route_started(self, route_id: str, tier: str, request_id: str, effort: str | None = None) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO route_run(route_id,tier,effort,request_id) VALUES(?,?,?,?)", (route_id, tier, effort, request_id))

    def route_finished(self, route_id: str, *, outcome: str, first_delta_ms: float | None = None,
                       completed_ms: float | None = None, selected_fingerprint: str | None = None,
                       selected_model: str | None = None, selected_site_id: str | None = None,
                       terminal_reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""UPDATE route_run SET completed_at=?,outcome=?,first_delta_ms=?,completed_ms=?,
                selected_fingerprint=?,selected_model=?,selected_site_id=?,terminal_reason=? WHERE route_id=?""", (
                self._timestamp(), outcome, first_delta_ms, completed_ms, selected_fingerprint,
                selected_model, selected_site_id, terminal_reason, route_id,
            ))

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
    def fingerprint(base_url: str, api_key: str, model: str) -> str:
        return hmac.new(b"provider-broker-source-v1", f"{base_url}\0{api_key}".encode(), hashlib.sha256).hexdigest()

    def replace_source_snapshot(self, entries: list[dict], synced_at: str):
        rows = []
        site_notes = []
        catalog = self.catalog()
        existing = {}
        for row in self.conn.execute("SELECT fingerprint,base_url,api_key,models_json FROM source_provider"):
            try:
                existing[(row["base_url"], self._decrypt(row["api_key"]))] = row
            except Exception:
                continue
        for entry in entries:
            base_url, api_key = entry["base_url"].rstrip("/"), entry["api_key"]
            from .catalog import canonicalize
            source_models = list(dict.fromkeys(canonicalize(model) for model in (entry.get("models") or [entry.get("model", "unavailable")])))
            models = [model for model in source_models if model in catalog]
            prior = existing.get((base_url, api_key))
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
                    fp = self.fingerprint(base_url, api_key, "\0".join(source_models))
            else:
                fp = self.fingerprint(base_url, api_key, "\0".join(source_models))
            site_id = self.site_id(entry.get("site_name"), base_url)
            if isinstance(entry.get('site_name'), str) and entry['site_name'].strip():
                site_notes.append((entry['site_name'].strip(), fp))
            source = entry.get("source", {}) | {"inventory_status":entry.get("inventory_status","unavailable")}
            request_headers = json.dumps(entry.get('request_headers') or {}, sort_keys=True)
            rows.append((fp, entry.get("name") or (models[0] if models else "unavailable"), base_url, self._encrypt(api_key), entry.get("provider_type", "openai"), self._encrypt(request_headers), json.dumps(models), json.dumps(source), synced_at, site_id))
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE incoming AS SELECT * FROM source_provider WHERE 0")
            self.conn.executemany("INSERT INTO incoming(fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at,site_id) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.execute("DELETE FROM source_provider")
            self.conn.execute("INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at,site_id) SELECT fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at,site_id FROM incoming")
            self.conn.execute("DELETE FROM route_block WHERE fingerprint NOT IN (SELECT fingerprint FROM incoming)")
            self.conn.execute("DROP TABLE incoming")
            self.conn.executemany("INSERT OR IGNORE INTO policy(fingerprint) VALUES(?)", [(r[0],) for r in rows])
            self.conn.executemany("INSERT OR IGNORE INTO site_policy(site_id) VALUES(?)", [(r[9],) for r in rows])
            self.conn.executemany("UPDATE policy SET calibrated=? WHERE fingerprint=?", [(int(any(model in catalog for model in json.loads(r[6]))), r[0]) for r in rows])
            self.conn.executemany("UPDATE policy SET note=? WHERE fingerprint=?", site_notes)

    @staticmethod
    def site_id(site_name: object, base_url: str) -> str:
        """Stable, non-secret fault-domain identifier from CPA site metadata."""
        value = str(site_name or "").strip().lower()
        if not value:
            from urllib.parse import urlsplit
            value = urlsplit(base_url).hostname or base_url
        return "".join(char if char.isalnum() or char in "._-" else "-" for char in value)[:80] or "default"

    def providers(self, tier: str) -> list[Provider]:
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
                    if not self.health_allows_route(r['fingerprint'], model):
                        continue
                    pricing = catalog[model]
                    result.append(Provider(r['id'],r['fingerprint'],r['name'],r['base_url'],self._decrypt(r['api_key']),r['provider_type'],headers,[model],pricing,int(blended_price(pricing)*r['multiplier']*100000),int(r['max_parallel']),bool(r['enabled']),float(r['multiplier']),r['site_id']))
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
        return Provider(row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']), row['provider_type'], headers, [model], pricing, int(blended_price(pricing) * row['multiplier'] * 100000), int(row['max_parallel']), bool(row['enabled']), float(row['multiplier']), row['site_id'])

    def key_test_providers(self) -> list[Provider]:
        """Return one representative model for every API key in inventory."""
        rows = self.conn.execute("""SELECT s.*,p.enabled,p.multiplier,p.max_parallel
            FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id""").fetchall()
        catalog = self.catalog()
        result = []
        for row in rows:
            model = next((item for item in json.loads(row['models_json']) if item in catalog), None)
            if model is None:
                continue
            headers = json.loads(self._decrypt(row['request_headers'])) if row['request_headers'] else {}
            pricing = catalog[model]
            result.append(Provider(
                row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']),
                row['provider_type'], headers, [model], pricing,
                int(blended_price(pricing) * row['multiplier'] * 100000), int(row['max_parallel']),
                bool(row['enabled']), float(row['multiplier']), row['site_id'],
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
                "SELECT avg(success) rate, avg(latency_ms) ttft, sum(cost) cost FROM observation WHERE fingerprint=? AND created_at>=datetime('now',?)",
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
            api_key = self._decrypt(row['api_key'])
            inventory.append({
                'fingerprint': row['fingerprint'], 'name': row['name'], 'base_url': row['base_url'], 'family': row['provider_type'],
                'api_key_mask': api_key[:3] + '***' + api_key[-3:], 'models': json.loads(row['models_json']),
                'inventory_status': json.loads(row['source_json']).get('inventory_status'), 'enabled': bool(row['enabled']),
                'calibrated': bool(row['calibrated']), 'note': row['note'], 'max_parallel': row['max_parallel'],
                'multiplier': row['multiplier'], 'technical_success_rate': stats['rate'], 'avg_ttft_ms': stats['ttft'],
                'cost_24h': stats['cost'], 'tiers': json.loads(row['tiers_json']), 'synced_at': row['synced_at'],
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
        return {
            'calls': row['calls'], 'technical_success_rate': row['rate'], 'avg_ttft_ms': row['ttft'], 'p95_ttft_ms': p95,
            'total_cost': row['total_cost'], 'model_fulfillment_rate': fulfillment, 'failures': failures,
            'request_calls': route_row['calls'], 'request_success_rate': route_row['request_success_rate'],
            'client_first_delta_avg_ms': route_row['client_first_delta_avg_ms'],
            'client_first_delta_p50_ms': percentile(deltas, .5), 'client_first_delta_p95_ms': percentile(deltas, .95),
            'request_completed_avg_ms': route_row['request_completed_avg_ms'],
            'request_completed_p95_ms': percentile(complete, .95), 'cancellation_neutral_attempts': cancelled,
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
