import hashlib
import hmac
import json
import sqlite3
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
        try: self.conn.execute('ALTER TABLE source_provider ADD COLUMN request_headers BLOB')
        except sqlite3.OperationalError: pass
        if not catalog_exists:
            from .catalog import CATALOG
            self.conn.executemany(
                "INSERT INTO model_catalog VALUES(?,?,?,?,?,?)",
                [(model, item['family'], item['intellect'], item['official_input_price'], item['official_cache_price'], item['official_output_price']) for model, item in CATALOG.items()],
            )
        self.conn.execute("INSERT OR IGNORE INTO broker_setting(name,value) VALUES('race_parallel_cap',?)", (str(self.default_race_parallel_cap),))
        self.conn.commit()

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
                pricing = catalog[models[0]]
                result.append(Provider(r['id'],r['fingerprint'],r['name'],r['base_url'],self._decrypt(r['api_key']),r['provider_type'],headers,models,pricing,int(blended_price(pricing)*r['multiplier']*100000),int(r['max_parallel']),bool(r['enabled']),float(r['multiplier'])))
        return result

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
        with self.conn:
            self.conn.execute("INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,input_tokens,output_tokens,cost,request_id) VALUES(:fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error,:status,:input_tokens,:output_tokens,:cost,:request_id)", data)

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
        return [{'id':r['id'],'time':r['created_at'],'provider':r['provider_name'] or r['fingerprint'],'note':r['note'],'requested_model':r['requested_model'],'actual_model':r['actual_model'],'intellect':r['tier'],'effort':r['effort'],'ttft_ms':r['latency_ms'],'status':r['status'],'input_tokens':r['input_tokens'],'output_tokens':r['output_tokens'],'cost':r['cost'],'request_id':r['request_id']} for r in rows]
