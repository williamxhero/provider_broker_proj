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
        """)
        try: self.conn.execute('ALTER TABLE policy ADD COLUMN calibrated INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE policy ADD COLUMN multiplier REAL NOT NULL DEFAULT 1.0')
        except sqlite3.OperationalError: pass
        self.conn.commit()

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
        rows = self.conn.execute("SELECT s.*,p.enabled,p.price_group,p.multiplier,p.calibrated,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id").fetchall()
        return [{"fingerprint":r["fingerprint"],"name":r["name"],"base_url":r["base_url"],"family":r["provider_type"],"models":json.loads(r["models_json"]),"inventory_status":json.loads(r['source_json']).get('inventory_status'),"enabled":bool(r["enabled"]),"calibrated":bool(r["calibrated"]),"multiplier":r['multiplier'],"tiers":json.loads(r["tiers_json"]),"synced_at":r["synced_at"]} for r in rows]

    def update_policy(self, fingerprint: str, body: dict):
        with self.conn:
            self.conn.execute("UPDATE policy SET enabled=?,multiplier=?,calibrated=?,tiers_json=? WHERE fingerprint=?", (int(body.get("enabled", True)),float(body.get('multiplier',1)),int(body.get('calibrated',False)),json.dumps(body.get("tiers",["standard","smart","expert"])),fingerprint))

    def observe(self, **data):
        with self.conn:
            self.conn.execute("INSERT INTO observation(fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error) VALUES(:fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error)", data)
