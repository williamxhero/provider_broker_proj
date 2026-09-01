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


class Store:
    def __init__(self, path: Path, encryption_key: bytes, default_race_parallel_cap: int = 3):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.aes = AESGCM(encryption_key)
        self._inflight: dict[str, int] = {}
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
        for name, definition in [('input_tokens','INTEGER'),('output_tokens','INTEGER'),('cost','REAL'),('request_id','TEXT'),('diagnostic_json','TEXT')]:
            try: self.conn.execute(f'ALTER TABLE observation ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE source_provider ADD COLUMN request_headers BLOB')
        except sqlite3.OperationalError: pass
        if not catalog_exists:
            from .catalog import CATALOG
            self.conn.executemany(
                "INSERT INTO model_catalog VALUES(?,?,?,?,?,?)",
                [(model, item['family'], item['intellect'], item['official_input_price'], item['official_cache_price'], item['official_output_price']) for model, item in CATALOG.items()],
            )
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('race_parallel_cap',?)", (str(self.default_race_parallel_cap),))
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('hedge_delay_ms','750')")
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
            state = "half_open" if not real and state == "open" else "healthy"
        else:
            failures += 1
            if immediate_open or failures >= 3:
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

    @staticmethod
    def fingerprint(base_url: str, api_key: str, model: str) -> str:
        return hmac.new(b"provider-broker-source-v1", f"{base_url}\0{api_key}".encode(), hashlib.sha256).hexdigest()

    def replace_source_snapshot(self, entries: list[dict], synced_at: str):
        rows = []
        site_notes = []
        catalog = self.catalog()
        for entry in entries:
            base_url, api_key = entry["base_url"].rstrip("/"), entry["api_key"]
            from .catalog import canonicalize
            source_models = list(dict.fromkeys(canonicalize(model) for model in (entry.get("models") or [entry.get("model", "unavailable")])))
            models = [model for model in source_models if model in catalog]
            fp = self.fingerprint(base_url, api_key, "\0".join(source_models))
            if isinstance(entry.get('site_name'), str) and entry['site_name'].strip():
                site_notes.append((entry['site_name'].strip(), fp))
            source = entry.get("source", {}) | {"inventory_status":entry.get("inventory_status","unavailable")}
            request_headers = json.dumps(entry.get('request_headers') or {}, sort_keys=True)
            rows.append((fp, entry.get("name") or (models[0] if models else "unavailable"), base_url, self._encrypt(api_key), entry.get("provider_type", "openai"), self._encrypt(request_headers), json.dumps(models), json.dumps(source), synced_at))
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE incoming AS SELECT * FROM source_provider WHERE 0")
            self.conn.executemany("INSERT INTO incoming(fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
            self.conn.execute("DELETE FROM source_provider")
            self.conn.execute("INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at) SELECT fingerprint,name,base_url,api_key,provider_type,request_headers,models_json,source_json,synced_at FROM incoming")
            self.conn.execute("DROP TABLE incoming")
            self.conn.execute("DELETE FROM route_block")
            self.conn.executemany("INSERT OR IGNORE INTO policy(fingerprint) VALUES(?)", [(r[0],) for r in rows])
            self.conn.executemany("UPDATE policy SET calibrated=? WHERE fingerprint=?", [(int(any(model in catalog for model in json.loads(r[6]))), r[0]) for r in rows])
            self.conn.executemany("UPDATE policy SET note=? WHERE fingerprint=?", site_notes)

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
                    result.append(Provider(r['id'],r['fingerprint'],r['name'],r['base_url'],self._decrypt(r['api_key']),r['provider_type'],headers,[model],pricing,int(blended_price(pricing)*r['multiplier']*100000),int(r['max_parallel']),bool(r['enabled']),float(r['multiplier'])))
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
        return Provider(row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']), row['provider_type'], headers, [model], pricing, int(blended_price(pricing) * row['multiplier'] * 100000), int(row['max_parallel']), bool(row['enabled']), float(row['multiplier']))

    def try_acquire(self, provider: Provider) -> bool:
        active = self._inflight.get(provider.fingerprint, 0)
        if active >= provider.max_parallel:
            return False
        self._inflight[provider.fingerprint] = active + 1
        return True

    def has_capacity(self, provider: Provider) -> bool:
        return self._inflight.get(provider.fingerprint, 0) < provider.max_parallel

    def release(self, provider: Provider):
        active = self._inflight.get(provider.fingerprint, 0)
        if active <= 1:
            self._inflight.pop(provider.fingerprint, None)
        else:
            self._inflight[provider.fingerprint] = active - 1

    def block_route(self, fingerprint: str, model: str):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO route_block(fingerprint,model) VALUES(?,?)',(fingerprint,model))

    def inventory(self, window='24h') -> list[dict]:
        modifier = {'1h': '-1 hour', '24h': '-24 hours', '7d': '-7 days', '30d': '-30 days'}[window]
        rows = self.conn.execute("SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.note,p.max_parallel,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id").fetchall()
        inventory = []
        for row in rows:
            stats = self.conn.execute(
                "SELECT avg(success) rate, avg(latency_ms) ttft, sum(cost) cost FROM observation WHERE fingerprint=? AND created_at>=datetime('now',?)",
                (row['fingerprint'], modifier),
            ).fetchone()
            api_key = self._decrypt(row['api_key'])
            inventory.append({
                'fingerprint': row['fingerprint'], 'name': row['name'], 'base_url': row['base_url'], 'family': row['provider_type'],
                'api_key_mask': api_key[:3] + '***' + api_key[-3:], 'models': json.loads(row['models_json']),
                'inventory_status': json.loads(row['source_json']).get('inventory_status'), 'enabled': bool(row['enabled']),
                'calibrated': bool(row['calibrated']), 'note': row['note'], 'max_parallel': row['max_parallel'],
                'multiplier': row['multiplier'], 'technical_success_rate': stats['rate'], 'avg_ttft_ms': stats['ttft'],
                'cost_24h': stats['cost'], 'tiers': json.loads(row['tiers_json']), 'synced_at': row['synced_at'],
            })
        return inventory

    def update_policy(self, fingerprint: str, body: dict):
        with self.conn:
            current=self.conn.execute('SELECT * FROM policy WHERE fingerprint=?',(fingerprint,)).fetchone()
            if current is None: return False
            self.conn.execute("UPDATE policy SET enabled=?,multiplier=?,calibrated=?,note=?,max_parallel=?,tiers_json=? WHERE fingerprint=?", (int(body.get("enabled",current['enabled'])),float(body.get('multiplier',current['multiplier'])),int(body.get('calibrated',current['calibrated'])),str(body.get('note',current['note'])),int(body.get('max_parallel',current['max_parallel'])),json.dumps(body.get("tiers",json.loads(current['tiers_json']))),fingerprint))
        return True

    def observe(self, **data):
        data["diagnostic_json"] = json.dumps(data.pop("diagnostic", None), sort_keys=True) if data.get("diagnostic") else None
        with self.conn:
            self.conn.execute("INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,input_tokens,output_tokens,cost,request_id,diagnostic_json) VALUES(:fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error,:status,:input_tokens,:output_tokens,:cost,:request_id,:diagnostic_json)", data)

    def quality(self, window='24h'):
        modifier={'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]
        where="created_at >= datetime('now', ?)"; params=(modifier,)
        row=self.conn.execute(f'SELECT count(*) calls, avg(success) rate, avg(latency_ms) ttft, sum(cost) total_cost FROM observation WHERE {where}',params).fetchone()
        values=[r[0] for r in self.conn.execute(f'SELECT latency_ms FROM observation WHERE {where} AND latency_ms IS NOT NULL ORDER BY latency_ms',params).fetchall()]
        p95=values[max(0, int(len(values)*.95)-1)] if values else None
        fulfillment=self.conn.execute(f'SELECT avg(actual_model=requested_model) FROM observation WHERE {where}',params).fetchone()[0]
        failures={s:self.conn.execute(f'SELECT count(*) FROM observation WHERE {where} AND status=?',params+(s,)).fetchone()[0] for s in ('cancelled','timed_out','transport_failed','protocol_failed','stream_incomplete')}
        return {'calls':row['calls'],'technical_success_rate':row['rate'],'avg_ttft_ms':row['ttft'],'p95_ttft_ms':p95,'total_cost':row['total_cost'],'model_fulfillment_rate':fulfillment,'failures':failures}
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
        return [{'id':r['id'],'time':r['created_at'],'provider':r['provider_name'] or r['fingerprint'],'note':r['note'],'requested_model':r['requested_model'],'actual_model':r['actual_model'],'intellect':r['tier'],'effort':r['effort'],'ttft_ms':r['latency_ms'],'status':r['status'],'input_tokens':r['input_tokens'],'output_tokens':r['output_tokens'],'cost':r['cost'],'request_id':r['request_id'],'diagnostic':json.loads(r['diagnostic_json']) if r['diagnostic_json'] else None} for r in rows]
