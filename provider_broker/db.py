import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class Provider:
    id: int
    fingerprint: str
    name: str
    base_url: str
    api_key: str
    provider_type: str
    models: list[str]
    price_group: int
    enabled: bool


class Store:
    def __init__(self, path: Path, encryption_key: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.aes = AESGCM(encryption_key)
        self._migrate()

    def _migrate(self):
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS source_provider (
          id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          base_url TEXT NOT NULL, api_key BLOB NOT NULL, provider_type TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS catalog_calibration (model TEXT PRIMARY KEY, family TEXT NOT NULL, intellect TEXT NOT NULL, input_price REAL NOT NULL, cache_price REAL NOT NULL, output_price REAL NOT NULL);
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
        for name, definition in [('input_tokens','INTEGER'),('output_tokens','INTEGER'),('cost','REAL'),('request_id','TEXT')]:
            try: self.conn.execute(f'ALTER TABLE observation ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError: pass
        self.conn.commit()

    def save_calibration(self, model, body):
        with self.conn: self.conn.execute('INSERT OR REPLACE INTO catalog_calibration VALUES(?,?,?,?,?,?)',(model,body['family'],body['intellect'],body['official_input_price'],body['official_cache_price'],body['official_output_price']))
    def calibrations(self):
        return {r['model']:{'family':r['family'],'intellect':r['intellect'],'official_input_price':r['input_price'],'official_cache_price':r['cache_price'],'official_output_price':r['output_price'],'available_provider_count':0} for r in self.conn.execute('SELECT * FROM catalog_calibration')}
    def catalog_counts(self):
        from .catalog import CATALOG
        rows=self.conn.execute('SELECT fingerprint,models_json FROM source_provider').fetchall(); counts={name:0 for name in CATALOG}
        for name in counts:
            counts[name]=len({r['fingerprint'] for r in rows if any(name in str(m).lower() for m in json.loads(r['models_json']))})
        return counts

    def _encrypt(self, value: str) -> bytes:
        nonce = __import__('os').urandom(12)
        return nonce + self.aes.encrypt(nonce, value.encode(), None)

    def _decrypt(self, value: bytes) -> str:
        return self.aes.decrypt(value[:12], value[12:], None).decode()

    @staticmethod
    def fingerprint(base_url: str, api_key: str, model: str) -> str:
        return hmac.new(b"provider-broker-source-v1", f"{base_url}\0{api_key}\0{model}".encode(), hashlib.sha256).hexdigest()

    def replace_source_snapshot(self, entries: list[dict], synced_at: str):
        rows = []
        for entry in entries:
            base_url, api_key = entry["base_url"].rstrip("/"), entry["api_key"]
            models = entry.get("models") or [entry.get("model", "unavailable")]
            fp = self.fingerprint(base_url, api_key, "\0".join(models))
            source = entry.get("source", {}) | {"inventory_status":entry.get("inventory_status","unavailable")}
            rows.append((fp, entry.get("name", models[0]), base_url, self._encrypt(api_key), entry.get("provider_type", "openai"), json.dumps(models), json.dumps(source), synced_at))
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE incoming AS SELECT * FROM source_provider WHERE 0")
            self.conn.executemany("INSERT INTO incoming(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) VALUES(?,?,?,?,?,?,?,?)", rows)
            self.conn.execute("DELETE FROM source_provider")
            self.conn.execute("INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at) SELECT fingerprint,name,base_url,api_key,provider_type,models_json,source_json,synced_at FROM incoming")
            self.conn.execute("DROP TABLE incoming")
            self.conn.executemany("INSERT OR IGNORE INTO policy(fingerprint) VALUES(?)", [(r[0],) for r in rows])

    def providers(self, tier: str) -> list[Provider]:
        rows = self.conn.execute("""SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint)
        WHERE p.enabled=1 AND p.calibrated=1 ORDER BY p.price_group, s.id""").fetchall()
        from .catalog import classify
        result=[]
        for r in rows:
            models=[m for m in json.loads(r['models_json']) if classify(m) and classify(m)[0] == tier]
            if models and tier in json.loads(r['tiers_json']):
                result.append(Provider(r['id'],r['fingerprint'],r['name'],r['base_url'],self._decrypt(r['api_key']),r['provider_type'],models,int(classify(models[0])[1]*r['multiplier']*100),bool(r['enabled'])))
        return result

    def inventory(self) -> list[dict]:
        rows = self.conn.execute("SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.note,p.preference,p.max_parallel,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id").fetchall()
        return [{"fingerprint":r["fingerprint"],"name":r["name"],"base_url":r["base_url"],"family":r["provider_type"],"api_key_mask":self._decrypt(r['api_key'])[:3]+'***'+self._decrypt(r['api_key'])[-3:],"models":json.loads(r["models_json"]),"inventory_status":json.loads(r['source_json']).get('inventory_status'),"enabled":bool(r["enabled"]),"calibrated":bool(r["calibrated"]),"note":r['note'],"preference":r['preference'],"max_parallel":r['max_parallel'],"multiplier":r['multiplier'],"technical_success_rate":self.conn.execute("SELECT avg(success) FROM observation WHERE fingerprint=? AND created_at>=datetime('now','-24 hours')",(r['fingerprint'],)).fetchone()[0],"avg_ttft_ms":self.conn.execute("SELECT avg(latency_ms) FROM observation WHERE fingerprint=? AND created_at>=datetime('now','-24 hours')",(r['fingerprint'],)).fetchone()[0],"tiers":json.loads(r["tiers_json"]),"synced_at":r["synced_at"]} for r in rows]

    def update_policy(self, fingerprint: str, body: dict):
        with self.conn:
            current=self.conn.execute('SELECT * FROM policy WHERE fingerprint=?',(fingerprint,)).fetchone()
            self.conn.execute("UPDATE policy SET enabled=?,multiplier=?,calibrated=?,note=?,preference=?,max_parallel=?,tiers_json=? WHERE fingerprint=?", (int(body.get("enabled",current['enabled'])),float(body.get('multiplier',current['multiplier'])),int(body.get('calibrated',current['calibrated'])),str(body.get('note',current['note'])),int(body.get('preference',current['preference'])),int(body.get('max_parallel',current['max_parallel'])),json.dumps(body.get("tiers",json.loads(current['tiers_json']))),fingerprint))

    def observe(self, **data):
        with self.conn:
            self.conn.execute("INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,input_tokens,output_tokens,cost,request_id) VALUES(:fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error,:status,:input_tokens,:output_tokens,:cost,:request_id)", data)

    def quality(self, window='24h'):
        modifier={'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]
        where="created_at >= datetime('now', ?)"; params=(modifier,)
        row=self.conn.execute(f'SELECT count(*) calls, avg(success) rate, avg(latency_ms) ttft FROM observation WHERE {where}',params).fetchone()
        values=[r[0] for r in self.conn.execute(f'SELECT latency_ms FROM observation WHERE {where} AND latency_ms IS NOT NULL ORDER BY latency_ms',params).fetchall()]
        p95=values[max(0, int(len(values)*.95)-1)] if values else None
        fulfillment=self.conn.execute(f'SELECT avg(actual_model=requested_model) FROM observation WHERE {where}',params).fetchone()[0]
        failures={s:self.conn.execute(f'SELECT count(*) FROM observation WHERE {where} AND status=?',params+(s,)).fetchone()[0] for s in ('cancelled','timed_out','transport_failed','protocol_failed','stream_incomplete')}
        return {'calls':row['calls'],'technical_success_rate':row['rate'],'avg_ttft_ms':row['ttft'],'p95_ttft_ms':p95,'model_fulfillment_rate':fulfillment,'failures':failures}
    def calls(self, limit, cursor=None, provider=None, status=None, window='24h'):
        clauses=["created_at >= datetime('now', ?)"]; params=[{'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]]
        if cursor: clauses.append('id < ?'); params.append(int(cursor))
        if provider: clauses.append('fingerprint=?'); params.append(provider)
        if status: clauses.append('status=?'); params.append(status)
        rows=self.conn.execute('SELECT o.*,s.name provider_name,p.note FROM observation o LEFT JOIN source_provider s ON s.fingerprint=o.fingerprint LEFT JOIN policy p ON p.fingerprint=o.fingerprint WHERE '+ ' AND '.join('o.'+x if not x.startswith('created_at') else x for x in clauses)+' ORDER BY o.id DESC LIMIT ?',(*params,limit)).fetchall()
        return [{'id':r['id'],'time':r['created_at'],'provider':r['provider_name'] or r['fingerprint'],'note':r['note'],'requested_model':r['requested_model'],'actual_model':r['actual_model'],'intellect':r['tier'],'effort':r['effort'],'ttft_ms':r['latency_ms'],'status':r['status'],'input_tokens':r['input_tokens'],'output_tokens':r['output_tokens'],'cost':r['cost'],'request_id':r['request_id']} for r in rows]
